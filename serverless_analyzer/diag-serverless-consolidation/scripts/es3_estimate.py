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

from constants import HOURS_PER_MONTH as MONTHLY_HOURS
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


def match_provider(raw: str | None, pricing: dict) -> str | None:
    key = (raw or "").strip().lower()
    name = PROVIDER_BY_NAME.get(key)
    if name in pricing:
        return name
    for provider in pricing:
        if provider.lower() == key:
            return provider
    return None


def _shared_match():
    org = Path(__file__).resolve().parents[2] / "k360-org-report" / "scripts"
    if str(org) not in sys.path:
        sys.path.insert(0, str(org))
    from estimator_layout import match_region as shared
    return shared


def _nearest_name(provider: str, region: str | None, names: list[str]) -> str | None:
    want = _norm(region)
    best = None
    best_score = -1
    for name in names:
        token = _norm(name)
        shared = 0
        for left, right in zip(want, token):
            if left != right:
                break
            shared += 1
        if shared > best_score:
            best_score = shared
            best = name
    return best


def _option_name(option: str) -> str:
    return option.split(":", 1)[-1].strip().split(" (", 1)[0].strip()


def match_region(provider: str, region: str | None, pricing: dict, *, allow_nearest: bool = False) -> tuple[str | None, str]:
    """Same rule as the sheet matcher. A miss uses that cloud's default region.

    AWS us-east-1, Azure eastus, GCP us-east1. Nearest is only used when that
    default is also absent from the price table. Another cloud is never used.
    """
    names = list((pricing or {}).get(provider) or [])
    options = [f"{provider}: {name} ({name})" for name in names]
    option, how = _shared_match()(provider, region, options)
    if option is not None:
        return _option_name(option), how
    if str(region or "").strip() and str(provider or "").strip():
        org = Path(__file__).resolve().parents[2] / "k360-org-report" / "scripts"
        if str(org) not in sys.path:
            sys.path.insert(0, str(org))
        from estimator_layout import provider_fallback_option

        fallback = provider_fallback_option(provider, options)
        if fallback is not None:
            return _option_name(fallback), "provider_default"
    if allow_nearest:
        nearest = _nearest_name(provider, region, names)
        if nearest is not None:
            return nearest, "nearest"
    return None, "none"


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


def workload_share(stored_gb: float, org_gb: float) -> float:
    """Share of org-wide index telemetry that belongs to one Search project.

    The numerator is that project's diagnostic search store. The denominator is
    diagnostic org store, the store the telemetry actually covers. A missing org
    store keeps the whole telemetry on the project. The result is clamped to 0–1.
    """
    stored = max(float(stored_gb or 0), 0.0)
    org = max(float(org_gb or 0), 0.0)
    if not org or not stored:
        return 1.0
    return min(max(stored / org, 0.0), 1.0)


def scale_search_workload(workload: dict, share: float) -> dict:
    """Copy of the org workload with docs/s and both QPS bands multiplied by `share`."""
    wl = json.loads(json.dumps(workload or {}))
    factor = float(share)
    search = dict(wl.get("search") or {})
    for band in ("peak", "nonpeak"):
        band_row = dict(search.get(band) or {})
        if band_row.get("qps") is not None:
            band_row["qps"] = round(float(band_row["qps"]) * factor, 4)
        if band_row:
            search[band] = band_row
    if search:
        wl["search"] = search
    ingest = dict(wl.get("ingest") or {})
    if ingest.get("docs_per_sec") is not None:
        # A project with a tiny store has a tiny share. Four decimals rounded it to zero and the
        # estimator then said it had no measured ingest, which dropped the whole plan.
        ingest["docs_per_sec"] = round(float(ingest["docs_per_sec"]) * factor, 10)
        wl["ingest"] = ingest
    return wl


def ingest_workloads(workload: dict, assumptions: list[str]) -> list[dict]:
    """Ingest inputs. Docs/s is already the project's share of the org rate."""
    del assumptions
    ingest = (workload or {}).get("ingest") or {}
    telemetry = (workload or {}).get("quote_window_telemetry") or {}
    if telemetry.get("status") != "complete":
        raise ValueError(
            "Search estimate requires measured workload for this project: "
            + str(telemetry.get("reason") or "quote-window telemetry was not verified")
        )
    dps = ingest.get("docs_per_sec")
    avg = ingest.get("avg_doc_bytes")
    if not dps or not avg or float(dps) <= 0 or float(avg) <= 0:
        raise ValueError("Search estimate requires measured docs/s and average document size from Cloud Tracker telemetry")
    active = ingest.get("active_hours_mo")
    hours = MONTHLY_HOURS if active is None else min(MONTHLY_HOURS, max(0.0, float(active)))
    return [
        {
            "docs_per_sec": round(float(dps), 10),
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
    share = workload_share(stored, org_gb)
    workload = scale_search_workload(payload.get("workload") or {}, share)
    if org_gb and stored and share < 1:
        assumptions.append(
            f"Docs/s and QPS scaled by this project's share of diagnostic org store ({share:.3f})."
        )
    provider = match_provider(payload.get("provider"), pricing)
    if provider is None:
        raise ValueError(f"region {payload.get('region')} is not on the estimator list")
    region, how = match_region(
        provider, payload.get("region"), pricing, allow_nearest=bool(payload.get("allow_nearest_region")),
    )
    if how == "none" or not region:
        raise ValueError(f"region {payload.get('region')} is not on the estimator list")
    if how == "provider_default":
        assumptions.append(
            f"Region {payload.get('region')!r} is not on the estimator list. It is priced as {region}."
        )
    if how == "nearest":
        assumptions.append(
            f"Region {payload.get('region')!r} is not on the estimator list. Nearest same-provider option {region} was written."
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
        "SEARCH_CONFIG": {"workloads": search_workloads(workload, assumptions)},
        "INGEST_CONFIG": {"workloads": ingest_workloads(workload, assumptions)},
        "TEXT_CONFIG": {"disk_storage_gb": stored},
        "VECTOR_CONFIG": {"num_docs": 0, "dims": 0, "compression": "none"},
        "TS_CONFIG": {"ingest_per_day_gb": 0, "boost_window_days": 0, "retention_days": 0},
        "REFRESH_MULT": 1.0,
    }
    label = f"{provider} {region} ({how})"
    if how in {"provider_default", "nearest"}:
        label = f"{provider} {region} ({how}; wanted {payload.get('region')})"
    return user_info, label, assumptions


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
