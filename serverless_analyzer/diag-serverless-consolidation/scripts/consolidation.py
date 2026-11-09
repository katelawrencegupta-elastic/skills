#!/usr/bin/env python3
"""Serverless consolidation-savings analysis (runs under the same ECH > 50 % trigger).

Explains *why* moving the ECH footprint onto a few serverless projects saves money.
Every serverless dollar is an estimator read-back (es3-estimator on-demand for
Search, the sheet for Observability and Security). ECH list is $1 per ECU.
The contract rate is recorded on the ECH bill only. Deployments, the tail, and
retention / fleet scenarios are ECU, GB, and overhead share — not a second price.
Pure function of `account.json` + `infra.json`; no network.

  python consolidation.py --account-json <key>_account.json --infra-json <key>_infra.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from quote_checks import e73_blocks_quote, g26_gate, integrity_score, money as _money
from skill_paths import find_skill

DAYS_PER_MONTH = 30.416
TAIL_SHARE_PCT = 1.0          # "small" deployment = < 1 % of lookback ECU (same as cost rec)
TOP_N = 15
RETENTION_SCENARIOS = (1, 3, 6, 12)
FIXED_TIERS = {"master", "kibana", "coordinating", "integrations server", "enterprise search", "apm"}

ORG_SKILL = find_skill("k360-org-report", Path(__file__).resolve().parent)

# --------------------------------------------------------------------------- pricing
# Prefer the parent skill's ladders (single source of truth); mirror only as a fallback.
try:  # pragma: no cover
    sys.path.insert(0, str(ORG_SKILL / "scripts"))
    from fetch_org import (  # type: ignore
        _O11Y_INGEST_EDGES,
        _O11Y_INGEST_PRICES,
        _O11Y_RET_EDGES,
        _O11Y_RET_PRICES,
        _tiered_cost,
        price_es3_search,
        price_o11y_serverless,
    )
    PRICING_SOURCE = f"parent fetch_org.py ({ORG_SKILL})"
except Exception:  # fallback mirror of Estimator_Reference v1.19 ladders
    _O11Y_INGEST_EDGES = (50, 100, 200, 500, 1000, 2000, 5000, 1e18)
    _O11Y_INGEST_PRICES = (0.50, 0.325, 0.225, 0.15, 0.105, 0.10, 0.095, 0.0925)
    _O11Y_RET_EDGES = (10_000, 20_000, 50_000, 100_000, 250_000, 1_000_000, 2_500_000, 1e18)
    _O11Y_RET_PRICES = (0.040, 0.032, 0.030, 0.028, 0.026, 0.022, 0.020, 0.019)
    PRICING_SOURCE = "local mirror of Estimator_Reference v1.19 ladders (parent skill not importable)"

    def _tiered_cost(gb: float, edges: tuple, prices: tuple) -> float:
        left, cost, lo = float(gb), 0.0, 0.0
        for hi, price in zip(edges, prices):
            take = min(left, hi - lo)
            if take > 0:
                cost += take * price
            left -= take
            lo = hi
            if left <= 0:
                break
        return cost

    def price_o11y_serverless(daily_raw_gb: float, retention_months: float, *, meter_factor: float = 1.595, support_uplift: float = 0.15) -> dict:
        metered = daily_raw_gb * meter_factor
        ingest_mo = _tiered_cost(metered, _O11Y_INGEST_EDGES, _O11Y_INGEST_PRICES) * DAYS_PER_MONTH
        retained = metered * DAYS_PER_MONTH * retention_months
        retain_mo = _tiered_cost(retained, _O11Y_RET_EDGES, _O11Y_RET_PRICES)
        sub = ingest_mo + retain_mo
        support = sub * support_uplift
        return {
            "daily_raw_gb": round(daily_raw_gb, 1), "daily_metered_gb": round(metered, 1), "retention_months": retention_months,
            "retained_gb": round(retained, 1), "ingest_mo": round(ingest_mo, 2), "retention_mo": round(retain_mo, 2),
            "support_mo": round(support, 2), "total_mo": round(sub + support, 2), "total_yr": round((sub + support) * 12, 2),
        }

    price_es3_search = None  # type: ignore


O11Y_METER_FACTOR = 1.595


def observability_metered_rates(rows: list[dict]) -> list[dict]:
    """Blended Observability ingest rate when each deployment is priced alone.

    Metered GB/day is raw bulk times 1.595. Logs are 90% of that and metrics
    are 10%. Each stream walks the ingest bands. The rate is ingest dollars
    per metered GB. A row with no bulk has no rate. This is not a D32 quote.
    """
    out = []
    for row in rows:
        try:
            raw = float(row.get("daily_raw_gb") if row.get("daily_raw_gb") is not None else row.get("bulk_gb_day") or 0)
        except (TypeError, ValueError):
            raw = 0.0
        logs = raw * 0.90 * O11Y_METER_FACTOR
        metrics = raw * 0.10 * O11Y_METER_FACTOR
        metered = logs + metrics
        rate = None
        if metered > 0:
            per_day = _tiered_cost(logs, _O11Y_INGEST_EDGES, _O11Y_INGEST_PRICES) + _tiered_cost(metrics, _O11Y_INGEST_EDGES, _O11Y_INGEST_PRICES)
            rate = round(per_day / metered, 4)
        out.append({
            "name": row.get("name") or "",
            "raw_gb_day": round(raw, 3),
            "metered_gb_day": round(metered, 3),
            "usd_per_gb": rate,
        })
    out.sort(key=lambda r: (-(r["metered_gb_day"] or 0), r["name"]))
    return out


def _price_slice(solution: str, gb: float, retention_months: float, workload: dict | None) -> dict:
    """Price one consolidated slice. Observability + Security → Complete ladders (Security = proxy);
    Search → ES3 on-demand ballpark when the parent is importable, else Complete ladders flagged as proxy."""
    if gb <= 0:
        return {"total_mo": 0.0, "total_yr": 0.0, "daily_raw_gb": 0.0, "ingest_mo": 0.0, "retention_mo": 0.0, "support_mo": 0.0, "daily_metered_gb": 0.0, "retained_gb": 0.0, "pricing": "none"}
    if solution == "search" and price_es3_search is not None:
        p = dict(price_es3_search(gb, workload))
        p.setdefault("daily_raw_gb", round(gb / (retention_months * DAYS_PER_MONTH), 1))
        p["pricing"] = "ES3 on-demand ballpark (storage-floored VCU + retain)"
        return p
    p = dict(price_o11y_serverless(gb / (retention_months * DAYS_PER_MONTH), retention_months))
    p["pricing"] = "Observability Complete ladders" if solution == "observability" else f"Observability Complete ladders as proxy for {solution}"
    return p


def _ladder_walk(metered_gb_day: float) -> list[dict]:
    rows, lo, left = [], 0.0, float(metered_gb_day)
    for hi, price in zip(_O11Y_INGEST_EDGES, _O11Y_INGEST_PRICES):
        take = min(left, hi - lo)
        if take > 0:
            rows.append({"band_gb_day": f"{lo:,.0f} – {hi:,.0f}" if hi < 1e17 else f"> {lo:,.0f}", "usd_per_gb": price, "gb_in_band": round(take, 1), "usd_mo": round(take * price * DAYS_PER_MONTH, 2)})
        left -= take
        lo = hi
        if left <= 0:
            break
    return rows


def _stash_model(blk: dict) -> None:
    """Keep the ladder or storage-floor figure off to the side. It is not a price."""
    blk["ballpark"] = {
        "total_mo": blk.get("total_mo"),
        "search_mo": blk.get("search_mo"),
        "retention_mo": blk.get("retention_mo"),
        "ingest_mo": blk.get("ingest_mo"),
        "support_mo": blk.get("support_mo"),
        "peak_vcu": blk.get("peak_vcu"),
        "idle_vcu": blk.get("idle_vcu"),
        "pricing": blk.get("pricing"),
        "daily_raw_gb": blk.get("daily_raw_gb"),
        "daily_metered_gb": blk.get("daily_metered_gb"),
    }


def _clear_components(blk: dict) -> None:
    for k in ("search_mo", "ingest_mo", "retention_mo", "support_mo"):
        blk[k] = None


def _apply_search_estimator(by_solution: dict, sz: dict) -> bool:
    """Replace the consolidated Search total with the es3-estimator on-demand read-back."""
    est = ((sz.get("estimators") or {}).get("search") or {})
    rb = est.get("readback") or {}
    monthly = _money(rb.get("monthly_steady_state"))
    search = by_solution.get("search") or {}
    if monthly is None:
        return False
    _stash_model(search)
    search["search_mo"] = _money(rb.get("search_monthly"))
    search["ingest_mo"] = _money(rb.get("ingest_monthly"))
    search["retention_mo"] = _money(rb.get("storage_monthly"))
    search["support_mo"] = _money(rb.get("support_monthly"))
    search["total_mo"] = round(monthly, 2)
    search["total_yr"] = round(monthly * 12, 2)
    search["peak_vcu"] = rb.get("provisioned_search_vcu")
    search["idle_vcu"] = rb.get("idle_search_vcu")
    search["pricing"] = "es3-estimator on-demand (quotable)"
    search["estimator_profile"] = rb.get("profile") or "on-demand"
    return True


def _apply_sheet_readback(by_solution: dict, sz: dict, solution: str) -> bool:
    """Replace an Observability or Security total with that sheet's read-back."""
    est = ((sz.get("estimators") or {}).get(solution) or {})
    rb = est.get("readback") or {}
    monthly = _money(rb.get("monthly_steady_state"))
    blk = by_solution.get(solution) or {}
    written = float(blk.get("daily_raw_gb") or 0)
    if monthly is None or not ((blk.get("stored_gb") or 0) or written):
        return False
    _stash_model(blk)
    _clear_components(blk)
    raw = _money(rb.get("total_raw_gb_day"))
    metered = _money(rb.get("metered_gb_day"))
    if raw is not None:
        blk["sheet_raw_gb_day"] = raw
    if metered is not None:
        blk["sheet_metered_gb_day"] = metered
        blk["daily_metered_gb"] = metered
    y1 = _money(rb.get("annual_year1"))
    blk["total_mo"] = round(monthly, 2)
    blk["total_yr"] = round(y1, 2) if y1 is not None else round(monthly * 12, 2)
    blk["pricing"] = f"{solution.title()} sheet read-back (quotable)"
    return True


def _omit_unquoted(blk: dict, notes: list[str], solution: str) -> None:
    if not ((blk.get("stored_gb") or 0) or (blk.get("daily_raw_gb") or 0)):
        return
    pricing = str(blk.get("pricing") or "")
    if "quotable" in pricing:
        return
    _stash_model(blk)
    _clear_components(blk)
    blk["total_mo"] = 0.0
    blk["total_yr"] = 0.0
    if pricing.startswith("rejected"):
        return
    blk["pricing"] = "omitted — no estimator read-back"
    notes.append(f"{solution.title()} has stored GB but no estimator read-back, so it is left out of the serverless total.")


def _query_workload(sz: dict, acc: dict) -> dict:
    return ((sz.get("consolidated") or {}).get("search") or {}).get("workload") or acc.get("workload") or {}


def _has_query_rate(workload: dict) -> bool:
    try:
        return float((((workload.get("search") or {}).get("peak") or {}).get("qps")) or 0) > 0
    except (TypeError, ValueError):
        return False


def _sized_store_gb(sz: dict, acc: dict) -> float:
    """Primary store on the cover. Searchable snapshots are already out of this figure."""
    diag = (sz.get("diagnostics") or {}).get("store_gb")
    if diag:
        return float(diag)
    stored = float((acc.get("storage") or {}).get("data_stored_gb") or 0)
    if stored:
        return stored
    con = sz.get("consolidated") or {}
    return sum(float((con.get(sol) or {}).get("stored_gb") or 0) for sol in ("observability", "security", "search"))


def _search_power(sz: dict, by_solution: dict, acc: dict | None = None) -> dict | None:
    """es3 profiles for the search-power page. They are not added to D32 or E74.

    A quoted Search slice uses its estimator profiles. When Search has no local
    store, the page still prices the sized stored GB and the measured query rate.
    """
    acc = acc or {}
    regional = (by_solution.get("search") or {}).get("regional_profiles") or []
    est = (sz.get("estimators") or {}).get("search") or {}
    profiles = regional or (est.get("profiles") or [])
    if profiles:
        out = {"profiles": profiles}
        if est.get("region_match") and not regional:
            out["region_match"] = est["region_match"]
        return out
    stored = _sized_store_gb(sz, acc)
    workload = _query_workload(sz, acc)
    if stored <= 0 or not _has_query_rate(workload):
        return None
    cached = sz.get("search_power") or {}
    cached_gb = (cached.get("inputs") or {}).get("stored_gb")
    if cached.get("profiles") and cached_gb is not None and abs(float(cached_gb) - stored) < 0.05:
        return {"profiles": cached["profiles"], "region_match": cached.get("region_match")}
    cloud = (acc.get("cloud") or {}).get("summary") or {}
    result = _es3_region({
        "stored_gb": stored,
        "org_stored_gb": stored,
        "provider": sz.get("provider") or cloud.get("provider"),
        "region": sz.get("region") or cloud.get("region"),
        "support_tier": sz.get("support_tier") or "Premium",
        "workload": workload,
    }) or {}
    profiles = result.get("profiles") or []
    if not profiles:
        return None
    sz["search_power"] = {
        "profiles": profiles,
        "region_match": result.get("region_match"),
        "inputs": {"stored_gb": round(stored, 2)},
    }
    return {"profiles": profiles, "region_match": result.get("region_match")}


def _deployment_bars(sz: dict, by_solution: dict, notes: list[str]) -> list:
    """Page-1 Observability bars. 100% reuses the headline read-back."""
    raw = sz.get("deployment_plan") or {}
    if not raw:
        return []
    if raw.get("error"):
        notes.append(str(raw["error"]))
        return []
    bars = []
    headline = by_solution.get("observability") or {}
    for row in raw.get("bars") or []:
        monthly = row.get("monthly")
        if row.get("reused_headline"):
            monthly = headline.get("total_mo") if "quotable" in str(headline.get("pricing") or "") else None
        if monthly is None:
            notes.append("Observability deployment-count plan was not priced.")
            return []
        bars.append({"label": row.get("label") or "", "monthly": monthly, "projects": row.get("projects")})
    return bars


def _deployment_groups(acc: dict, infra: dict | None, months: float) -> list[dict]:
    """ECH deployments grouped by solution and region. A serverless project lives in one region."""
    region_by_name = {
        d.get("name"): d.get("region")
        for d in ((infra or {}).get("by_deployment") or []) + (((acc.get("cloud") or {}).get("by_deployment")) or [])
        if d.get("region")
    }
    grouped: dict[tuple[str, str], dict] = {}
    for d in (acc.get("solutions") or {}).get("by_deployment") or []:
        if d.get("platform") == "serverless":
            continue
        sol = d.get("solution") or "search"
        reg = region_by_name.get(d["name"]) or "unknown region"
        g = grouped.setdefault((sol, reg), {"solution": sol, "region": reg, "deployments": [], "ech_ecu_mo": 0.0, "stored_gb": 0.0, "daily_raw_gb": 0.0, "retention_weighted": 0.0, "retention_bulk": 0.0, "has_daily": False})
        g["deployments"].append(d["name"])
        g["ech_ecu_mo"] += float(d.get("ecu") or 0) / months
        g["stored_gb"] += float(d.get("gb") or 0)
        if d.get("daily_raw_gb") is not None:
            rate = float(d.get("daily_raw_gb") or 0)
            g["has_daily"] = True
            g["daily_raw_gb"] += rate
            try:
                days = float(d.get("retention_days"))
            except (TypeError, ValueError):
                days = 0.0
            if days > 0:
                g["retention_weighted"] += rate * days
                g["retention_bulk"] = g.get("retention_bulk", 0.0) + rate
    groups = []
    for g in grouped.values():
        row = {
            "solution": g["solution"], "region": g["region"], "deployments": g["deployments"],
            "n": len(g["deployments"]), "ech_ecu_mo": round(g["ech_ecu_mo"], 2), "stored_gb": round(g["stored_gb"], 1),
        }
        if g["has_daily"]:
            row["daily_raw_gb"] = round(g["daily_raw_gb"], 3)
            if g["retention_bulk"] > 0:
                row["retention_months"] = round((g["retention_weighted"] / g["retention_bulk"]) / DAYS_PER_MONTH, 2)
        groups.append(row)
    order = {"search": 0, "observability": 1, "security": 2}
    groups.sort(key=lambda g: (order.get(g["solution"], 9), -g["ech_ecu_mo"]))
    return groups


def _scale_search_qps(workload: dict, share: float) -> dict:
    wl = json.loads(json.dumps(workload or {}))
    search = wl.get("search") or {}
    for band in ("peak", "nonpeak"):
        b = dict(search.get(band) or {})
        if b.get("qps") is not None:
            b["qps"] = round(float(b["qps"]) * share, 4)
        search[band] = b
    wl["search"] = search
    return wl


def _es3_region(payload: dict) -> dict | None:
    py = os.environ.get("ES3_ESTIMATOR_PY") or str(Path.home() / ".config/es3-estimator/venv/bin/python")
    if not Path(py).is_file():
        return None
    try:
        proc = subprocess.run(
            [py, str(Path(__file__).with_name("es3_estimate.py"))],
            input=json.dumps(payload), text=True, capture_output=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _saved_projects(acc: dict) -> dict:
    rows = ((acc.get("sizing") or {}).get("estimators") or {}).get("projects") or []
    return {(p.get("solution"), p.get("region")): p for p in rows if p.get("solution") and p.get("region")}


def _security_sheet_was_read(saved: dict | None) -> bool:
    """True when a Security estimator copy was filled. A missing project is not a blank E73."""
    if not isinstance(saved, dict) or not saved:
        return False
    if saved.get("status") == "filled":
        return True
    readback = saved.get("readback")
    return isinstance(readback, dict) and bool(readback)


def regional_quote(
    classified: list[dict],
    consolidated_monthly: float,
    *,
    solution: str,
    consolidated_annual: float | None = None,
    consolidated_steady: float | None = None,
    consolidated_monthly_unrounded: float | None = None,
) -> dict:
    """One price mode per solution.

    Every region has its own accepted read-back, or every region is a store share
    of the one consolidated total. A rejected Observability or Security region
    zeros that solution. Search uses the same all-or-nothing choice and is not zeroed.
    """
    rejected = [row for row in classified if row.get("kind") == "rejected"]
    if rejected and solution != "search":
        priced = []
        for row in classified:
            item = dict(row)
            item["monthly"] = None
            item["sizing_monthly"] = None
            item["annual_year1"] = None
            item["annual_steady_state"] = None
            if row.get("kind") == "rejected":
                item["pricing"] = row.get("own_pricing") or "rejected"
            else:
                item["pricing"] = "not quoted — another region was rejected"
            priced.append(item)
        return {
            "mode": "rejected",
            "total_mo": 0.0,
            "total_yr": 0.0,
            "pricing": str(rejected[0].get("own_pricing") or "rejected"),
            "rows": priced,
        }
    complete = bool(classified) and all(row.get("kind") == "own" for row in classified)
    if complete:
        priced = []
        for row in classified:
            item = dict(row)
            item["monthly"] = row.get("own_monthly")
            item["monthly_unrounded"] = row.get("own_monthly")
            item["sizing_monthly"] = row.get("own_monthly")
            item["pricing"] = row.get("own_pricing") or "sheet read-back"
            item["annual_year1"] = row.get("own_annual")
            item["annual_steady_state"] = row.get("own_steady")
            priced.append(item)
        total = round(sum(float(p.get("monthly") or 0) for p in priced), 2)
        return {
            "mode": "per_region",
            "total_mo": total,
            "total_yr": round(total * 12, 2),
            "pricing": None,
            "rows": priced,
        }
    total_gb = sum(float(row.get("stored_gb") or 0) for row in classified)
    consolidated = float(consolidated_monthly or 0)
    how = "on-demand read-back split by stored GB" if solution == "search" else "sheet read-back split by stored GB"
    priced = []
    for row in classified:
        gb = float(row.get("stored_gb") or 0)
        share = (gb / total_gb) if total_gb else 0.0
        item = dict(row)
        if gb > 0:
            item["monthly"] = round(consolidated * share, 2)
            if consolidated_monthly_unrounded is not None:
                item["monthly_unrounded"] = float(consolidated_monthly_unrounded) * share
            item["annual_year1"] = None if consolidated_annual is None else round(float(consolidated_annual) * share, 2)
            item["annual_steady_state"] = None if consolidated_steady is None else round(float(consolidated_steady) * share, 2)
        else:
            item["monthly"] = None
            item["monthly_unrounded"] = None
            item["annual_year1"] = None
            item["annual_steady_state"] = None
        item["sizing_monthly"] = item["monthly"]
        item["pricing"] = how
        priced.append(item)
    return {
        "mode": "store_split",
        "total_mo": round(consolidated, 2),
        "total_yr": None if consolidated_annual is None else round(float(consolidated_annual), 2),
        "pricing": None,
        "rows": priced,
    }


def _replicate_by_region(acc: dict, by_solution: dict, groups: list[dict], notes: list[str]) -> bool:
    """One serverless project per solution per region. Each project uses its own sizing when one was written."""
    spans = {}
    for g in groups:
        spans.setdefault(g["solution"], set()).add(g["region"])
    if not any(len(v) > 1 for v in spans.values()):
        return False
    cloud = (acc.get("cloud") or {}).get("summary") or {}
    provider = cloud.get("provider")
    org_gb = float((acc.get("storage") or {}).get("data_stored_gb") or 0)
    search_slice = ((acc.get("sizing") or {}).get("consolidated") or {}).get("search") or {}
    workload = search_slice.get("workload") or acc.get("workload") or {}
    support = ((acc.get("sizing") or {}).get("support_tier")) or "Premium"
    saved = _saved_projects(acc)
    store_split = False
    profile_keys = ("Total $/month", "Provisioned Search VCU/h", "Idle Search VCU/h", "Search $/month", "Ingest $/month", "Storage $/month", "Support $/month")
    for sol, regions in spans.items():
        if len(regions) <= 1:
            continue
        blk = by_solution.get(sol) or {}
        if "quotable" not in str(blk.get("pricing") or ""):
            continue
        rows = [g for g in groups if g["solution"] == sol]
        total_gb = sum(g["stored_gb"] for g in rows) or float(blk.get("stored_gb") or 0)
        classified = []
        for g in rows:
            share = (g["stored_gb"] / total_gb) if total_gb else 0.0
            saved_hit = saved.get((sol, g["region"])) or {}
            saved_rb = saved_hit.get("readback") or {}
            if sol == "search" and str(blk.get("pricing") or "").startswith("es3-estimator") and g["stored_gb"] > 0:
                result = {"readback": saved_rb, "profiles": saved_hit.get("profiles"), "region_match": saved_hit.get("region_match")} if _money(saved_rb.get("monthly_steady_state")) is not None else _es3_region({
                    "stored_gb": g["stored_gb"],
                    "org_stored_gb": org_gb,
                    "provider": provider,
                    "region": g["region"],
                    "support_tier": support,
                    "workload": _scale_search_qps(workload, share),
                })
                rb = (result or {}).get("readback") or {}
                monthly = _money(rb.get("monthly_steady_state"))
                classified.append({
                    "region": g["region"], "stored_gb": g["stored_gb"],
                    "retention_months": g.get("retention_months"),
                    "kind": "own" if monthly is not None else "missing",
                    "own_monthly": monthly, "own_pricing": "re-estimated on-demand",
                    "own_annual": _money(rb.get("annual_year1")),
                    "own_steady": _money(rb.get("annual_steady_state")),
                    "search_mo": _money(rb.get("search_monthly")),
                    "ingest_mo": _money(rb.get("ingest_monthly")),
                    "retention_mo": _money(rb.get("storage_monthly")),
                    "support_mo": _money(rb.get("support_monthly")),
                    "profiles": (result or {}).get("profiles") or [],
                    "region_match": (result or {}).get("region_match"),
                    "url": saved_hit.get("url"),
                })
                continue
            gate = (saved_hit.get("g26_gate") or {}) if sol == "observability" else {}
            g26_rejected = sol == "observability" and (
                bool(saved_hit.get("readback_rejected")) or (bool(gate) and not gate.get("accepted"))
            )
            sheet_read = _security_sheet_was_read(saved_hit)
            e73_rejected = sol == "security" and sheet_read and e73_blocks_quote(saved_rb.get("cps_addon_monthly"))
            if g26_rejected:
                notes.append(f"Observability {g['region']} G26 differed from the written bulk, so that region was not quoted.")
                classified.append({
                    "region": g["region"], "stored_gb": g["stored_gb"], "kind": "rejected",
                    "own_pricing": "rejected — G26 differs from the written bulk",
                    "region_match": saved_hit.get("region_match"), "url": saved_hit.get("url"),
                })
                continue
            if e73_rejected:
                blank = _money(saved_rb.get("cps_addon_monthly")) is None
                notes.append(
                    f"Security {g['region']} E73 was blank, so that region was not quoted."
                    if blank else
                    f"Security {g['region']} still has an E73 add-on, so that region was not quoted."
                )
                classified.append({
                    "region": g["region"], "stored_gb": g["stored_gb"], "kind": "rejected",
                    "own_pricing": "rejected — E73 was blank" if blank else "rejected — E73 add-on is not zero",
                    "region_match": saved_hit.get("region_match"), "url": saved_hit.get("url"),
                })
                continue
            sheet_monthly = _money(saved_rb.get("monthly_steady_state"))
            classified.append({
                "region": g["region"], "stored_gb": g["stored_gb"],
                "retention_months": g.get("retention_months"),
                "kind": "own" if sheet_monthly is not None else "missing",
                "own_monthly": sheet_monthly,
                "own_pricing": "sheet read-back" if sheet_monthly is not None else "",
                "own_annual": _money(saved_rb.get("annual_year1")),
                "own_steady": _money(saved_rb.get("annual_steady_state")),
                "region_match": saved_hit.get("region_match"), "url": saved_hit.get("url"),
            })
        est_rb = (((acc.get("sizing") or {}).get("estimators") or {}).get(sol) or {}).get("readback") or {}
        steady_total = _money(est_rb.get("annual_steady_state"))
        quote = regional_quote(
            classified,
            float(blk.get("total_mo") or 0),
            solution=sol,
            consolidated_annual=blk.get("total_yr"),
            consolidated_steady=steady_total if steady_total is not None else blk.get("total_yr"),
            consolidated_monthly_unrounded=_money(est_rb.get("monthly_steady_state")),
        )
        blk["by_region"] = quote["rows"]
        blk["regional_mode"] = quote["mode"]
        if quote["mode"] == "rejected":
            blk["pricing"] = quote["pricing"]
            blk["total_mo"] = 0.0
            blk["total_yr"] = 0.0
            notes.append(f"{sol.title()} was left out of the quote because a regional read-back was not accepted.")
        elif quote["mode"] == "per_region":
            blk["total_mo"] = quote["total_mo"]
            blk["total_yr"] = quote["total_yr"]
        else:
            store_split = True
        if sol == "search" and quote["mode"] == "per_region":
            for key in ("search_mo", "ingest_mo", "retention_mo", "support_mo"):
                vals = [p.get(key) for p in quote["rows"] if p.get(key) is not None]
                blk[key] = round(sum(vals), 2) if vals else blk.get(key)
            profile_sums: dict[str, dict] = {}
            for row in quote["rows"]:
                for profile in row.get("profiles") or []:
                    name = str(profile.get("Search Power Setting") or "")
                    if not name:
                        continue
                    bucket = profile_sums.setdefault(name, {"Search Power Setting": name})
                    for key in profile_keys:
                        val = profile.get(key)
                        if isinstance(val, (int, float)):
                            bucket[key] = round(float(bucket.get(key) or 0) + float(val), 2)
            if profile_sums:
                blk["regional_profiles"] = list(profile_sums.values())
    n_regions = len({g["region"] for g in groups})
    note = (
        f"Deployments run in {n_regions} regions, so the estimate is one serverless project per solution per region. "
        "Each project has its own sizing, named by region. "
        "Search is re-estimated in that region. Observability and Security are a sheet filled for that region."
    )
    misses = [
        (sol, p)
        for sol, blk in by_solution.items()
        for p in (blk.get("by_region") or [])
        if "provider_default" in str(p.get("region_match") or "")
    ]
    if misses:
        bits = "; ".join(
            (
                f"Search {p['region']} is not in the Search price table, so that project is priced as {str(p.get('region_match')).split('(')[0].strip()}"
                if sol == "search"
                else f"{sol.title()} {p['region']} is not on the estimator region list, so that project is priced as {str(p.get('region_match')).split('(')[0].strip()}"
            )
            for sol, p in misses
        )
        note += " " + bits + "."
    if store_split:
        note += " Where a region had no read-back of its own, that solution keeps the consolidated total and every region is a stored-GB share of it."
    notes.append(note)
    return True


def _search_eis(acc: dict, months: float) -> dict | None:
    """Search ML that moves to EIS: billed tokens, plus ML-node ECU on deployments that billed jina."""
    solution = {d.get("name"): d.get("solution") for d in ((acc.get("solutions") or {}).get("by_deployment") or [])}
    region = {
        d.get("name"): d.get("region")
        for d in ((acc.get("cloud") or {}).get("by_deployment") or [])
        if d.get("region")
    }
    rows = []
    for d in (acc.get("ai_ml") or {}).get("by_deployment") or []:
        if solution.get(d.get("name")) != "search":
            continue
        ml = float(d.get("ml_node_ecu") or 0)
        eis = float(d.get("eis_inference_ecu") or 0)
        models = list(d.get("models") or [])
        if ml <= 0 and eis <= 0 and not d.get("elser") and not models:
            continue
        rows.append({
            "name": d.get("name"),
            "region": region.get(d.get("name")) or "unknown region",
            "ml": ml,
            "eis": eis,
            "models": models,
            "elser": bool(d.get("elser")),
            "jina": any("jina" in str(m).lower() for m in models),
        })
    if not rows:
        return None
    by_region: dict[str, float] = {}
    for r in rows:
        moved = r["ml"] if r["jina"] else 0.0
        by_region[r["region"]] = by_region.get(r["region"], 0.0) + (r["eis"] + moved) / months
    models = sorted({m for r in rows for m in r["models"] if m})
    if any(r["elser"] for r in rows) and "ELSER" not in models:
        models.append("ELSER")
    labels = {"rainbow-sprinkles": "Elastic Managed LLM"}
    kind_label = {
        "chat_input": "Input tokens",
        "chat_output": "Output tokens",
        "embed_dense": "Embedding tokens",
        "embed_sparse": "Sparse embedding tokens",
    }
    # Tokens move with the deployment that billed them. An org-wide SKU list
    # would put Observability and Security chat onto the Search estimate.
    meter = (acc.get("ai_ml") or {}).get("token_meter")
    if isinstance(meter, dict):
        token_source = meter.get("search_rows") or []
    else:
        token_source = (acc.get("ai_ml") or {}).get("inference_skus") or []
    workloads = []
    for sku in token_source:
        ecu = float(sku.get("ecu") or 0)
        qty = sku.get("million_tokens")
        if qty is None:
            qty = sku.get("qty")
        if ecu <= 0 and not qty:
            continue
        if sku.get("model_label"):
            model_name = sku["model_label"]
        elif str(sku.get("kind") or "").startswith("chat"):
            model_name = "Chat"
        else:
            model_name = sku.get("label") or labels.get(sku.get("model"), sku.get("model") or "EIS")
        workloads.append({
            "model": model_name,
            "kind": sku.get("kind_label") or kind_label.get(sku.get("kind"), sku.get("kind") or "Tokens"),
            "million_tokens": round(float(qty or 0), 4),
            "ecu": round(ecu, 2),
            "monthly": round(ecu / months, 2),
        })
    moved_rows = [r for r in rows if r["jina"] and r["ml"] > 0]
    moved_rows.sort(key=lambda r: -r["ml"])
    for r in moved_rows:
        workloads.append({
            "model": r["name"],
            "kind": f"ML nodes · {r['region']}",
            "million_tokens": None,
            "ecu": round(r["ml"], 2),
            "monthly": round(r["ml"] / months, 2),
        })
    moved_ecu = sum(r["ml"] for r in moved_rows)
    other_ml = sum(r["ml"] for r in rows if not r["jina"])
    for sku in token_source:
        if sku.get("model") and sku.get("model_label"):
            labels[sku["model"]] = sku["model_label"]
    for d in (acc.get("ai_ml") or {}).get("by_deployment") or []:
        for mid, lab in zip(d.get("models") or [], d.get("model_labels") or []):
            labels.setdefault(mid, lab)
    return {
        "monthly": round(sum(by_region.values()), 2),
        "by_region": {k: round(v, 2) for k, v in by_region.items()},
        "ml_node_ecu_mo": round(sum(r["ml"] for r in rows) / months, 2),
        "jina_ml_ecu": round(moved_ecu, 2),
        "jina_ml_ecu_mo": round(moved_ecu / months, 2),
        "other_ml_ecu": round(other_ml, 2),
        "models": [labels.get(m, m) for m in models],
        "deployments": len(rows),
        "jina_deployments": len(moved_rows),
        "workloads": workloads,
    }


def _apply_search_eis(acc: dict, by_solution: dict, months: float, notes: list[str]) -> None:
    eis = _search_eis(acc, months)
    blk = by_solution.get("search") or {}
    if not eis or "quotable" not in str(blk.get("pricing") or ""):
        return
    base = sum(float(blk.get(k) or 0) for k in ("search_mo", "ingest_mo", "retention_mo"))
    support = float(blk.get("support_mo") or 0)
    rate = (support / base) if base else 0.0
    usage = float(eis["monthly"] or 0)
    blk["eis_mo"] = usage
    blk["eis_models"] = eis["models"]
    blk["ml_node_ecu_mo"] = eis["ml_node_ecu_mo"]
    blk["jina_ml_ecu"] = eis.get("jina_ml_ecu")
    blk["jina_ml_ecu_mo"] = eis.get("jina_ml_ecu_mo")
    blk["other_ml_ecu"] = eis.get("other_ml_ecu")
    blk["eis_workloads"] = eis["workloads"]
    blk["eis_deployments"] = eis["deployments"]
    blk["jina_deployments"] = eis.get("jina_deployments")
    extra_support = round(usage * rate, 2)
    blk["eis_with_support_mo"] = round(usage * (1 + rate), 2)
    if blk.get("by_region"):
        for r in blk["by_region"]:
            part = float(eis["by_region"].get(r["region"]) or 0)
            r["eis_mo"] = round(part, 2)
            if r.get("monthly") is not None:
                r["monthly"] = round(float(r["monthly"]) + part * (1 + rate), 2)
        if blk.get("regional_mode") == "store_split":
            blk["total_mo"] = round(float(blk.get("total_mo") or 0) + usage * (1 + rate), 2)
        else:
            blk["total_mo"] = round(sum(float(r.get("monthly") or 0) for r in blk["by_region"]), 2)
    else:
        blk["total_mo"] = round(float(blk.get("total_mo") or 0) + usage * (1 + rate), 2)
    blk["total_yr"] = round(float(blk["total_mo"]) * 12, 2)
    blk["support_mo"] = round(support + extra_support, 2)
    for row in blk.get("regional_profiles") or []:
        if isinstance(row.get("Total $/month"), (int, float)):
            row["Total $/month"] = round(float(row["Total $/month"]) + usage * (1 + rate), 2)
        if isinstance(row.get("Support $/month"), (int, float)):
            row["Support $/month"] = round(float(row["Support $/month"]) + extra_support, 2)
    notes.append(
        f"Deployments that billed jina embeddings also ran {eis['jina_ml_ecu']:,.0f} ECU of ML nodes over the year "
        f"({eis['jina_ml_ecu_mo']:,.0f} ECU/mo). That capacity is in the EIS line at list, with the billed token usage. "
        f"EIS is ${usage:,.2f}/mo before support. "
        f"Another {eis['other_ml_ecu']:,.0f} ECU of ML nodes sit on Search deployments with no jina SKU, and are not in this line."
    )


def _align_primary_cost_rec(acc: dict, savings: dict, by_solution: dict) -> None:
    """Point the move-to-serverless recommendation at the list comparison, not the org-report ballpark."""
    recs = ((acc.get("analysis") or {}).get("cost") or [])
    if not recs or savings.get("list_mo") is None:
        return
    rec = recs[0]
    if "serverless" not in str(rec.get("title") or "").lower() and "ballpark" not in str(rec.get("evidence") or ""):
        return
    parts = []
    for sol in ("search", "observability", "security"):
        blk = by_solution.get(sol) or {}
        if "quotable" in str(blk.get("pricing") or "") and blk.get("total_mo") is not None:
            parts.append(f"{sol.title()} ${float(blk['total_mo']):,.0f}/mo")
    if not parts:
        return
    if savings.get("ech_contract_mo") is not None:
        rec["evidence"] = (
            f"ECH {savings['ech_list_mo']:,.0f} ECU/mo at ARR ÷ (commit × 365) is ${savings['ech_contract_mo']:,.0f}/mo. "
            "Serverless is "
            + " + ".join(parts)
            + f" = ${savings['serverless_mo']:,.0f}/mo."
        )
    else:
        rec["evidence"] = (
            f"ECH {savings['ech_list_mo']:,.0f} ECU/mo is ${savings['ech_list_mo']:,.0f} at list ($1 per ECU). "
            "Serverless is "
            + " + ".join(parts)
            + f" = ${savings['serverless_mo']:,.0f}/mo."
        )
    eis_mo = (by_solution.get("search") or {}).get("eis_mo")
    if eis_mo:
        rec["evidence"] += f" That Search total includes EIS at ${float(eis_mo):,.2f}/mo: billed tokens plus the ML-node ECU on deployments that billed jina."
    if savings.get("cost_mo") is not None:
        rec["impact"] = f"${savings['cost_mo']:,.0f}/mo below the contract ECH cost (${savings['cost_yr']:,.0f}/yr)"
    else:
        rec["impact"] = f"${savings['list_mo']:,.0f}/mo at list (${savings['list_yr']:,.0f}/yr)"
    rec["title"] = str(rec.get("title") or "").replace(" (ES3)", "")
    rec["action"] = "Quote the Search Serverless and Observability Serverless estimators. Migrate the dominant slice first. Keep latency-sensitive search on ECH if a project cannot meet it."


def _has_workload(blk: dict) -> bool:
    return bool((blk.get("stored_gb") or 0) or (blk.get("daily_raw_gb") or 0) or (blk.get("deployments") or 0))


def _integrity(acc: dict, sz: dict, by_solution: dict, quotes: list[dict], headline: float) -> dict:
    """Six integrity checks. The mean skips a check that does not apply."""
    estimators = sz.get("estimators") or {}
    o11y = estimators.get("observability") or {}
    security = estimators.get("security") or {}
    o11y_blk = by_solution.get("observability") or {}
    sec_blk = by_solution.get("security") or {}
    coverage = (sz.get("diagnostics") or {}) if sz.get("ingest_source") == "diagnostics" else {}
    unmeasured = sum(float(row.get("bulk_gb_day") or 0) for row in (coverage.get("retention_unmeasured") or []))
    return integrity_score({
        "observability": {
            "has_workload": _has_workload(o11y_blk),
            "sheet_read": bool(o11y.get("readback")),
            "written_bulk": float(o11y_blk.get("daily_raw_gb") or 0),
            "sheet_raw": (o11y.get("readback") or {}).get("total_raw_gb_day"),
        },
        "security": {
            "has_workload": _has_workload(sec_blk),
            "sheet_read": bool(security.get("readback")),
            "e73": (security.get("readback") or {}).get("cps_addon_monthly"),
        },
        "coverage": None if not coverage else {
            "matched": coverage.get("matched"),
            "active": coverage.get("active_deployments"),
            "fallback": coverage.get("k360_fallback"),
            "fallback_names": coverage.get("fallback_names") or [],
            "bulk_gb_day": coverage.get("bulk_gb_day"),
            "unmeasured_bulk": unmeasured,
        },
        "solutions": [
            {
                "has_workload": _has_workload(blk),
                "direct": "quotable" in str(blk.get("pricing") or "") and blk.get("regional_mode") != "store_split",
            }
            for blk in by_solution.values()
        ],
        "headline_monthly": headline,
        "lines": quotes,
    })


# --------------------------------------------------------------------------- main build
def build_consolidation(acc: dict, infra: dict | None) -> dict:
    sz = acc.get("sizing") or {}
    e = acc.get("ecu") or {}
    lookback_days = float(e.get("lookback_days") or 365)
    months = max(lookback_days / DAYS_PER_MONTH, 1.0)
    ecu_total = float(e.get("total_lookback") or 0)
    ech_mo = float(e.get("monthly_avg_complete_months") or e.get("monthly_avg_all") or (ecu_total / months))
    stored_gb = float((acc.get("storage") or {}).get("data_stored_gb") or 0)
    con = sz.get("consolidated") or {}
    workload = acc.get("workload") or {}
    notes: list[str] = []

    # ---- ECH composition (per-deployment tiers from infra)
    deps_infra = (infra or {}).get("by_deployment") or []
    fixed = ml = data_tiers = 0.0
    tiers_by_name: dict[str, dict] = {}
    for d in deps_infra:
        t = d.get("tiers") or {}
        tiers_by_name[d["name"]] = d
        fixed += sum(v for k, v in t.items() if k in FIXED_TIERS)
        ml += float(t.get("ml") or 0)
        data_tiers += sum(v for k, v in t.items() if str(k).startswith("data"))
    non_cap = float((acc.get("cloud") or {}).get("non_capacity_ecu") or 0)
    if not deps_infra:
        notes.append("infra.json missing — ECH composition (fixed overhead / ML / data tiers) unavailable")
    composition = {
        "data_tiers_mo": round(data_tiers / months, 2),
        "fixed_overhead_mo": round(fixed / months, 2),
        "ml_nodes_mo": round(ml / months, 2),
        "non_capacity_mo": round(non_cap / months, 2),
        "fixed_overhead_pct": round(100 * fixed / ecu_total, 1) if ecu_total else None,
        "ml_pct": round(100 * ml / ecu_total, 1) if ecu_total else None,
        "non_capacity_pct": round(100 * non_cap / ecu_total, 1) if ecu_total else None,
    }

    # ---- serverless: one project per solution (what the estimators price)
    by_solution: dict[str, dict] = {}
    svls_total_mo = 0.0
    for sol in ("observability", "security", "search"):
        blk = con.get(sol) or {}
        gb = float(blk.get("stored_gb") or 0)
        written_bulk = float(blk.get("daily_raw_gb") or 0)
        ret = float(blk.get("retention_months") or 3.0)
        p = _price_slice(sol, gb, ret, workload)
        p.update({"stored_gb": round(gb, 1), "deployments": blk.get("deployments"), "ech_ecu_mo": blk.get("ecu_monthly_avg")})
        if sz.get("ingest_source") == "diagnostics":
            p["daily_raw_gb"] = round(written_bulk, 3)
        by_solution[sol] = p
    quoted = _apply_search_estimator(by_solution, sz)
    for sol in ("observability", "security"):
        est = ((sz.get("estimators") or {}).get(sol) or {})
        blk = by_solution[sol]
        readback = est.get("readback") or {}
        if sol == "observability" and readback and not est.get("readback_rejected"):
            gate = est.get("g26_gate") or g26_gate(float(blk.get("daily_raw_gb") or 0), readback.get("total_raw_gb_day"))
            if not gate.get("accepted"):
                blk["pricing"] = "rejected — G26 differs from the written bulk"
                notes.append(
                    f"Observability sheet G26 is {gate.get('sheet_raw_gb_day')} GB/day against {gate.get('written_bulk')} GB/day written. D32 was not quoted."
                )
                continue
        if sol == "security" and readback and e73_blocks_quote(readback.get("cps_addon_monthly")):
            addon = _money(readback.get("cps_addon_monthly"))
            if addon is None:
                blk["pricing"] = "rejected — E73 was blank"
                notes.append("Security E73 was blank or could not be read. E74 was not quoted.")
            else:
                blk["pricing"] = "rejected — E73 add-on is not zero"
                notes.append(
                    f"Security month-12 total includes {addon} from template cross-project search (sheet E73). E74 was not quoted."
                )
            continue
        if est.get("readback_rejected"):
            blk["pricing"] = "rejected — " + str(est.get("readback_rejected"))
            notes.append(f"{sol.title()} read-back was not quoted: {est.get('readback_rejected')}")
            continue
        quoted = _apply_sheet_readback(by_solution, sz, sol) or quoted
    for sol, blk in by_solution.items():
        _omit_unquoted(blk, notes, sol)
    svls_total_mo = sum(float(p.get("total_mo") or 0) for p in by_solution.values())
    region_groups = _deployment_groups(acc, infra, months)
    _replicate_by_region(acc, by_solution, region_groups, notes)
    _apply_search_eis(acc, by_solution, months, notes)
    svls_total_mo = sum(float(p.get("total_mo") or 0) for p in by_solution.values())
    if quoted:
        notes.append(
            "Every serverless dollar comes from a named estimator: Search Serverless on-demand, or the Observability or Security Serverless estimator. "
            "EIS on a Search project is the billed token usage plus the ML-node ECU on deployments that billed jina, both at list. "
            "ECH dollars, when ARR and commit exist, are ARR ÷ (commit ECU/day × 365), applied to ECH only. Serverless is not discounted."
        )
    if sz.get("ingest_source") == "diagnostics":
        notes.extend(sz.get("notes") or [])
        notes.insert(0, (
            "Ingest is bulk GB/day from the Elasticsearch diagnostic bundles. K360 does not provide a raw ingest total. "
            "Its backup GB/day is stored GB divided by an assumed retention (default 3 months), which prices stored-byte turnover "
            "and can sit far below the bulk payload the cluster accepted. An active deployment with no bundle adds no ingest. "
            "A deployment with no bill in the latest month is left out, and its last snapshot is not carried in. "
            "Pass --k360-ingest to also price the whole fleet that way."
        ))
    else:
        notes.insert(0, (
            "K360 backup. K360 does not provide a raw ingest total, so GB/day is stored GB divided by an assumed retention "
            "(default 3 months). That prices stored-byte turnover and can sit far below the bulk payload the cluster accepted. "
            "The default quote uses diagnostic bundles when they are available."
        ))
    dominant = max(by_solution, key=lambda k: float(by_solution[k].get("stored_gb") or 0)) if by_solution else "observability"
    dom = by_solution.get(dominant) or {}
    metered = float(dom.get("daily_metered_gb") or dom.get("sheet_metered_gb_day") or 0)
    retained = float(dom.get("retained_gb") or 0)
    ingest_mo = dom.get("ingest_mo")
    retain_mo = dom.get("retention_mo")
    serverless = {
        "by_solution": by_solution,
        "dominant_solution": dominant,
        "total_mo": round(svls_total_mo, 2),
        "total_yr": round(svls_total_mo * 12, 2),
        "eff_ingest_usd_per_gb": round(float(ingest_mo) / (metered * DAYS_PER_MONTH), 4) if ingest_mo is not None and metered else None,
        "eff_retain_usd_per_gb_mo": round(float(retain_mo) / retained, 4) if retain_mo is not None and retained else None,
        "eff_usd_per_stored_gb_mo": round(svls_total_mo / stored_gb, 4) if stored_gb else None,
        "ech_ecu_per_stored_gb_mo": round(ech_mo / stored_gb, 4) if stored_gb else None,
        "pricing_source": PRICING_SOURCE,
    }

    # ---- implied contract rate
    org_meta = (acc.get("meta") or {}).get("org") or {}
    arr = float(org_meta.get("ARR") or 0)
    commit = float((e.get("contract") or {}).get("commit_pace_ecu_per_day") or 0)
    rate = (arr / (commit * 365)) if arr > 0 and commit > 0 else None
    contract_rate = {
        "arr": round(arr, 2) if arr else None,
        "commit_ecu_per_day": commit or None,
        "implied_usd_per_ecu": round(rate, 3) if rate else None,
        "note": (
            "ARR ÷ (commit ECU/day × 365), applied unrounded to ECH ECU. Shown rounded. Serverless stays at estimator list. Verify with the AE."
            if rate else "no ARR or commit pace in K360 — list view only"
        ),
    }

    # ---- savings (list only: $1/ECU minus estimator read-backs)
    if quoted and ech_mo:
        save_list_mo = ech_mo - svls_total_mo
        savings = {
            "ech_list_mo": round(ech_mo, 2),
            "serverless_mo": round(svls_total_mo, 2),
            "list_mo": round(save_list_mo, 2),
            "list_yr": round(save_list_mo * 12, 2),
            "list_pct": round(100 * save_list_mo / ech_mo, 1),
        }
    else:
        save_list_mo = 0.0
        savings = {"ech_list_mo": round(ech_mo, 2), "serverless_mo": None, "list_mo": None, "list_yr": None, "list_pct": None}
    if rate:
        ech_cost = ech_mo * rate
        savings["ech_contract_mo"] = round(ech_cost, 2)
        savings["below_list_mo"] = round(ech_mo - ech_cost, 2)
        savings["below_list_yr"] = round((ech_mo - ech_cost) * 12, 2)
        savings["below_list_pct"] = round(100 * (ech_mo - ech_cost) / ech_mo, 1) if ech_mo else None
        if savings.get("serverless_mo") is not None and ech_cost:
            gap = ech_cost - float(savings["serverless_mo"])
            savings["cost_mo"] = round(gap, 2)
            savings["cost_yr"] = round(gap * 12, 2)
            savings["cost_pct"] = round(100 * gap / ech_cost, 1)

    # ---- per-deployment standalone pricing + fragmentation
    sol_deps = (acc.get("solutions") or {}).get("by_deployment") or []
    per: list[dict] = []
    for d in sol_deps:
        if d.get("platform") == "serverless":
            continue
        gb = float(d.get("gb") or 0)
        sol = d.get("solution") or "search"
        ecu_mo = float(d.get("ecu") or 0) / months
        ti = tiers_by_name.get(d["name"], {})
        t = ti.get("tiers") or {}
        fixed_mo = round(sum(v for k, v in t.items() if k in FIXED_TIERS) / months, 2) if t else None
        per.append({
            "name": d["name"], "solution": sol, "stored_gb": round(gb, 1),
            "ech_ecu_mo": round(ecu_mo, 2), "ech_ecu_lookback": round(float(d.get("ecu") or 0), 2),
            "fixed_overhead_ecu_mo": fixed_mo,
            "overhead_pct": round(100 * fixed_mo / ecu_mo, 1) if fixed_mo is not None and ecu_mo > 0 else None,
            "ml_ecu_mo": round(float(t.get("ml") or 0) / months, 2) if t else None,
            "zones": ti.get("zones"), "gb_known": gb > 0,
            "ech_cost_mo": round(ecu_mo * rate, 2) if rate else None,
        })
    per.sort(key=lambda r: -r["ech_ecu_mo"])
    n_with_gb = sum(1 for r in per if r["gb_known"])
    ecu_by_name = {r["name"]: r["ech_ecu_mo"] for r in per}
    gaps = []
    for g in (acc.get("storage") or {}).get("size_gaps") or []:
        if g.get("name") in ecu_by_name:
            gaps.append({**g, "ech_ecu_mo": ecu_by_name[g["name"]]})
    gaps.sort(key=lambda g: -(g.get("ech_ecu_mo") or 0))
    gap_by_name = {g["name"]: g for g in gaps}
    for r in per:
        g = gap_by_name.get(r["name"])
        r["size_now_on"] = (g or {}).get("size_now_on")
    n_moved = sum(1 for g in gaps if g.get("size_now_on"))
    fragmentation = {
        "n_deployments": len(per),
        "n_with_gb": n_with_gb,
        "n_size_moved": n_moved,
        "n_size_never": sum(1 for g in gaps if not g.get("last_size_day")),
        "size_gaps": gaps,
        "stored_gb": round(sum(r["stored_gb"] for r in per), 1),
        "ech_ecu_mo": round(sum(r["ech_ecu_mo"] for r in per), 2),
        "fixed_overhead_ecu_mo": round(sum((r["fixed_overhead_ecu_mo"] or 0) for r in per), 2),
        "note": "One estimator read-back per solution. Deployments are ECU, GB, and overhead share, not a second price.",
    }
    if n_with_gb < len(per):
        if gaps:
            notes.append(f"{len(per) - n_with_gb} deployments have no size on the snapshot day. {n_moved} of them now report size under a sibling name that is already in the stored-GB total. The rest have no current size. All stay in the ECU totals.")
        else:
            notes.append(f"{len(per) - n_with_gb} deployment(s) have no storage row. They stay in the ECU totals and out of the stored-GB total. They are not given a serverless price.")

    # ---- long tail (< 1 % of spend each)
    thr = ecu_total * TAIL_SHARE_PCT / 100
    tail = [r for r in per if r["ech_ecu_lookback"] < thr]
    tail_gb = sum(r["stored_gb"] for r in tail)
    tail_ecu_mo = sum(r["ech_ecu_mo"] for r in tail)
    tail_block = {
        "n": len(tail),
        "stored_gb": round(tail_gb, 1),
        "ech_ecu_mo": round(tail_ecu_mo, 2),
        "ech_share_pct": round(100 * sum(r["ech_ecu_lookback"] for r in tail) / ecu_total, 1) if ecu_total else None,
        "fixed_overhead_ecu_mo": round(sum(r["fixed_overhead_ecu_mo"] or 0 for r in tail), 2),
        "ech_cost_mo": round(tail_ecu_mo * rate, 2) if rate else None,
        "ech_consolidation_saving_mo": round(0.25 * tail_ecu_mo, 2),
        "data_share_pct": round(100 * tail_gb / stored_gb, 1) if stored_gb else None,
        "note": "ECH consolidation assumes 25 % overhead removal (cost-rec rule). The tail is not given its own serverless price.",
    }

    # ---- fixed-overhead share by size bucket
    buckets_def = [(0, 1_000, "< 1k ECU"), (1_000, 5_000, "1k – 5k"), (5_000, thr, f"5k – {thr / 1000:,.1f}k (< {TAIL_SHARE_PCT:g} %)"), (thr, 50_000, f"{thr / 1000:,.1f}k – 50k"), (50_000, float("inf"), "> 50k")]
    buckets = []
    for lo, hi, label in buckets_def:
        if hi <= lo:
            continue
        b = [r for r in per if lo <= r["ech_ecu_lookback"] < hi]
        if not b:
            continue
        ecu_b = sum(r["ech_ecu_lookback"] for r in b)
        fx_b = sum((r["fixed_overhead_ecu_mo"] or 0) for r in b) * months
        buckets.append({"bucket": label, "deployments": len(b), "ecu_lookback": round(ecu_b, 2), "fixed_overhead_pct": round(100 * fx_b / ecu_b, 1) if ecu_b else None})
    highest_overhead = sorted([r for r in per if r["fixed_overhead_ecu_mo"] and r["ech_ecu_mo"] > 0], key=lambda r: -(r["fixed_overhead_ecu_mo"] / r["ech_ecu_mo"]))[:5]

    # ---- sensitivity: volume only. The quote is not re-run.
    dom_gb = float(dom.get("stored_gb") or 0)
    used_ret = float((con.get(dominant) or {}).get("retention_months") or 3.0)
    retention_rows = []
    for r in RETENTION_SCENARIOS:
        raw = (dom_gb / (float(r) * DAYS_PER_MONTH)) if dom_gb > 0 else None
        retention_rows.append({
            "retention_months": r,
            "used": abs(r - used_ret) < 1e-9,
            "raw_gb_day": round(raw, 2) if raw is not None else None,
            "stored_gb": round(dom_gb, 1),
        })

    fleet_rows = []
    scen = [("Lookback-average fleet (estimator basis)", 1.0)]
    low = e.get("monthly_low") or {}
    if low.get("ecu") and ech_mo:
        scen.append((f"Quietest month {low.get('month')}", float(low["ecu"]) / ech_mo))
    if e.get("last_30d_daily_avg") and ech_mo:
        scen.append(("Last 30 days run-rate", float(e["last_30d_daily_avg"]) * DAYS_PER_MONTH / ech_mo))
    eis_full = float((by_solution.get("search") or {}).get("eis_with_support_mo") or 0)
    eis_usage = float((by_solution.get("search") or {}).get("eis_mo") or 0)
    for label, k in scen:
        base = (svls_total_mo - eis_full) if svls_total_mo else 0.0
        svl_k = round(base * k + eis_full, 2) if svls_total_mo else None
        fleet_rows.append({
            "scenario": label,
            "scale": round(k, 3),
            "stored_gb": round(stored_gb * k, 1),
            "ech_ecu_mo": round(ech_mo * k, 2),
            "ech_cost_mo": round(ech_mo * k * rate, 2) if rate else None,
            "serverless_mo": svl_k,
            "eis_mo": round(eis_usage, 2) if eis_usage else None,
            "save_vs_list_mo": round(ech_mo * k - svl_k, 2) if svl_k is not None else None,
            "save_vs_contract_mo": round(ech_mo * k * rate - svl_k, 2) if (svl_k is not None and rate) else None,
        })

    # ---- architecture: one serverless project per solution per region
    priced = {}
    for sol, blk in by_solution.items():
        for r in blk.get("by_region") or []:
            priced[(sol, r["region"])] = r
    arch_projects = []
    for g in region_groups:
        hit = priced.get((g["solution"], g["region"]))
        blk = by_solution.get(g["solution"]) or {}
        if hit:
            monthly = hit.get("monthly")
        elif "quotable" in str(blk.get("pricing") or "") and len({x["region"] for x in region_groups if x["solution"] == g["solution"]}) == 1:
            monthly = blk.get("total_mo")
        else:
            monthly = None
        arch_projects.append({
            "solution": g["solution"], "region": g["region"], "stored_gb": g["stored_gb"],
            "serverless_mo": monthly,
            "pricing": (hit or {}).get("pricing"),
            "region_match": (hit or {}).get("region_match"),
            "url": (hit or {}).get("url"),
        })
    architecture = {
        "groups": region_groups,
        "projects": arch_projects,
        "regions": sorted({g["region"] for g in region_groups}),
        "per_region": any(len({g["region"] for g in region_groups if g["solution"] == sol}) > 1 for sol in ("search", "observability", "security")),
    }
    age = (acc.get("storage") or {}).get("telemetry_age_days")
    if age and age > 60:
        notes.append(f"storage telemetry is {age} days old — stored GB reflects the fleet at that time. Fleet rows scale ECU and GB. The estimator was not re-run.")

    quotes = []
    for sol in ("search", "observability", "security"):
        blk = by_solution.get(sol) or {}
        if "quotable" not in str(blk.get("pricing") or ""):
            continue
        est = (sz.get("estimators") or {}).get(sol) or {}
        rb = est.get("readback") or {}
        label = "Search Serverless, on-demand" if sol == "search" else f"{sol.title()} Serverless"
        regions = blk.get("by_region") or []
        if regions:
            for r in regions:
                monthly = r.get("monthly")
                if r.get("annual_year1") is not None:
                    annual = r.get("annual_year1")
                    steady = r.get("annual_steady_state")
                elif monthly is not None:
                    annual = round(float(monthly) * 12, 2)
                    steady = annual
                else:
                    annual = None
                    steady = None
                quotes.append({
                    "solution": sol,
                    "region": r.get("region"),
                    "source": f"{label}, {r.get('region')}",
                    "monthly": monthly,
                    "monthly_unrounded": r.get("monthly_unrounded") if r.get("monthly_unrounded") is not None else r.get("own_monthly"),
                    "annual_year1": annual,
                    "annual_steady_state": steady,
                    "retention_months": r.get("retention_months") if r.get("retention_months") is not None else blk.get("retention_months"),
                    "url": r.get("url") or est.get("url"),
                })
        else:
            quotes.append({
                "solution": sol,
                "source": label,
                "monthly": blk.get("total_mo"),
                "monthly_unrounded": _money(rb.get("monthly_steady_state")),
                "annual_year1": _money(rb.get("annual_year1")),
                "annual_steady_state": _money(rb.get("annual_steady_state")),
                "retention_months": blk.get("retention_months"),
                "url": est.get("url"),
            })
    recon = {"kind": "readbacks", "quotes": quotes, "total_mo": round(svls_total_mo, 2), "quotable": "estimator read-backs"} if quotes else None
    integrity = _integrity(acc, sz, by_solution, quotes, svls_total_mo)

    callouts: list[dict] = []
    if per:
        top = per[0]
        oh = f"{top['overhead_pct']}% fixed overhead" if top.get("overhead_pct") is not None else "fixed overhead not available"
        gb_bit = f"{top['stored_gb']:,.0f} GB" if top["gb_known"] else (f"its size now reports under {top['size_now_on']}" if top.get("size_now_on") else "no current size")
        callouts.append({"tone": "info", "title": f"Largest deployment: {top['name']}", "detail": f"{top['ech_ecu_mo']:,.0f} ECU/mo, {gb_bit}, {oh}. Deployments are not priced alone. The estimate is one project per solution in each region."})
    if composition.get("fixed_overhead_mo"):
        callouts.append({"tone": "success", "title": f"Fixed overhead is {composition['fixed_overhead_mo']:,.0f} ECU/mo", "detail": f"{composition.get('fixed_overhead_pct')}% of lookback ECU is master, Kibana, coordinating, and the other fixed roles. A serverless project does not bill those roles."})
    if tail_block["n"] >= 8:
        callouts.append({"tone": "info", "title": f"Long tail: {tail_block['n']} deployments, {tail_block['ech_share_pct']}% of spend, {tail_block.get('data_share_pct')}% of data", "detail": f"{tail_block['ech_ecu_mo']:,.0f} ECU/mo holding {tail_block['stored_gb']:,.0f} GB. Merging them on ECH, at the 25% overhead-removal assumption, recovers about {tail_block['ech_consolidation_saving_mo']:,.0f} ECU/mo."})

    caveats = [
        "Serverless $ are usage-based: a burst month costs more than the steady-state figure",
        "Security-adjacent sources tagged observability in K360 belong in a Security project (own ladder) if they need SIEM detections",
        "Excludes migration effort, dual-running during cutover, and any ECH kept for latency-sensitive search",
        "Remaining commit balance must be consumed or renegotiated regardless of platform",
    ]
    sec = by_solution.get("security") or {}
    if sec.get("stored_gb") and "quotable" not in str(sec.get("pricing") or ""):
        caveats.append("Security has stored GB and no sheet read-back, so it is omitted from the serverless total.")
    _align_primary_cost_rec(acc, savings, by_solution)

    return {
        "triggered": bool(sz.get("triggered")),
        "source": sz.get("ingest_source") or "k360",
        "basis": {"ech_monthly_ecu": round(ech_mo, 2), "lookback_days": lookback_days, "stored_gb": round(stored_gb, 1), "deployments": len(per), "retention_months": {k: (v or {}).get("retention_months") for k, v in con.items()}},
        "ech_composition": composition,
        "serverless": serverless,
        "contract_rate": contract_rate,
        "savings": savings,
        "ladder": [] if quoted else (_ladder_walk(metered) if metered else []),
        "metered_rates": observability_metered_rates(((acc.get("solutions") or {}).get("by_deployment") or [])) if (sz.get("ingest_source") == "diagnostics") else [],
        "deployment_plan": _deployment_bars(sz, by_solution, notes),
        "search_power": _search_power(sz, by_solution, acc),
        "fragmentation": fragmentation,
        "architecture": architecture,
        "by_deployment": per[:TOP_N],
        "tail": tail_block,
        "overhead_buckets": buckets,
        "highest_overhead_deployments": [{"name": r["name"], "fixed_overhead_pct": round(100 * r["fixed_overhead_ecu_mo"] / r["ech_ecu_mo"], 1), "ech_ecu_mo": r["ech_ecu_mo"]} for r in highest_overhead],
        "sensitivity": {"retention": retention_rows, "fleet": fleet_rows},
        "reconciliation": recon,
        "integrity": integrity,
        "callouts": callouts,
        "caveats": caveats,
        "notes": notes,
    }


def attach(acc_path: Path, infra_path: Path | None) -> dict:
    acc = json.loads(acc_path.read_text())
    infra = json.loads(infra_path.read_text()) if infra_path and infra_path.is_file() else None
    acc["consolidation"] = build_consolidation(acc, infra)
    from privacy import seal_text
    seal_text(acc_path, json.dumps(acc, indent=2))
    return acc["consolidation"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-json", required=True)
    ap.add_argument("--infra-json", default=None, help="default: <key>_infra.json beside the account JSON")
    args = ap.parse_args()
    acc_path = Path(args.account_json)
    infra_path = Path(args.infra_json) if args.infra_json else acc_path.with_name(acc_path.name.replace("_account.json", "_infra.json"))
    c = attach(acc_path, infra_path)
    s = c["savings"]
    if s.get("serverless_mo") is None:
        print(f"consolidation: ECH {s['ech_list_mo']:,.0f} ECU/mo · no estimator read-back{bill}")
    elif s.get("ech_contract_mo") is not None:
        print(f"consolidation: ECH at contract ${s['ech_contract_mo']:,.0f}/mo → serverless ${s['serverless_mo']:,.0f}/mo · ${s['cost_mo']:,.0f}/mo below the contract rate")
    else:
        print(f"consolidation: ECH list ${s['ech_list_mo']:,.0f}/mo → serverless ${s['serverless_mo']:,.0f}/mo · ${s['list_mo']:,.0f}/mo below list ({s['list_pct']}%)")


if __name__ == "__main__":
    main()
