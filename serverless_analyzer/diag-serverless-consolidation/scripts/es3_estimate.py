#!/usr/bin/env python3
"""Price a consolidated Search slice with the es3-estimator library.

The Jupyter UI in elastic/es3-estimator calls ``estimate_serverless_pricing``.
This script builds the same ``user_info`` dict from a K360 search slice and
prints JSON. Run it with the estimator venv (scikit-learn 1.7.2), not the
Sheets venv:

  ES3_ESTIMATOR_PY=~/.config/es3-estimator/venv/bin/python
  echo '{...}' | "$ES3_ESTIMATOR_PY" es3_estimate.py

Stdout is one JSON object. The library's own prints are swallowed.
"""
from __future__ import annotations

import json
import os
import sys
import warnings
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

MONTHLY_HOURS = 730
# The search-power setting whose total is the Search quote. The estimator's README
# says more than 98% of customers run performant, so its model has the most data
# for it. The other settings are reported beside it and are never the quote.
QUOTED_SEARCH_PROFILE = "performant"
QTT_BY_COMPLEXITY = {"Low": 25.0, "Medium": 100.0, "High": 300.0}
SUPPORT_BY_TIER = {
    "premium": "Enterprise",
    "enterprise": "Enterprise",
    "enhanced": "Platinum",
    "platinum": "Platinum",
    "base": "Gold",
    "gold": "Gold",
    "limited": "Standard",
    "standard": "Standard",
}
PROVIDER_BY_NAME = {
    "aws": "AWS",
    "amazon": "AWS",
    "azure": "Azure",
    "gcp": "GCP",
    "google": "GCP",
}


def estimator_dir() -> Path:
    env = os.environ.get("ES3_ESTIMATOR_DIR")
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates.append(Path.home() / "Documents" / "GItHub" / "es3-estimator-main")
    for root in candidates:
        if (root / "lib" / "estimation.py").is_file():
            return root
        nested = root / "estimator"
        if (nested / "lib" / "estimation.py").is_file():
            return nested
    raise FileNotFoundError(
        "es3-estimator library not found. Set ES3_ESTIMATOR_DIR to the clone "
        "(the directory that contains estimator/lib)."
    )


def _norm(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def _azure_aliases(region: str) -> set[str]:
    r = _norm(region)
    out = {r}
    # eastus2 ⇄ useast2, and a leading provider token (azureeastus2) stripped by the caller
    for compass in ("east", "west", "north", "south", "central", "southeast", "northeast"):
        for geo in (
            "us", "europe", "asia", "india", "australia", "uk", "japan", "brazil",
            "canada", "germany", "france", "norway", "sweden", "switzerland",
            "uae", "korea", "africa", "qatar", "poland", "italy", "israel", "spain",
        ):
            if r.startswith(compass + geo):
                out.add(geo + compass + r[len(compass + geo):])
            if r.startswith(geo + compass):
                out.add(compass + geo + r[len(geo + compass):])
    return out


def match_provider(raw: str | None, pricing: dict) -> str | None:
    key = (raw or "").strip().lower()
    name = PROVIDER_BY_NAME.get(key)
    if name in pricing:
        return name
    for provider in pricing:
        if provider.lower() == key:
            return provider
    return None


def match_region(provider: str, region: str | None, pricing: dict) -> tuple[str, str]:
    options = list(pricing[provider])
    if not options:
        raise ValueError(f"no regions priced for {provider}")
    token = _norm(region or "")
    for prefix in ("aws", "azure", "gcp"):
        if token.startswith(prefix):
            token = token[len(prefix):]
            break
    want = _azure_aliases(token) if token else set()
    for option in options:
        if _norm(option) in want:
            how = "exact" if _norm(option) == token else "alias"
            return option, how
    loose = {item[:-1] if item and item[-1].isdigit() else item for item in want}
    for option in options:
        folded = _norm(option)
        folded = folded[:-1] if folded and folded[-1].isdigit() else folded
        if folded and folded in loose:
            return option, "alias"
    return options[0], "provider_default"


def _hours(pct) -> float | None:
    if pct is None or pct == "":
        return None
    return max(0.0, float(pct) / 100.0 * MONTHLY_HOURS)


def _qtt(band: dict) -> float:
    ms = band.get("complexity_ms")
    if ms is not None and float(ms) > 0:
        return float(ms)
    return QTT_BY_COMPLEXITY.get(band.get("complexity") or "", 10.0)


def search_workloads(workload: dict, assumptions: list[str]) -> list[dict]:
    search = (workload or {}).get("search") or {}
    bands = []
    for name in ("peak", "nonpeak"):
        band = search.get(name) or {}
        if band.get("qps") is None:
            continue
        bands.append(band)
    if not bands:
        assumptions.append(
            "No search QPS in the workload window. Search $ is the idle storage floor only."
        )
        return [{"QPS": 0.0, "QTT": 10.0, "active_hours_per_month": 0.0}]

    specified: list[tuple[dict, float]] = []
    missing: list[dict] = []
    for band in bands:
        hours = _hours(band.get("pct_hours_mo"))
        if hours is None:
            missing.append(band)
        else:
            specified.append((band, hours))
    if missing:
        used = sum(hours for _, hours in specified)
        each = (MONTHLY_HOURS if not specified else max(0.0, MONTHLY_HOURS - used)) / len(missing)
        assumptions.append(
            "A search band had QPS but no hour share; remaining hours of the 730 h month were split across those bands."
        )
        for band in missing:
            specified.append((band, each))
    total = sum(hours for _, hours in specified)
    if total > MONTHLY_HOURS and total > 0:
        scale = MONTHLY_HOURS / total
        specified = [(band, hours * scale) for band, hours in specified]
        assumptions.append("Peak and non-peak hour shares summed past 730 h and were scaled down.")
    return [
        {
            "QPS": float(band.get("qps") or 0),
            "QTT": _qtt(band),
            "active_hours_per_month": round(hours, 2),
        }
        for band, hours in specified
    ]


def ingest_workloads(workload: dict, stored_gb: float, org_gb: float, assumptions: list[str]) -> list[dict]:
    ingest = (workload or {}).get("ingest") or {}
    dps = ingest.get("docs_per_sec")
    avg = ingest.get("avg_doc_bytes")
    if not dps or not avg or float(dps) <= 0 or float(avg) <= 0:
        assumptions.append(
            "Ingest omitted: docs/s or average document size is missing, so ingest $ is 0."
        )
        return []
    share = (stored_gb / org_gb) if org_gb and stored_gb else 1.0
    share = min(max(share, 0.0), 1.0)
    if org_gb and stored_gb and share < 1:
        assumptions.append(
            f"Ingest docs/s scaled by the search share of stored GB ({share:.3f}). QPS is not scaled."
        )
    active = ingest.get("active_hours_mo")
    hours = MONTHLY_HOURS if active is None else min(MONTHLY_HOURS, max(0.0, float(active)))
    return [
        {
            "docs_per_sec": round(float(dps) * share, 4),
            "avg_doc_size_bytes": float(avg),
            "active_hours_per_month": round(hours, 2),
        }
    ]


def build_user_info(payload: dict, pricing: dict, support_matrix: dict) -> tuple[dict, str, list[str]]:
    assumptions: list[str] = [
        "Workload type is text. Vector and time-series profiles are not inferred from K360.",
        "QPS and query time are day averages from index telemetry, not peak-second load.",
        "Performant is the quoted profile. On-demand and high availability are shown beside it.",
    ]
    stored = max(float(payload.get("stored_gb") or 0), 0.0)
    org_gb = max(float(payload.get("org_stored_gb") or 0), 0.0)
    provider = match_provider(payload.get("provider"), pricing)
    if provider is None:
        provider = "AWS"
        how = "provider_default"
        assumptions.append(f"Cloud provider {payload.get('provider')!r} is not in the estimator. Used AWS.")
    else:
        how = None
    region, region_how = match_region(provider, payload.get("region"), pricing)
    if how is None:
        how = region_how
    if how == "provider_default":
        assumptions.append(
            f"Region {payload.get('region')!r} did not match a priced {provider} region. Used {region}."
        )
    tier = str(payload.get("support_tier") or "Premium").strip().lower()
    support = SUPPORT_BY_TIER.get(tier)
    if support not in support_matrix:
        support = "Enterprise"
        assumptions.append(f"Support tier {payload.get('support_tier')!r} mapped to Enterprise (15%).")
    user_info = {
        "WORKLOAD": "text",
        "CLOUD_PROVIDER": provider,
        "REGION": region,
        "SUPPORT_LEVEL": support,
        "SEARCH_CONFIG": {"workloads": search_workloads(payload.get("workload") or {}, assumptions)},
        "INGEST_CONFIG": {"workloads": ingest_workloads(payload.get("workload") or {}, stored, org_gb, assumptions)},
        "TEXT_CONFIG": {"disk_storage_gb": stored},
        "VECTOR_CONFIG": {"num_docs": 0, "dims": 0, "compression": "none"},
        "TS_CONFIG": {"ingest_per_day_gb": 0, "boost_window_days": 0, "retention_days": 0},
        "REFRESH_MULT": 1.0,
    }
    return user_info, f"{provider} {region} ({how})", assumptions


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def readback_from(profiles: list[dict]) -> dict | None:
    # No fallback to another setting: a quote labelled performant is the performant total or nothing.
    chosen = next((row for row in profiles if row.get("Search Power Setting") == QUOTED_SEARCH_PROFILE), None)
    if not chosen:
        return None
    monthly = _num(chosen.get("Total $/month"))
    if monthly is None:
        return None
    return {
        "profile": chosen.get("Search Power Setting"),
        "monthly_steady_state": round(monthly, 2),
        "annual_year1": round(monthly * 12, 2),
        "annual_steady_state": round(monthly * 12, 2),
        "search_monthly": _num(chosen.get("Search $/month")),
        "ingest_monthly": _num(chosen.get("Ingest $/month")),
        "storage_monthly": _num(chosen.get("Storage $/month")),
        "support_monthly": _num(chosen.get("Support $/month")),
        "provisioned_search_vcu": _num(chosen.get("Provisioned Search VCU/h")),
        "idle_search_vcu": _num(chosen.get("Idle Search VCU/h")),
        "minimum_search_vcu": _num(chosen.get("Minimum Search VCU/h")),
        "search_vcu_hours": _num(chosen.get("Total Search VCU-hrs/mo")),
        "ingest_vcu": _num(chosen.get("Ingest VCU")),
        "ingest_vcu_hours": _num(chosen.get("Ingest VCU-hrs/mo")),
        "storage_gb": _num(chosen.get("Storage (GB)")),
        "profile_limitation": chosen.get("Profile Limitation"),
    }


def run(payload: dict) -> dict:
    root = estimator_dir()
    os.chdir(root)
    sys.path.insert(0, str(root))
    # Models load from ./models at import time.
    from lib.display import build_report_csv
    from lib.estimation import estimate_serverless_pricing
    from lib.service_data import SUPPORT_MATRIX, pricing
    from lib.user_config import validate_user_config

    user_info, region_match, assumptions = build_user_info(payload, pricing, SUPPORT_MATRIX)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate_user_config(user_info)
        sink = StringIO()
        with redirect_stdout(sink):
            profiles = estimate_serverless_pricing(user_info)
    for row in profiles:
        limit = row.get("Profile Limitation")
        if limit is not None and not isinstance(limit, (str, int, float)):
            row["Profile Limitation"] = str(limit)
    for item in caught:
        assumptions.append(str(item.message))
    rb = readback_from(profiles)
    status = "estimated" if rb else f"estimated — {QUOTED_SEARCH_PROFILE} total was not numeric"
    return {
        "status": status,
        "source": "es3-estimator estimate_serverless_pricing",
        "estimator_dir": str(root),
        "region_match": region_match,
        "support_level": user_info["SUPPORT_LEVEL"],
        "assumptions": assumptions,
        "inputs": {
            "provider": user_info["CLOUD_PROVIDER"],
            "region": user_info["REGION"],
            "support_level": user_info["SUPPORT_LEVEL"],
            "stored_gb": user_info["TEXT_CONFIG"]["disk_storage_gb"],
            "search_workloads": user_info["SEARCH_CONFIG"]["workloads"],
            "ingest_workloads": user_info["INGEST_CONFIG"]["workloads"],
        },
        "profiles": profiles,
        "readback": rb,
        "report_csv": build_report_csv(user_info, profiles),
    }


def main() -> int:
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    try:
        out = run(payload)
    except Exception as exc:  # the caller records this; do not traceback into stdout
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(out, default=str))
    return 0 if out.get("readback") else 1


if __name__ == "__main__":
    sys.exit(main())
