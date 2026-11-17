#!/usr/bin/env python3
"""Serverless consolidation-savings analysis (runs under the same ECH > 50 % trigger).

Explains *why* moving the ECH footprint onto a few serverless projects saves money.
Every serverless dollar is an estimator read-back (es3-estimator performant for
Search, the sheet for Observability and Security). Every ECH dollar is the K360
billing `cost` field, summed as billed: that field is the ECU total, and 1 ECU is
$1. No ARR ÷ commit rate is applied to it. Deployments, the tail, and
retention / fleet scenarios are ECU, GB, and overhead share — not a second price.
Pure function of `account.json` + `infra.json`; no network.

  python consolidation.py --account-json <key>_account.json --infra-json <key>_infra.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

from quote_checks import (
    BILLING_WINDOW_MIN_SHARE,
    e73_blocks_quote, echo_gate, ingest_echo, integrity_score, money as _money,
    observability_echo_gate, region_match_how,
)
from skill_paths import find_skill

DAYS_PER_MONTH = 30.416
MAX_LOOKBACK_DAYS = 365
_INGEST_KEYS = (
    "daily_raw_gb", "logs_gb_day", "metrics_gb_day", "siem_gb_day",
    "bulk_gb_day", "compressioncalc_accounted_for",
)
TAIL_SHARE_PCT = 1.0          # "small" deployment = < 1 % of lookback ECU (same as cost rec)
TOP_N = 15
RETENTION_SCENARIOS = (1, 3, 6, 12)
FIXED_TIERS = {"master", "kibana", "coordinating", "integrations server", "enterprise search", "apm"}

ORG_SKILL = find_skill("k360-org-report", Path(__file__).resolve().parent)

def quote_lookback_days(days) -> int:
    """Billing window for the quote. The default is 90 days, three months. A year is the maximum."""
    try:
        window = int(days)
    except (TypeError, ValueError) as exc:
        raise ValueError("lookback days must be a whole number from 1 to 365") from exc
    if not 1 <= window <= MAX_LOOKBACK_DAYS:
        raise ValueError("lookback days must be from 1 to 365")
    return window


def billed_series(ecu: dict) -> tuple[list[str], list[float]]:
    """The org's daily cost over its billing life in the fetch: day labels and totals.

    Days after the last positive total are not billed yet, and days before the
    first one are before the org's first bill. Neither is a billed day. Labels
    are empty when the account has none that line up with the totals.
    """
    days = [str(d)[:10] for d in (ecu.get("daily_days") or [])]
    direct = ecu.get("daily_deployment_costs") or {}
    direct_totals = direct.get("org_daily_totals") or []
    if direct.get("complete") and list(direct.get("days") or []) == days and len(direct_totals) == len(days):
        totals = [float(v or 0) for v in direct_totals]
    else:
        totals = [float(v or 0) for v in (ecu.get("daily_totals") or [])]
    if len(days) != len(totals):
        days = []
    while totals and totals[-1] <= 0:
        totals.pop()
        if days:
            days.pop()
    lead = next((i for i, value in enumerate(totals) if value > 0), 0)
    return (days[lead:] if days else []), totals[lead:]


def _window_daily_avg(ecu: dict, days: int) -> float | None:
    """Daily average over the last `days` billed days, or over all of them when there are fewer."""
    _labels, totals = billed_series(ecu)
    if totals:
        window = totals[-days:]
        return sum(window) / len(window)
    if days <= 30 and ecu.get("last_30d_daily_avg") is not None:
        return float(ecu["last_30d_daily_avg"])
    fetched = int(ecu.get("lookback_days") or 0)
    if ecu.get("daily_avg") is not None and fetched and days >= fetched:
        return float(ecu["daily_avg"])
    return None


def _scale_ingest(row: dict, factor: float) -> None:
    for key in _INGEST_KEYS:
        if row.get(key) is None:
            continue
        try:
            row[key] = round(float(row[key]) * factor, 3)
        except (TypeError, ValueError):
            continue


def apply_quote_lookback(acc: dict, days: int = 90) -> float:
    """Price ECH at the last `days` of billed ECU.

    The window sets the ECH month and `quote_usage_factor`, which scales ECH
    dollars only. Diagnostic bulk stays measured; Search workload rates stay
    telemetry-derived and are accepted only when index telemetry covers the
    exact billed quote window. The K360 backup's stored-GB ingest can take the
    billing pace because it is explicitly an assumption-based scenario.
    """
    window = quote_lookback_days(days)
    ecu = acc.setdefault("ecu", {})
    measured = (acc.get("sizing") or {}).get("ingest_source") == "diagnostics"
    daily = _window_daily_avg(ecu, window)
    complete = ecu.get("monthly_avg_complete_months")
    if complete:
        base_daily = float(complete) / DAYS_PER_MONTH
    elif ecu.get("daily_avg") is not None:
        base_daily = float(ecu["daily_avg"])
    else:
        base_daily = None
    if daily is None:
        factor = 1.0
        monthly = float(complete or 0)
    else:
        monthly = daily * DAYS_PER_MONTH
        factor = (daily / base_daily) if base_daily else 1.0
    ingest_factor = 1.0 if measured else factor
    if not ecu.get("quote_ingest_scaled"):
        if ingest_factor != 1.0:
            for dep in (acc.get("solutions") or {}).get("by_deployment") or []:
                if isinstance(dep, dict):
                    _scale_ingest(dep, ingest_factor)
            for block in ((acc.get("sizing") or {}).get("consolidated") or {}).values():
                if isinstance(block, dict):
                    _scale_ingest(block, ingest_factor)
            diagnostics = (acc.get("sizing") or {}).get("diagnostics") or {}
            if isinstance(diagnostics, dict):
                _scale_ingest(diagnostics, ingest_factor)
        ecu["quote_ingest_scaled"] = True
    # The window that was priced. Fewer billed days than asked for is a shorter window, and no
    # daily series at all is no window: the ECH month is then the complete-month average.
    billed_days = len(billed_series(ecu)[1])
    ecu["quote_lookback_requested"] = window
    ecu["quote_lookback_days"] = None if daily is None else (min(window, billed_days) if billed_days else window)
    ecu["quote_daily_avg"] = None if daily is None else round(daily, 2)
    ecu["quote_monthly_ecu"] = round(monthly, 2)
    ecu["quote_usage_factor"] = round(factor, 6)
    ecu["quote_ingest_factor"] = round(ingest_factor, 6)
    workload = acc.get("workload") or {}
    quote_days = ecu.get("quote_lookback_days")
    telemetry_days = int(workload.get("window_days") or 0)
    telemetry_start = workload.get("window_start")
    telemetry_end = workload.get("window_end")
    labels, _ = billed_series(ecu)
    quote_labels = labels[-int(quote_days):] if quote_days and labels else []
    expected_start = None
    if quote_labels:
        try:
            expected_start = (dt.date.fromisoformat(str(quote_labels[0])) - dt.timedelta(days=1)).isoformat()
        except ValueError:
            expected_start = None
    exact_window = bool(
        quote_days
        and telemetry_days == int(quote_days)
        and expected_start == telemetry_start
        and quote_labels[-1] == telemetry_end
        and float(((workload.get("ingest") or {}).get("docs_per_sec") or 0)) > 0
        and float(((workload.get("ingest") or {}).get("avg_doc_bytes") or 0)) > 0
    )
    workload["quote_window_telemetry"] = {
        "status": "complete" if exact_window and workload.get("indices_complete", True) else "incomplete",
        "quote_days": quote_days,
        "telemetry_days": telemetry_days,
        "quote_start": quote_labels[0] if quote_labels else None,
        "quote_end": quote_labels[-1] if quote_labels else None,
        "telemetry_start": telemetry_start,
        "telemetry_end": telemetry_end,
        "reason": None if exact_window and workload.get("indices_complete", True) else (
            "Cloud Tracker index telemetry index aggregation was truncated"
            if workload.get("indices_complete") is False
            else workload.get("quote_window_fetch_error") or "Cloud Tracker index telemetry lacks complete exact-window QPS, docs/s, or average document size"
        ),
    }
    return float(ecu["quote_usage_factor"])


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

ECH_SCOPES = ("priced", "all")
MIN_PRICED_ECU_PCT = 90.0


def quote_options(acc: dict) -> dict:
    """How the ECH side of the comparison is built. Set by the run from its flags.

    An account with no options (written before they existed) compares the whole
    org bill, existing serverless spend included, as those runs did.
    """
    raw = (acc.get("sizing") or {}).get("quote_options") or {}
    scope = raw.get("ech_scope") if raw.get("ech_scope") in ECH_SCOPES else "all"
    try:
        minimum = float(raw.get("min_priced_ecu_pct", MIN_PRICED_ECU_PCT))
    except (TypeError, ValueError):
        minimum = MIN_PRICED_ECU_PCT
    return {
        "ech_scope": scope,
        "keep_serverless_spend": bool(raw.get("keep_serverless_spend", True)),
        "unmeasured": raw.get("unmeasured") or "exclude",
        "min_priced_ecu_pct": min(100.0, max(0.0, minimum)),
    }


def unpriced_reason(dep: dict, by_solution: dict, diagnostic: bool) -> str | None:
    """Why an ECH deployment has no serverless price. None when it has one."""
    sol = dep.get("solution") or "search"
    src = dep.get("ingest_source")
    if src == "decommissioned":
        return "decommissioned, not in the admin console"
    if src == "active_no_bundle":
        return "no diagnostic bundle"
    blk = by_solution.get(sol) or {}
    if "quotable" not in str(blk.get("pricing") or ""):
        return f"{sol.title()} has no accepted estimator read-back"
    if sol == "search":
        return None if float(dep.get("gb") or 0) > 0 else "no stored GB to price"
    if diagnostic and float(dep.get("daily_raw_gb") or 0) <= 0:
        return "its bundle measured no bulk"
    return None


def ech_scope(acc: dict, by_solution: dict) -> dict | None:
    """Split quote-window billing from reconciled daily deployment cost series.

    Monthly deployment shares are not used to infer daily allocation. If the
    direct series is missing, incomplete, or misaligned, return an explicit
    unavailable scope rather than presenting a prorated estimate as measured.
    """
    e = acc.get("ecu") or {}
    window_days = int(e.get("quote_lookback_days") or 0)
    days, totals = billed_series(e)
    opts = quote_options(acc)
    daily = e.get("daily_deployment_costs") or {}
    status = daily.get("status") or {}
    labels = list(daily.get("days") or [])
    account_daily_labels = [str(day)[:10] for day in (e.get("daily_days") or [])]
    by_dep = daily.get("by_deployment")
    org_daily_totals = list(daily.get("org_daily_totals") or [])
    window_labels = days[-window_days:] if window_days and days else []
    reason = status.get("reason")
    if not daily.get("complete") or not status.get("complete"):
        reason = reason or "daily per-deployment billing is incomplete or unreconciled"
    elif not window_days or len(window_labels) != window_days:
        reason = "the requested quote window has no complete daily org billing series"
    elif labels != account_daily_labels:
        reason = "daily deployment billing labels do not align with daily org billing"
    elif len(org_daily_totals) != len(labels):
        reason = "daily org billing totals are missing from the deployment billing source"
    elif not isinstance(by_dep, dict):
        reason = "daily per-deployment billing series are unavailable"
    elif any(len(values or []) != len(labels) for values in by_dep.values()):
        reason = "one or more daily deployment billing series have incomplete day coverage"
    else:
        named = [
            str(dep.get("name"))
            for dep in (acc.get("solutions") or {}).get("by_deployment") or []
            if isinstance(dep, dict) and dep.get("name")
        ]
        if any(name not in by_dep for name in named):
            reason = "one or more daily deployment billing series have incomplete day coverage"
    if reason:
        return {
            "status": "unavailable",
            "scope": opts["ech_scope"],
            "window_days": window_days or None,
            "in_comparison_mo": None,
            "by_deployment_mo": {},
            "method": "unavailable: " + reason,
            "reason": reason,
            "keep_serverless_spend": opts["keep_serverless_spend"],
            "min_priced_ecu_pct": opts["min_priced_ecu_pct"],
        }

    diagnostic = (acc.get("sizing") or {}).get("ingest_source") == "diagnostics"
    as_month = DAYS_PER_MONTH / window_days
    rows = [d for d in (acc.get("solutions") or {}).get("by_deployment") or [] if isinstance(d, dict) and d.get("name")]
    priced = 0.0
    unpriced: list[dict] = []
    serverless = 0.0
    by_name: dict[str, float] = {}
    window_indices = [labels.index(day) for day in window_labels]

    def window_ecu(name: str) -> float:
        series = by_dep.get(name) or []
        return sum(float(series[index] or 0) for index in window_indices)

    for dep in rows:
        ecu = window_ecu(dep["name"])
        if dep.get("platform") == "serverless":
            serverless += ecu
            continue
        by_name[dep["name"]] = ecu
        reason = unpriced_reason(dep, by_solution, diagnostic)
        if reason is None:
            priced += ecu
        else:
            unpriced.append({"name": dep["name"], "solution": dep.get("solution") or "search", "reason": reason, "ecu_mo": round(ecu * as_month, 2)})
    total = sum(float(org_daily_totals[index] or 0) for index in window_indices)
    unpriced_total = sum(by_name.get(row["name"], 0.0) for row in unpriced)
    other = max(0.0, total - priced - unpriced_total - serverless)
    compared = priced if opts["ech_scope"] == "priced" else total - (0.0 if opts["keep_serverless_spend"] else serverless)
    unpriced.sort(key=lambda row: (-row["ecu_mo"], row["name"]))
    ech_only = priced + unpriced_total
    return {
        "status": "complete",
        "scope": opts["ech_scope"],
        "keep_serverless_spend": opts["keep_serverless_spend"],
        "window_days": window_days,
        "window_mo": round(total * as_month, 2),
        "priced_mo": round(priced * as_month, 2),
        "unpriced_mo": round(unpriced_total * as_month, 2),
        "serverless_mo": round(serverless * as_month, 2),
        "other_mo": round(other * as_month, 2),
        "in_comparison_mo": round(compared * as_month, 2),
        "priced_deployments": len([d for d in rows if d.get("platform") != "serverless"]) - len(unpriced),
        "unpriced_deployments": len(unpriced),
        "priced_share_pct": round(100 * priced / ech_only, 1) if ech_only > 0 else None,
        "min_priced_ecu_pct": opts["min_priced_ecu_pct"],
        "unpriced": unpriced,
        "by_deployment_mo": {name: round(value * as_month, 2) for name, value in by_name.items()},
        "method": "direct Cloud Tracker daily deployment cost, summed over the quote window; source totals reconcile to org daily billing.",
    }


def _product_split(row: dict) -> tuple[float, float] | None:
    """Logs plus traces, and metrics, from a measured product mix.

    Returns None when the row has no classification. A 90/10 split is not invented.
    """
    mix = row.get("bulk_mix") if isinstance(row.get("bulk_mix"), dict) else None
    if mix is not None:
        logs = float(mix.get("logs") or 0) + float(mix.get("traces") or 0)
        metrics = float(mix.get("metrics") or 0)
    elif any(row.get(key) is not None for key in ("logs_gb_day", "metrics_gb_day", "traces_gb_day")):
        logs = float(row.get("logs_gb_day") or 0) + float(row.get("traces_gb_day") or 0)
        metrics = float(row.get("metrics_gb_day") or 0)
    else:
        return None
    if logs + metrics <= 0:
        return None
    return logs, metrics


def observability_metered_rates(rows: list[dict], factor: float = O11Y_METER_FACTOR) -> list[dict]:
    """Blended Observability ingest rate when each deployment is priced alone.

    Metered GB/day is the row's GB/day times `factor`: 1.0 for diagnostic bulk,
    which is already metered, and 1.595 for a raw rate. Logs plus traces, and
    metrics, each walk the ingest bands using that row's measured product mix.
    A row with no mix keeps its metered volume and has no $/GB, so it is not
    marked as the $0.50 row. A row with no bulk has no rate. This is not a D38 quote.
    """
    out = []
    for row in rows:
        try:
            raw = float(row.get("daily_raw_gb") if row.get("daily_raw_gb") is not None else row.get("bulk_gb_day") or 0)
        except (TypeError, ValueError):
            raw = 0.0
        metered = raw * factor if raw > 0 else 0.0
        rate = None
        split = _product_split(row)
        if metered > 0 and split is not None:
            logs_gb, metrics_gb = split
            share = logs_gb + metrics_gb
            logs = metered * logs_gb / share
            metrics = metered * metrics_gb / share
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
    """Stand-in price for one slice, replaced before anything is published.

    Observability and Security use the Complete ladders until the sheet read-back
    replaces them. Search uses an on-demand ladder until the performant read-back
    replaces it. A missing performant total leaves Search unquoted."""
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
    """Drop the stored-GB ladder's parts once a read-back is the price. They are not parts of that price."""
    for k in ("search_mo", "ingest_mo", "retention_mo", "support_mo", "retained_gb"):
        blk[k] = None


def _apply_search_estimator(by_solution: dict, sz: dict) -> bool:
    """Replace the consolidated Search total with the es3-estimator performant read-back."""
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
    search["pricing"] = "es3-estimator performant (quotable)"
    search["estimator_profile"] = rb.get("profile") or "performant"
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


def _solution_has_regional_readback(sz: dict, solution: str) -> bool:
    """True when a per-region estimator for this solution already has a monthly read-back."""
    for project in ((sz.get("estimators") or {}).get("projects") or []):
        if project.get("solution") != solution:
            continue
        if _money((project.get("readback") or {}).get("monthly_steady_state")) is not None:
            return True
    return False


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


def _search_power(sz: dict, by_solution: dict, _acc: dict | None = None) -> dict | None:
    """es3 profiles when Search was estimated. They are not added to D38 or E74.

    No profiles, and no page, when the Search estimator was skipped or never ran.
    The sized stored GB is not priced as Search in that case.
    """
    search = by_solution.get("search") or {}
    est = (sz.get("estimators") or {}).get("search") or {}
    status = str(est.get("status") or "")
    if status.startswith("skipped") or "no search workload" in status.lower():
        return None
    regional = search.get("regional_profiles") or []
    profiles = regional or (est.get("profiles") or [])
    if not profiles:
        return None
    out = {"profiles": profiles}
    if est.get("region_match") and not regional:
        out["region_match"] = est["region_match"]
    return out


def _apply_eis_to_plan(acc: dict, eis_with_support: float) -> None:
    """Add the cover's Search EIS dollar once to every bar that prices Search.

    Packing does not change which Search deployments are sized, so every bar
    gets the same amount. It is spread across that bar's Search groups so the
    per-cluster columns still add up to the bar.
    """
    amount = round(float(eis_with_support or 0), 2)
    if amount <= 0:
        return
    plan = (acc.get("sizing") or {}).get("deployment_plan") or {}
    if plan.get("error"):
        return
    by_name = {
        d.get("name"): d
        for d in ((acc.get("solutions") or {}).get("by_deployment") or [])
        if d.get("name")
    }
    for bar in plan.get("bars") or []:
        if not isinstance(bar, dict) or bar.get("reused_headline") or bar.get("monthly") is None:
            continue
        search_groups = []
        for group in bar.get("groups") or []:
            names = [name for name in (group.get("names") or []) if name]
            if names and all(((by_name.get(name) or {}).get("solution") or "search") == "search" for name in names):
                search_groups.append(group)
        if not search_groups:
            continue
        weights = [max(float(group.get("monthly") or 0), 0.0) for group in search_groups]
        weight_total = sum(weights)
        allocated = 0.0
        for index, group in enumerate(search_groups):
            if index == len(search_groups) - 1:
                share = round(amount - allocated, 2)
            elif weight_total:
                share = round(amount * weights[index] / weight_total, 2)
            else:
                share = round(amount / len(search_groups), 2)
            allocated = round(allocated + share, 2)
            group["monthly"] = round(float(group.get("monthly") or 0) + share, 2)
            group["eis_with_support_mo"] = share
        bar["monthly"] = round(float(bar["monthly"]) + amount, 2)
        bar["eis_with_support_mo"] = amount


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
        bars.append({
            "label": row.get("label") or "",
            "monthly": monthly,
            "projects": row.get("projects"),
            "from_projects": row.get("from_projects"),
        })
    return bars


def per_cluster_plan(sz: dict, acc: dict, infra: dict | None) -> dict | None:
    """Serverless $/mo for each cluster at every deployment-count step, from the ceiling (1:1) to the floor.

    A cluster's cost at a step is its project's priced total shared by bulk GB/day. A project with
    no bulk is shared evenly. The columns add up to the bars. None when there is nothing to split.
    """
    raw = sz.get("deployment_plan") or {}
    bars = [b for b in (raw.get("bars") or []) if isinstance(b, dict)]
    if raw.get("error") or len(bars) < 1 or any(not b.get("groups") for b in bars):
        return None
    meta: dict[str, dict] = {}
    region_by_name = {
        d.get("name"): d.get("region")
        for d in ((infra or {}).get("by_deployment") or []) + (((acc.get("cloud") or {}).get("by_deployment")) or [])
        if d.get("region")
    }
    for d in (acc.get("solutions") or {}).get("by_deployment") or []:
        meta[d.get("name")] = {"solution": d.get("solution"), "region": region_by_name.get(d.get("name"))}
    steps = [{"label": b.get("label") or "", "projects": b.get("projects"), "monthly": b.get("monthly")} for b in bars]
    by_name = {d.get("name"): d for d in (acc.get("solutions") or {}).get("by_deployment") or []}
    rows: dict[str, dict] = {}
    unallocated = [0.0] * len(steps)
    for si, bar in enumerate(bars):
        for g in bar["groups"]:
            names = [n for n in g.get("names") or [] if n]
            if not names:
                continue
            bulk = {n: float((by_name.get(n) or {}).get("daily_raw_gb") or 0) for n in names}
            store = {n: float((by_name.get(n) or {}).get("gb") or 0) for n in names}
            # Packing keeps one feature per project. Search is priced on stored GB, so a Search
            # project is shared by stored GB. Observability and Security are shared by bulk GB/day.
            is_search = all(((by_name.get(n) or {}).get("solution") or "search") == "search" for n in names)
            weight = store if is_search else bulk
            money = float(g.get("monthly") or 0)
            # A cluster that was measured and has nothing to price is not sized. One with no bundle is
            # flagged missing. Money a project carries for clusters that are not sized stays
            # unallocated, so each column still adds up to its bar.
            def _status(n: str) -> str:
                src = (by_name.get(n) or {}).get("ingest_source")
                if src == "decommissioned":
                    return "decommissioned"
                if src == "active_no_bundle":
                    return "missing_diag"
                return "no_bulk" if weight[n] < 0.0005 and src == "diagnostics" else "sized"

            sized = [n for n in names if _status(n) == "sized"]
            sized_weight = sum(weight[n] for n in sized)
            for n in names:
                status = _status(n)
                row = rows.setdefault(n, {
                    "name": n, "solution": (meta.get(n) or {}).get("solution"), "region": (meta.get(n) or {}).get("region"),
                    "bulk_gb_day": round(bulk[n], 3), "stored_gb": round(store[n], 2),
                    "share_basis": "stored GB" if is_search else "bulk GB/day",
                    "status": status, "steps": [None] * len(steps),
                })
                if status != "sized":
                    row["steps"][si] = {"monthly": None, "project_monthly": round(money, 2), "pack": len(names)}
                    continue
                share = weight[n] / sized_weight if sized_weight > 0 else 1.0 / len(sized)
                row["steps"][si] = {"monthly": round(money * share, 2), "project_monthly": round(money, 2), "pack": len(names)}
            if not sized:
                unallocated[si] = round(unallocated[si] + money, 2)
    out_rows = sorted(rows.values(), key=lambda r: (r.get("status") != "sized", -float((r["steps"][0] or {}).get("monthly") or 0)))
    if len(out_rows) < 2:
        return None
    return {
        "steps": steps,
        "rows": out_rows,
        "unallocated": unallocated,
        "method": "Each project's priced $/mo shared across its clusters: by stored GB in a Search project, by bulk GB/day in an Observability or Security project, and evenly when the project has neither.",
    }


def _deployment_groups(acc: dict, infra: dict | None, months: float) -> list[dict]:
    """ECH deployments grouped by solution and region. A serverless project lives in one region."""
    region_by_name = {
        d.get("name"): d.get("region")
        for d in ((infra or {}).get("by_deployment") or []) + (((acc.get("cloud") or {}).get("by_deployment")) or [])
        if d.get("region")
    }
    grouped: dict[tuple[str, str], dict] = {}
    by_name = {d.get("name"): d for d in (acc.get("solutions") or {}).get("by_deployment") or [] if d.get("platform") != "serverless"}
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
            if g["solution"] == "observability":
                from diag_account import observability_mix
                try:
                    row["mix"] = observability_mix([by_name[n] for n in g["deployments"] if n in by_name])
                except ValueError as exc:
                    # One Observability deployment without a mix is named and left
                    # unquoted. Security and Search still build.
                    row["mix"] = None
                    row["mix_error"] = str(exc)
            if g["retention_bulk"] > 0:
                row["retention_months_measured"] = round((g["retention_weighted"] / g["retention_bulk"]) / DAYS_PER_MONTH, 6)
            if g["daily_raw_gb"]:
                # One-year estimate: every project with bulk is written at 12 months, measured or not.
                from diag_inputs import ESTIMATE_MONTHS
                row["retention_months"] = ESTIMATE_MONTHS
        groups.append(row)
    order = {"search": 0, "observability": 1, "security": 2}
    groups.sort(key=lambda g: (order.get(g["solution"], 9), -g["ech_ecu_mo"]))
    return groups


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
    of the one consolidated total. A rejected region zeros that solution.
    Search, Observability, and Security use the same rule.
    """
    rejected = [row for row in classified if row.get("kind") == "rejected"]
    if rejected:
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
    how = "performant read-back split by stored GB" if solution == "search" else "sheet read-back split by stored GB"
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
    """One serverless project per solution per region. Each project uses its own sizing when one was written.
    """
    spans = {}
    for g in groups:
        spans.setdefault(g["solution"], set()).add(g["region"])
    if not any(len(v) > 1 for v in spans.values()):
        return False
    cloud = (acc.get("cloud") or {}).get("summary") or {}
    provider = cloud.get("provider")
    _store = acc.get("storage") or {}
    org_gb = float(_store.get("ingest_share_store_gb") or _store.get("data_stored_gb") or 0)
    search_slice = ((acc.get("sizing") or {}).get("consolidated") or {}).get("search") or {}
    workload = search_slice.get("workload") or acc.get("workload") or {}
    support = ((acc.get("sizing") or {}).get("support_tier")) or "Premium"
    saved = _saved_projects(acc)
    store_split = False
    regional_quoted = False
    profile_keys = ("Total $/month", "Provisioned Search VCU/h", "Idle Search VCU/h", "Search $/month", "Ingest $/month", "Storage $/month", "Support $/month")
    for sol, regions in spans.items():
        if len(regions) <= 1:
            continue
        blk = by_solution.get(sol) or {}
        if "quotable" not in str(blk.get("pricing") or "") and not any(key[0] == sol for key in saved):
            continue
        rows = [g for g in groups if g["solution"] == sol]
        classified = []
        for g in rows:
            source_months = g.get("retention_months") if g.get("retention_months") is not None else blk.get("retention_months")
            try:
                region_daily = float(g.get("daily_raw_gb") or 0)
            except (TypeError, ValueError):
                region_daily = 0.0
            if sol == "security":
                if region_daily <= 0:
                    continue
            saved_hit = saved.get((sol, g["region"])) or {}
            saved_rb = saved_hit.get("readback") or {}
            saved_search = sol == "search" and _money(saved_rb.get("monthly_steady_state")) is not None
            if sol == "search" and g["stored_gb"] > 0 and (str(blk.get("pricing") or "").startswith("es3-estimator") or saved_search):
                search_how = region_match_how(saved_hit.get("region_match"))
                if saved_rb and search_how in {"provider_default", "nearest", "none", "unknown"}:
                    notes.append(f"Search {g['region']} is not on the estimator list, so that region was not quoted.")
                    classified.append({
                        "region": g["region"], "stored_gb": g["stored_gb"], "kind": "rejected",
                        "own_pricing": f"rejected — region {g['region']} is not on the estimator list",
                        "region_match": saved_hit.get("region_match"), "url": saved_hit.get("url"),
                    })
                    continue
                result = {"readback": saved_rb, "profiles": saved_hit.get("profiles"), "region_match": saved_hit.get("region_match")} if _money(saved_rb.get("monthly_steady_state")) is not None else _es3_region({
                    "stored_gb": g["stored_gb"],
                    "org_stored_gb": org_gb,
                    "provider": provider,
                    "region": g["region"],
                    "support_tier": support,
                    "workload": workload,
                })
                rb = (result or {}).get("readback") or {}
                monthly = _money(rb.get("monthly_steady_state"))
                classified.append({
                    "region": g["region"], "stored_gb": g["stored_gb"],
                    "retention_months": source_months,
                    "kind": "own" if monthly is not None else "missing",
                    "own_monthly": monthly, "own_pricing": "re-estimated performant",
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
            if sol == "observability" and g.get("mix_error") and not saved_hit.get("readback"):
                classified.append({
                    "region": g["region"], "stored_gb": g["stored_gb"], "kind": "rejected",
                    "own_pricing": "rejected — " + str(g["mix_error"]),
                })
                continue
            gate = saved_hit.get("echo_gate") or saved_hit.get("g28_gate") or saved_hit.get("g26_gate") or {}
            how = region_match_how(saved_hit.get("region_match"))
            if saved_hit.get("readback") and how in {"provider_default", "nearest", "none", "unknown"}:
                notes.append(
                    f"{sol.title()} {g['region']} is not on the estimator list, so that region was not quoted."
                )
                classified.append({
                    "region": g["region"], "stored_gb": g["stored_gb"], "kind": "rejected",
                    "own_pricing": f"rejected — region {g['region']} is not on the estimator list",
                    "region_match": saved_hit.get("region_match"), "url": saved_hit.get("url"),
                })
                continue
            echo_rejected = sol in {"observability", "security"} and (
                bool(saved_hit.get("readback_rejected")) or (bool(gate) and not gate.get("accepted"))
            )
            sheet_read = _security_sheet_was_read(saved_hit)
            e73_rejected = sol == "security" and sheet_read and e73_blocks_quote(saved_rb.get("cps_addon_monthly"))
            if echo_rejected:
                notes.append(f"{sol.title()} {g['region']} the sheet echo differed from the written bulk, so that region was not quoted.")
                classified.append({
                    "region": g["region"], "stored_gb": g["stored_gb"], "kind": "rejected",
                    "own_pricing": "rejected — the sheet echo differs from the written bulk",
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
                "retention_months": source_months,
                "kind": "own" if sheet_monthly is not None else "missing",
                "own_monthly": sheet_monthly,
                "own_pricing": "sheet read-back" if sheet_monthly is not None else "",
                "own_annual": _money(saved_rb.get("annual_year1")),
                "own_steady": _money(saved_rb.get("annual_steady_state")),
                "region_match": saved_hit.get("region_match"), "url": saved_hit.get("url"),
            })
        fidelity = [
            row for row in classified
            if row.get("kind") == "rejected" and "not on the estimator list" in str(row.get("own_pricing") or "")
        ]
        if not classified:
            blk["by_region"] = []
            blk["regional_mode"] = "no_eligible_region"
            blk["total_mo"] = 0.0
            blk["total_yr"] = 0.0
            blk["pricing"] = "omitted — region is not on the estimator list" if fidelity else blk.get("pricing")
            continue
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
            if sol != "search":
                # The regional sheets are the price. The ladder's ingest, retention and support
                # lines came from stored GB and must not be drawn beside it.
                if "ballpark" not in blk:
                    _stash_model(blk)
                _clear_components(blk)
                metered = (acc.get("sizing") or {}).get("ingest_source") == "diagnostics"
                blk["daily_metered_gb"] = round(sum(float(g.get("daily_raw_gb") or 0) for g in rows), 3) if metered else None
            blk["total_mo"] = quote["total_mo"]
            blk["total_yr"] = quote["total_yr"]
            if "quotable" not in str(blk.get("pricing") or ""):
                blk["pricing"] = (
                    "es3-estimator performant (quotable)" if sol == "search"
                    else f"{sol.title()} sheet read-back (quotable)"
                )
            regional_quoted = True
        elif "quotable" in str(blk.get("pricing") or ""):
            store_split = True
        else:
            blk["regional_mode"] = "missing"
            blk["pricing"] = "omitted — a region had no read-back"
            blk["total_mo"] = 0.0
            blk["total_yr"] = 0.0
            notes.append(f"{sol.title()} was left out of the quote because a region had no read-back.")
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
    return regional_quoted


def _search_eis(
    acc: dict,
    months: float,
    *,
    usage_factor: float = 1.0,
    eligible_names: set[str] | None = None,
) -> dict:
    """Price EIS only from complete source totals, preferring reconciled daily data."""
    region = {
        d.get("name"): d.get("region")
        for d in ((acc.get("cloud") or {}).get("by_deployment") or [])
        if d.get("region")
    }
    eligible_names = eligible_names or set()
    ai_ml = acc.get("ai_ml") or {}
    deployment_rows = {d.get("name"): d for d in ai_ml.get("by_deployment") or [] if d.get("name")}
    source_totals = ai_ml.get("billing_by_deployment") or {}
    totals_complete = bool(ai_ml.get("billing_by_deployment_complete"))
    daily_usage = ai_ml.get("daily_usage_by_deployment") or {}
    e = acc.get("ecu") or {}
    quote_days = int(e.get("quote_lookback_days") or 0)
    daily_labels, _daily_totals = billed_series(e)
    window_labels = daily_labels[-quote_days:] if quote_days and daily_labels else []
    if not eligible_names:
        return {"status": "not_applicable", "reason": "no priced Search deployments are eligible for EIS"}

    # The composite total query is the authoritative source for fetched-period
    # totals. Missing data must not be interpreted as a zero-cost deployment.
    if not totals_complete:
        return {
            "status": "incomplete",
            "reason": (ai_ml.get("billing_by_deployment_status") or {}).get("reason") or "complete filtered deployment totals are unavailable",
        }

    def total_for(name: str) -> dict | None:
        raw = source_totals.get(name)
        if raw is None:
            # A completed composite has no bucket for a deployment with no
            # billing documents in the fetched interval, which means zero.
            return {"eis": 0.0, "tokens": 0.0, "ml": 0.0}
        try:
            return {
                "eis": float(raw["eis_ecu"]),
                "tokens": float(raw["million_tokens"]),
                "ml": float(raw["ml_node_ecu"]),
            }
        except (KeyError, TypeError, ValueError):
            return None

    if any(total_for(name) is None for name in eligible_names):
        return {"status": "incomplete", "reason": "a deployment total is missing a required filtered billing metric"}

    daily_complete = bool(ai_ml.get("daily_usage_complete")) and bool(window_labels)
    if daily_complete:
        for name in eligible_names:
            data = daily_usage.get(name) or {}
            days = list(data.get("days") or [])
            fields = ("eis_ecu", "million_tokens", "ml_node_ecu", "models")
            if any(len(data.get(field) or []) != len(days) for field in fields) or any(day not in days for day in window_labels):
                daily_complete = False
                break
            authoritative = total_for(name)
            if authoritative is None:
                daily_complete = False
                break
            for field, total_key in (("eis_ecu", "eis"), ("million_tokens", "tokens"), ("ml_node_ecu", "ml")):
                daily_sum = sum(float(v or 0) for v in (data.get(field) or []))
                tolerance = max(0.02, abs(authoritative[total_key]) * 0.001)
                if abs(daily_sum - authoritative[total_key]) > tolerance:
                    daily_complete = False
                    break
            if not daily_complete:
                break

    if not daily_complete:
        return {
            "status": "incomplete",
            "reason": "complete daily EIS and ML-node telemetry for the exact quote window is unavailable or does not reconcile to fetched-period totals",
        }

    exact = daily_complete
    scale = DAYS_PER_MONTH / len(window_labels)
    rows = []
    for name in eligible_names:
        d = deployment_rows.get(name) or {}
        if not name or name not in eligible_names:
            continue
        if exact:
            data = daily_usage.get(name) or {}
            indices = {day: i for i, day in enumerate(data.get("days") or [])}
            def sum_window(field: str) -> float:
                values = data.get(field) or []
                return sum(float(values[indices[day]] or 0) for day in window_labels if day in indices)
            models_by_day = data.get("models") or []
            models = sorted({str(model) for day in window_labels if day in indices
                             for model in (models_by_day[indices[day]] or []) if model})
            labels = dict(zip(d.get("models") or [], d.get("model_labels") or []))
            row = {
                "name": name, "region": region.get(name) or "unknown region",
                "ml": sum_window("ml_node_ecu"),
                "eis": sum_window("eis_ecu"), "million_tokens": sum_window("million_tokens"),
                "models": models, "model_labels": [labels.get(model, model) for model in models],
                "elser": any("elser" in model.lower() for model in models),
            }
        else:
            models = list(d.get("models") or [])
            totals = total_for(name)
            ml = totals["ml"]
            row = {
                "name": name,
                "region": region.get(name) or "unknown region",
                "ml": ml,
                "eis": totals["eis"],
                "million_tokens": totals["tokens"],
                "models": models,
                "model_labels": list(d.get("model_labels") or []),
                "elser": bool(d.get("elser")),
            }
        if row["ml"] <= 0 and row["eis"] <= 0 and not row["elser"] and not row["models"]:
            continue
        rows.append(row)
    if not rows:
        return {
            "status": "complete",
            "usage_basis": "quote_window_daily",
            "usage_days": len(window_labels) if exact else None,
            "monthly": 0.0,
            "by_region": {},
            "ml_node_ecu": 0.0,
            "ml_node_ecu_mo": 0.0,
            "models": [],
            "deployments": 0,
            "workloads": [],
        }
    by_region: dict[str, float] = {}
    for r in rows:
        by_region[r["region"]] = by_region.get(r["region"], 0.0) + r["eis"] * scale
    models = sorted({m for r in rows for m in r["models"] if m})
    if any(r["elser"] for r in rows) and "ELSER" not in models:
        models.append("ELSER")
    workloads = []
    for r in rows:
        if r["eis"] <= 0 and r["million_tokens"] <= 0:
            continue
        labels = r["model_labels"] or r["models"]
        workloads.append({
            "model": ", ".join(str(label) for label in labels) or r["name"],
            "kind": f"Inference tokens · {r['region']}",
            "deployment": r["name"],
            "million_tokens": round(r["million_tokens"], 4),
            "ecu": round(r["eis"], 2),
            "monthly": round(r["eis"] * scale, 2),
        })
    return {
        "status": "complete",
        "monthly": round(sum(by_region.values()), 2),
        "by_region": {k: round(v, 2) for k, v in by_region.items()},
        "ml_node_ecu_mo": round(sum(r["ml"] for r in rows) * scale, 2),
        "ml_node_ecu": round(sum(r["ml"] for r in rows), 2),
        "models": models,
        "deployments": len(rows),
        "workloads": workloads,
        "usage_basis": "quote_window_daily",
        "usage_days": len(window_labels) if exact else None,
    }


def _apply_search_eis(
    acc: dict,
    by_solution: dict,
    months: float,
    notes: list[str],
    *,
    usage_factor: float = 1.0,
    eligible_names: set[str] | None = None,
) -> None:
    eis = _search_eis(acc, months, usage_factor=usage_factor, eligible_names=eligible_names)
    blk = by_solution.get("search") or {}
    if "quotable" not in str(blk.get("pricing") or ""):
        blk["eis_data_status"] = {"status": "not_applicable", "reason": "Search base estimate is not quotable"}
        return
    blk["eis_data_status"] = {key: value for key, value in eis.items() if key in ("status", "reason", "usage_basis", "usage_days")}
    if eis.get("status") == "incomplete":
        notes.append(
            "Search EIS was not added because complete filtered deployment billing totals could not be verified. "
            "The Search estimate stays the read-back from the notebook or spreadsheet."
        )
        return
    if eis.get("status") == "not_applicable":
        return
    if eis.get("monthly") is None:
        return
    base = sum(float(blk.get(k) or 0) for k in ("search_mo", "ingest_mo", "retention_mo"))
    support = float(blk.get("support_mo") or 0)
    rate = (support / base) if base else 0.0
    usage = float(eis["monthly"] or 0)
    blk["eis_mo"] = usage
    blk["eis_models"] = eis["models"]
    blk["ml_node_ecu_mo"] = eis["ml_node_ecu_mo"]
    blk["ml_node_ecu"] = eis.get("ml_node_ecu")
    blk["ml_node_ecu_mo"] = eis.get("ml_node_ecu_mo")
    blk["eis_workloads"] = eis["workloads"]
    blk["eis_deployments"] = eis["deployments"]
    blk["eis_usage_basis"] = eis["usage_basis"]
    blk["eis_usage_days"] = eis.get("usage_days")
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
    if eis["usage_basis"] == "quote_window_daily":
        pace_text = f"EIS inference billing was summed over the {eis['usage_days']}-day quote window"
    else:
        pace_text = "Fetched-period EIS inference billing was scaled by the account quote-usage factor"
    notes.append(
        f"The Search EIS line includes activity found on {eis['deployments']} sized ECH Search deployment(s). "
        f"{pace_text}. EIS is ${usage:,.2f}/mo before support, based on billed inference ECU and tokens. "
        f"ML-node ECU is tracked separately (${eis['ml_node_ecu_mo']:,.2f}/mo after normalization) and is not attributed to EIS."
    )


def _priced_search_names(acc: dict, by_solution: dict, scope: dict | None) -> set[str]:
    """ECH Search deployments represented in the accepted Search quote."""
    search = by_solution.get("search") or {}
    if "quotable" not in str(search.get("pricing") or ""):
        return set()
    regional_mode = search.get("regional_mode")
    accepted_regions: set[str] | None = None
    if regional_mode is not None:
        if regional_mode not in ("per_region", "store_split"):
            return set()
        accepted_regions = {
            str(row.get("region"))
            for row in search.get("by_region") or []
            if row.get("region") and row.get("monthly") is not None
        }
    rows = (acc.get("solutions") or {}).get("by_deployment") or []
    active = (acc.get("deployments") or {}).get("active") or {}
    active_names = set(active.get("names") or [])
    unpriced_names = {str(row.get("name")) for row in (scope or {}).get("unpriced") or []}
    region_by_name = {
        str(row.get("name")): str(row.get("region"))
        for row in ((acc.get("cloud") or {}).get("by_deployment") or [])
        if row.get("name") and row.get("region")
    }
    diagnostic = (acc.get("sizing") or {}).get("ingest_source") == "diagnostics"
    eligible = set()
    for dep in rows:
        name = str(dep.get("name") or "")
        if not name or dep.get("platform") == "serverless" or (dep.get("solution") or "search") != "search":
            continue
        if active_names and name not in active_names:
            continue
        if accepted_regions is not None and region_by_name.get(name) not in accepted_regions:
            continue
        if scope is not None:
            if name in unpriced_names:
                continue
        elif unpriced_reason(dep, by_solution, diagnostic) is not None:
            continue
        eligible.add(name)
    return eligible


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
    rec["evidence"] = (
        f"ECH billed cost in K360 is ${savings['ech_list_mo']:,.0f}/mo. "
        "Serverless is "
        + " + ".join(parts)
        + f" = ${savings['serverless_mo']:,.0f}/mo."
    )
    eis_mo = (by_solution.get("search") or {}).get("eis_mo")
    if eis_mo:
        rec["evidence"] += f" That Search total includes EIS at ${float(eis_mo):,.2f}/mo from billed inference ECU and tokens; ML-node ECU is tracked separately."
    gap, year = float(savings["list_mo"]), float(savings.get("list_yr") or 0)
    side = "above" if gap < 0 else "below"
    rec["impact"] = f"${abs(gap):,.0f}/mo {side} the ECH billed cost (${abs(year):,.0f}/yr)"
    rec["title"] = str(rec.get("title") or "").replace(" (ES3)", "")
    if gap < 0:
        # Serverless is estimated to cost more. This is not a cost saving and must not rank as one.
        rec["severity"] = "low"
        rec["title"] = "Serverless is estimated to cost more than ECH for this fleet"
        rec["action"] = "Do not position this move as a cost saving. Check the estimator inputs, and compare on operations and scale instead of price."
    else:
        rec["action"] = "Quote the Search Serverless and Observability Serverless estimators. Migrate the dominant slice first. Keep latency-sensitive search on ECH if a project cannot meet it."


def _has_workload(blk: dict) -> bool:
    """A solution has a workload when something in it was sized.

    A solution whose only deployments were left unsized (no bundle, decommissioned)
    has none. Priced share reports those. Direct read-back does not count them twice.
    """
    count = blk.get("sized_deployments") if blk.get("sized_deployments") is not None else blk.get("deployments")
    return bool((blk.get("stored_gb") or 0) or (blk.get("daily_raw_gb") or 0) or (count or 0))


def _cell_echo_fields(gate: dict) -> dict:
    """Per-cell Observability echo, when the gate recorded G19 and G20 separately."""
    if not isinstance(gate, dict) or "written_logs_traces" not in gate:
        return {}
    return {
        "cell_echo": True,
        "written_logs_traces": gate.get("written_logs_traces"),
        "written_metrics": gate.get("written_metrics"),
        "sheet_logs_traces": gate.get("sheet_logs_traces"),
        "sheet_metrics": gate.get("sheet_metrics"),
    }


def _written_regions(estimators: dict) -> list[dict]:
    """Sheets and notebooks that were actually written, with the region match they recorded."""
    rows = []
    for sol in ("observability", "security", "search"):
        entry = estimators.get(sol) or {}
        if not isinstance(entry, dict):
            continue
        if entry.get("region_match") or entry.get("readback_rejected"):
            rows.append({
                "solution": sol,
                "region": entry.get("region"),
                "region_match": entry.get("region_match"),
                "readback_rejected": entry.get("readback_rejected"),
                "provider": entry.get("provider"),
            })
    for project in estimators.get("projects") or []:
        if not isinstance(project, dict):
            continue
        if not (project.get("region_match") or project.get("readback_rejected") or (project.get("readback") or {}).get("monthly_steady_state") is not None):
            continue
        rows.append({
            "solution": project.get("solution"),
            "region": project.get("region"),
            "region_match": project.get("region_match"),
            "readback_rejected": project.get("readback_rejected"),
            "provider": project.get("provider"),
        })
    return rows


def _plan_consistency_facts(sz: dict, by_solution: dict, headline: float) -> dict:
    plan = sz.get("deployment_plan") or {}
    bars = [bar for bar in (plan.get("bars") or []) if isinstance(bar, dict)]
    floor = next((bar for bar in bars if bar.get("label") == "Floor"), None)
    quoted = {
        sol for sol, blk in by_solution.items()
        if "quotable" in str(blk.get("pricing") or "") and float(blk.get("total_mo") or 0) > 0
    }
    unquoted_priced = False
    for bar in bars:
        for group in bar.get("groups") or []:
            if float(group.get("monthly") or 0) <= 0:
                continue
            sols = set()
            # Group solution is carried when the plan builder set it. Otherwise leave the check to the floor gap.
            if group.get("solution"):
                sols.add(group.get("solution"))
            if sols and not sols <= quoted:
                unquoted_priced = True
    regional = plan.get("floor")
    return {
        "headline": headline,
        "floor_monthly": None if floor is None else floor.get("monthly"),
        "floor_projects": None if floor is None else floor.get("projects"),
        "regional_projects": regional,
        "bar_prices_unquoted": unquoted_priced,
    }


def _sync_plan_to_quote(acc: dict, by_solution: dict) -> None:
    """Drop plan dollars for a region or solution the headline did not quote.

    The remaining groups stay priced, and the bar is their sum. A gap is marked
    where a group was taken off.
    """
    plan = (acc.get("sizing") or {}).get("deployment_plan") or {}
    bars = plan.get("bars") or []
    if not bars:
        return
    region_by_name = {
        dep.get("name"): dep.get("region")
        for dep in ((acc.get("cloud") or {}).get("by_deployment") or [])
        if dep.get("name") and dep.get("region")
    }
    by_name = {
        dep.get("name"): dep
        for dep in (acc.get("solutions") or {}).get("by_deployment") or []
        if dep.get("name")
    }
    quoted_regions: dict[str, set[str] | None] = {}
    for sol, blk in by_solution.items():
        if "quotable" not in str(blk.get("pricing") or "") or float(blk.get("total_mo") or 0) <= 0:
            continue
        regions = {
            row.get("region") for row in (blk.get("by_region") or [])
            if row.get("monthly") not in (None, 0) and "rejected" not in str(row.get("pricing") or "")
        }
        quoted_regions[sol] = regions or None
    for bar in bars:
        if not isinstance(bar, dict) or bar.get("reused_headline"):
            continue
        total = 0.0
        priced = False
        gap = False
        for group in bar.get("groups") or []:
            if not isinstance(group, dict):
                continue
            if group.get("error"):
                group["monthly"] = None
                group["unpriced_reason"] = str(group.get("error"))
                gap = True
                continue
            names = [name for name in (group.get("names") or []) if name]
            sols = {str((by_name.get(name) or {}).get("solution") or "search") for name in names}
            regions = {region_by_name.get(name) for name in names if region_by_name.get(name)}
            sol = next(iter(sols)) if len(sols) == 1 else None
            region = next(iter(regions)) if len(regions) == 1 else None
            allowed = quoted_regions.get(sol) if sol else None
            if sol not in quoted_regions:
                group["monthly"] = None
                group["unpriced_reason"] = f"{(sol or 'solution').title()} was not quoted"
                gap = True
                continue
            if allowed is not None and region not in allowed:
                group["monthly"] = None
                group["unpriced_reason"] = f"region {region} is not on the estimator list"
                gap = True
                continue
            if group.get("monthly") is None:
                gap = True
                continue
            total += float(group.get("monthly") or 0)
            priced = True
        if priced:
            bar["monthly"] = round(total, 2)
            if gap:
                bar["gap"] = True
        elif gap:
            bar["monthly"] = None
            bar["unpriced_reason"] = bar.get("unpriced_reason") or "unpriced"


def _integrity(acc: dict, sz: dict, by_solution: dict, quotes: list[dict], headline: float, scope: dict | None = None, billing_window: dict | None = None) -> dict:
    """Quote integrity checks. The mean skips a check that does not apply."""
    estimators = sz.get("estimators") or {}
    o11y = estimators.get("observability") or {}
    security = estimators.get("security") or {}
    o11y_blk = by_solution.get("observability") or {}
    sec_blk = by_solution.get("security") or {}
    coverage = dict((sz.get("diagnostics") or {}) if sz.get("ingest_source") == "diagnostics" else {})
    if coverage and coverage.get("retention_policy_bulk_gb_day") is None:
        policy = sum(
            float(dep.get("daily_raw_gb") or 0)
            for dep in (acc.get("solutions") or {}).get("by_deployment") or []
            if isinstance(dep, dict) and dep.get("platform") != "serverless" and dep.get("retention_basis") == "policy"
        )
        if any(
            isinstance(dep, dict) and dep.get("retention_basis")
            for dep in (acc.get("solutions") or {}).get("by_deployment") or []
        ):
            coverage["retention_policy_bulk_gb_day"] = round(policy, 3)
    unmeasured = sum(float(row.get("bulk_gb_day") or 0) for row in (coverage.get("retention_unmeasured") or []))
    regional_sheets = []
    for project in (estimators.get("projects") or []):
        if project.get("solution") not in ("observability", "security") or not project.get("readback"):
            continue
        gate = project.get("echo_gate") or {}
        regional_sheets.append({
            "solution": project["solution"],
            "region": project.get("region"),
            "written_bulk": gate.get("written_bulk") if gate.get("written_bulk") is not None else project.get("daily_raw_gb"),
            "sheet_raw": ingest_echo(project.get("readback") or {}),
            "e73": (project.get("readback") or {}).get("cps_addon_monthly"),
            **_cell_echo_fields(gate),
        })
    plan_bars = [
        {"label": b.get("label"), "projects": b.get("projects"), "monthly": b.get("monthly")}
        for b in ((sz.get("deployment_plan") or {}).get("bars") or [])
        if isinstance(b, dict) and not b.get("reused_headline")
    ]
    return integrity_score({
        "plan": plan_bars,
        "regional_sheets": regional_sheets,
        "observability": {
            "has_workload": _has_workload(o11y_blk),
            "sheet_read": bool(o11y.get("readback")),
            "written_bulk": float(o11y_blk.get("daily_raw_gb") or 0),
            "sheet_raw": ingest_echo(o11y.get("readback") or {}),
            **_cell_echo_fields(o11y.get("echo_gate") or {}),
        },
        "security": {
            "has_workload": _has_workload(sec_blk),
            "sheet_read": bool(security.get("readback")),
            "written_bulk": float(sec_blk.get("daily_raw_gb") or 0),
            "sheet_raw": ingest_echo(security.get("readback") or {}),
            "e73": (security.get("readback") or {}).get("cps_addon_monthly"),
        },
        "coverage": None if not coverage else {
            "matched": coverage.get("matched"),
            "active": None if coverage.get("active_deployments") is None else coverage.get("active_deployments") - int(coverage.get("decommissioned") or 0),
            "missing_diagnostics": coverage.get("missing_diagnostics"),
            "fallback_names": coverage.get("fallback_names") or [],
            "bulk_gb_day": coverage.get("bulk_gb_day"),
            "retention_policy_bulk_gb_day": coverage.get("retention_policy_bulk_gb_day"),
            "bulk_short_window_gb_day": coverage.get("bulk_short_window_gb_day"),
            "bulk_restart_gb_day": coverage.get("bulk_restart_gb_day"),
            "unmeasured_bulk": unmeasured,
            "unrated": coverage.get("unrated"),
            "estimation_limits": coverage.get("estimation_limits") or [],
        },
        "eis": (by_solution.get("search") or {}).get("eis_data_status"),
        "solutions": [
            {
                "solution": sol,
                "has_workload": _has_workload(blk),
                "direct": "quotable" in str(blk.get("pricing") or "") and blk.get("regional_mode") != "store_split",
                "serverless_mo": blk.get("total_mo"),
            }
            for sol, blk in by_solution.items()
        ],
        "written_regions": _written_regions(estimators),
        "plan_consistency": _plan_consistency_facts(sz, by_solution, headline),
        "billing_window": billing_window,
        "headline_monthly": headline,
        "lines": quotes,
        "priced": None if not scope else {
            "status": scope.get("status"),
            "reason": scope.get("reason"),
            "share_pct": scope.get("priced_share_pct"),
            "min_pct": scope.get("min_priced_ecu_pct"),
            "priced": scope.get("priced_deployments"),
            "unpriced": scope.get("unpriced_deployments"),
            "unpriced_mo": scope.get("unpriced_mo"),
            "names": [row["name"] for row in (scope.get("unpriced") or [])[:6]],
        },
    })


# --------------------------------------------------------------------------- main build
def _excluded_names(acc: dict) -> set[str]:
    """Deployment names removed from the run. They are never part of a priced average."""
    raw = ((acc.get("meta") or {}).get("excluded_deployments") or {}).get("deployments") or []
    names: set[str] = set()
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))
        elif isinstance(item, str) and item:
            names.add(item)
    return names


def recent_ech_months_priced(acc: dict, by_solution: dict, n: int = 3) -> dict:
    """Last `n` complete calendar months of ECH cost for priced deployments only.

    Each month is the sum of `ecu.monthly_by_deployment` for ECH deployments whose
    `unpriced_reason` is None. Those are whole calendar months, not a share of a
    daily bill. An excluded deployment is left out of the average and the unpriced list.
    """
    empty = {
        "months": [], "average": None, "comparison": None, "comparison_basis": None,
        "stable": None, "unpriced": [], "unpriced_avg_mo": None, "basis": "monthly_by_deployment",
    }
    ecu = acc.get("ecu") or {}
    monthly = ecu.get("monthly") or {}
    labels = [str(month) for month in (monthly.get("months") or [])]
    by_dep = ecu.get("monthly_by_deployment") or {}
    if not labels or not isinstance(by_dep, dict):
        return empty
    partial = {str(month) for month in (monthly.get("partial_months") or [])}
    chosen = [(index, label) for index, label in enumerate(labels) if label not in partial][-n:]
    if not chosen:
        return empty
    diagnostic = (acc.get("sizing") or {}).get("ingest_source") == "diagnostics"
    excluded = _excluded_names(acc)
    priced_names: list[str] = []
    unpriced: list[dict] = []
    for dep in (acc.get("solutions") or {}).get("by_deployment") or []:
        if not isinstance(dep, dict) or not dep.get("name") or dep.get("platform") == "serverless":
            continue
        name = str(dep["name"])
        if name in excluded:
            continue
        series = by_dep.get(name) or []
        values = [float(series[index] or 0) if index < len(series) else 0.0 for index, _label in chosen]
        average = round(sum(values) / len(values), 2) if values else 0.0
        reason = unpriced_reason(dep, by_solution, diagnostic)
        if reason is None:
            priced_names.append(name)
        else:
            unpriced.append({"name": name, "reason": reason, "avg_mo": average})
    if not priced_names:
        unpriced.sort(key=lambda row: (-row["avg_mo"], row["name"]))
        return {
            "months": [],
            "average": None,
            "comparison": None,
            "comparison_basis": None,
            "stable": None,
            "unpriced": unpriced,
            "unpriced_avg_mo": round(sum(row["avg_mo"] for row in unpriced), 2) if unpriced else 0.0,
            "basis": "monthly_by_deployment",
        }
    months = []
    for index, label in chosen:
        total = 0.0
        for name in priced_names:
            series = by_dep.get(name) or []
            if index < len(series):
                total += float(series[index] or 0)
        months.append({"month": label, "cost": round(total, 2)})
    average = round(sum(row["cost"] for row in months) / len(months), 2) if months else None
    latest_cost = months[-1]["cost"] if months else None
    stable = None
    if len(months) >= 2 and latest_cost is not None:
        stable = all(float(row["cost"]) >= float(latest_cost) * BILLING_WINDOW_MIN_SHARE for row in months)
    elif len(months) == 1:
        stable = True
    comparison = None
    comparison_basis = None
    if months and latest_cost is not None:
        comparison = average if stable else latest_cost
        comparison_basis = "three_complete_months" if stable and len(months) > 1 else "latest_complete_month"
    unpriced.sort(key=lambda row: (-row["avg_mo"], row["name"]))
    return {
        "months": months,
        "average": average,
        "comparison": comparison,
        "comparison_basis": comparison_basis,
        "stable": stable,
        "unpriced": unpriced,
        "unpriced_avg_mo": round(sum(row["avg_mo"] for row in unpriced), 2) if unpriced else 0.0,
        "basis": "monthly_by_deployment",
    }


def recent_ech_months(acc: dict, n: int = 3) -> dict:
    """The last `n` complete calendar months of billed ECH cost, oldest first.

    Cost is the K360 billing total for the month (1 ECU is $1). A partial month
    is not a month. Fewer than `n` complete months in the series gives what exists.
    """
    monthly = ((acc.get("ecu") or {}).get("monthly")) or {}
    partial = {str(m) for m in (monthly.get("partial_months") or [])}
    rows = [
        {"month": str(m), "cost": round(float(t or 0), 2)}
        for m, t in zip(monthly.get("months") or [], monthly.get("totals") or [])
        if str(m) not in partial
    ][-n:]
    avg = round(sum(r["cost"] for r in rows) / len(rows), 2) if rows else None
    return {"months": rows, "average": avg}


def build_consolidation(acc: dict, infra: dict | None) -> dict:
    sz = acc.get("sizing") or {}
    e = acc.get("ecu") or {}
    lookback_days = float(e.get("lookback_days") or 365)
    months = max(lookback_days / DAYS_PER_MONTH, 1.0)
    ecu_total = float(e.get("total_lookback") or 0)
    usage = float(e.get("quote_usage_factor") or 1.0)
    if e.get("quote_monthly_ecu"):
        ech_mo = float(e["quote_monthly_ecu"])
    else:
        ech_mo = float(e.get("monthly_avg_complete_months") or e.get("monthly_avg_all") or (ecu_total / months))
    stored_gb = float((acc.get("storage") or {}).get("data_stored_gb") or 0)
    con = sz.get("consolidated") or {}
    workload = acc.get("workload") or {}
    notes: list[str] = []
    asked = e.get("quote_lookback_requested")
    if asked and not e.get("quote_lookback_days"):
        notes.append(
            f"A {int(asked)}-day quote window was asked for, and this account has no daily billing series. "
            f"ECH is the complete-month average, {ech_mo:,.2f} ECU/mo, and the comparison is the whole org."
        )
    elif asked and int(asked) > int(e["quote_lookback_days"]):
        notes.append(
            f"A {int(asked)}-day quote window was asked for. The org has {int(e['quote_lookback_days'])} billed days in the fetch, so those were used."
        )
    if e.get("quote_lookback_days"):
        notes.append(
            f"Quote lookback is {int(e['quote_lookback_days'])} days. "
            f"ECH is that daily average × 30.416, {ech_mo:,.2f} ECU/mo. "
            + (
                "Ingest written to the estimators is the diagnostic bulk as measured. The billing window does not scale it."
                if float(e.get("quote_ingest_factor") if e.get("quote_ingest_factor") is not None else usage) == 1.0
                else f"Ingest written to the estimators is stored-GB ingest × {usage:.3f}, that pace divided by the complete-month pace."
            )
        )

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
        p.update({"stored_gb": round(gb, 1), "deployments": blk.get("deployments"), "sized_deployments": blk.get("sized_deployments"), "ech_ecu_mo": blk.get("ecu_monthly_avg")})
        if sz.get("ingest_source") == "diagnostics":
            p["daily_raw_gb"] = round(written_bulk, 3)
        by_solution[sol] = p
    quoted = _apply_search_estimator(by_solution, sz)
    search_telemetry = (workload.get("quote_window_telemetry") or {})
    # A fleet sheet can be absent when each region has its own notebook. Those read-backs are the quote.
    if search_telemetry.get("status") == "complete" and not quoted and not _solution_has_regional_readback(sz, "search"):
        search = by_solution.get("search") or {}
        if float(search.get("stored_gb") or 0) or float(search.get("total_mo") or 0):
            search["pricing"] = "rejected — the performant read-back is missing"
            search["total_mo"] = 0.0
            search["total_yr"] = 0.0
            notes.append(
                "Search was not quoted because the performant read-back is missing. The on-demand ladder is not the quote."
            )
    if search_telemetry.get("status") != "complete":
        search = by_solution.get("search") or {}
        search["pricing"] = "rejected — exact quote-window Search telemetry is unavailable"
        search["total_mo"] = 0.0
        search["total_yr"] = 0.0
        search["by_region"] = []
        notes.append(
            "Search was not quoted because Cloud Tracker index telemetry does not cover the exact billed quote window. "
            "Rerun once quote-window telemetry is available."
        )
        limits = sz.setdefault("diagnostics", {}).setdefault("estimation_limits", [])
        if not any(row.get("code") == "search_quote_window_telemetry" for row in limits):
            limits.append({
                "code": "search_quote_window_telemetry",
                "detail": search_telemetry.get("reason") or "exact quote-window index telemetry is missing",
                "severity": "high",
            })
    for sol in ("observability", "security"):
        est = ((sz.get("estimators") or {}).get(sol) or {})
        blk = by_solution[sol]
        readback = est.get("readback") or {}
        if sol == "observability" and readback and not est.get("readback_rejected"):
            gate = est.get("echo_gate") or est.get("g28_gate") or est.get("g26_gate")
            metered = readback.get("ingest_basis") == "metered" or (
                sz.get("ingest_source") == "diagnostics" and (
                    readback.get("metered_ex_metrics_gb_day") is not None or readback.get("metered_metrics_gb_day") is not None
                )
            )
            if metered and "written_logs_traces" not in (gate or {}):
                gate = observability_echo_gate((sz.get("consolidated") or {}).get("observability") or blk, readback)
            elif not gate:
                gate = echo_gate(float(blk.get("daily_raw_gb") or 0), ingest_echo(readback))
            if not gate.get("accepted"):
                blk["pricing"] = "rejected — the sheet echo differs from the written bulk"
                if gate.get("written_logs_traces") is not None:
                    notes.append(
                        f"Observability logs and traces echo {gate.get('sheet_logs_traces')} GB/day against {gate.get('written_logs_traces')} written, "
                        f"and metrics echo {gate.get('sheet_metrics')} against {gate.get('written_metrics')}. D38 was not quoted."
                    )
                else:
                    notes.append(
                        f"Observability sheet echo is {gate.get('sheet_gb_day')} GB/day against {gate.get('written_bulk')} GB/day written. D38 was not quoted."
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
                    str(readback.get("addon_note")) + " E74 was not quoted."
                    if readback.get("addon_note") else
                    f"Security month-12 total includes {addon} from template cross-project search (sheet E73). E74 was not quoted."
                )
            continue
        if est.get("readback_rejected"):
            blk["pricing"] = "rejected — " + str(est.get("readback_rejected"))
            notes.append(f"{sol.title()} read-back was not quoted: {est.get('readback_rejected')}")
            continue
        quoted = _apply_sheet_readback(by_solution, sz, sol) or quoted
    for sol, blk in by_solution.items():
        if _solution_has_regional_readback(sz, sol):
            continue
        _omit_unquoted(blk, notes, sol)
    svls_total_mo = sum(float(p.get("total_mo") or 0) for p in by_solution.values())
    region_groups = _deployment_groups(acc, infra, months)
    for group in region_groups:
        if not group.get("mix_error"):
            continue
        names = ", ".join(str(name) for name in (group.get("deployments") or []))
        notes.append(
            f"Observability {group.get('region')} ({names}) was not quoted: {group['mix_error']}."
        )
    regional_quoted = _replicate_by_region(acc, by_solution, region_groups, notes)
    quoted = regional_quoted or quoted
    # Scope is independent of EIS dollars and provides the exact set of Search
    # deployments represented in the quote. Existing Serverless projects are
    # never part of the proposed migration line.
    scope = ech_scope(acc, by_solution)
    _apply_search_eis(
        acc,
        by_solution,
        months,
        notes,
        usage_factor=usage,
        eligible_names=_priced_search_names(acc, by_solution, scope),
    )
    search_eis = by_solution.get("search") or {}
    coverage = sz.get("diagnostics") or {}
    if (search_eis.get("eis_data_status") or {}).get("status") == "incomplete":
        limits = sz.setdefault("diagnostics", {}).setdefault("estimation_limits", [])
        if not any(row.get("code") == "eis_data_incomplete" for row in limits):
            limits.append({
                "code": "eis_data_incomplete",
                "detail": (search_eis.get("eis_data_status") or {}).get("reason") or "filtered EIS billing totals could not be verified; no EIS amount was added",
                "severity": "high",
            })
    svls_total_mo = sum(float(p.get("total_mo") or 0) for p in by_solution.values())
    quoted = any("quotable" in str(p.get("pricing") or "") for p in by_solution.values()) and svls_total_mo > 0
    scope = ech_scope(acc, by_solution)
    _apply_eis_to_plan(acc, float((by_solution.get("search") or {}).get("eis_with_support_mo") or 0))
    _sync_plan_to_quote(acc, by_solution)
    priced_months = recent_ech_months_priced(acc, by_solution)
    if isinstance(coverage, dict) and sz.get("ingest_source") == "diagnostics":
        deps = [
            dep for dep in (acc.get("solutions") or {}).get("by_deployment") or []
            if isinstance(dep, dict) and dep.get("platform") != "serverless"
        ]
        if coverage.get("retention_policy_bulk_gb_day") is None and any(dep.get("retention_basis") for dep in deps):
            coverage["retention_policy_bulk_gb_day"] = round(sum(
                float(dep.get("daily_raw_gb") or 0) for dep in deps if dep.get("retention_basis") == "policy"
            ), 3)
        for key in ("bulk_short_window_gb_day", "bulk_restart_gb_day"):
            if coverage.get(key) is None and any(dep.get(key) is not None for dep in deps):
                coverage[key] = round(sum(float(dep.get(key) or 0) for dep in deps), 3)
    # The window month for the whole org, before the comparison scope narrows it.
    ech_window_mo = ech_mo
    ech_comparison_available = True
    if scope and scope.get("status") == "unavailable":
        ech_comparison_available = False
        kept = (
            f" The serverless estimate stays the estimator read-back, ${svls_total_mo:,.2f}/mo."
            if quoted else
            " No estimator read-back was returned."
        )
        notes.append(
            "ECH deployment scope could not be measured from complete, reconciled daily Cloud Tracker billing. "
            f"The deployment-scoped ECH comparison is unavailable: {scope.get('reason') or 'daily deployment billing is unavailable'}."
            + kept
        )
    elif scope:
        ech_mo = float(scope["in_comparison_mo"])
        names = ", ".join(f"{row['name']} ({row['reason']})" for row in scope["unpriced"][:6])
        more = "" if len(scope["unpriced"]) <= 6 else f", and {len(scope['unpriced']) - 6} more"
        if scope["scope"] == "priced":
            lead = (
                f"ECH in the comparison is ${ech_mo:,.0f}/mo: the {scope['priced_deployments']} deployments that got a serverless price. "
                f"The window as billed is ${scope['window_mo']:,.0f}/mo."
            )
            left = []
            if scope["unpriced_deployments"]:
                left.append(f"{scope['unpriced_deployments']} deployments with no serverless price, ${scope['unpriced_mo']:,.0f}/mo ({names}{more})")
            if scope["serverless_mo"] > 0:
                left.append(f"existing serverless projects, ${scope['serverless_mo']:,.0f}/mo")
            if scope["other_mo"] > 0:
                left.append(f"lines with no deployment on the roster, ${scope['other_mo']:,.0f}/mo")
            notes.append(lead + (" Not compared: " + "; ".join(left) + "." if left else ""))
        else:
            lead = f"ECH in the comparison is ${ech_mo:,.0f}/mo, the whole billing window"
            lead += "." if scope["keep_serverless_spend"] or scope["serverless_mo"] <= 0 else f" less ${scope['serverless_mo']:,.0f}/mo of existing serverless projects."
            if scope["unpriced_deployments"]:
                lead += (
                    f" That still includes {scope['unpriced_deployments']} deployments with no serverless price, "
                    f"${scope['unpriced_mo']:,.0f}/mo ({names}{more}), so the saving is overstated by their bill."
                )
            notes.append(lead)
    ml_raw = (by_solution.get("search") or {}).get("ml_node_ecu_mo")
    ech_ml_excluded = None
    if ml_raw is not None and ech_comparison_available:
        ech_ml_excluded = round(float(ml_raw), 2)
        if ech_ml_excluded:
            ech_mo = round(max(0.0, float(ech_mo) - ech_ml_excluded), 2)
            notes.append(
                f"The ECH comparison excludes ${ech_ml_excluded:,.2f}/mo of quote-window ML-node ECU. "
                "That capacity stays out of the serverless total."
            )
    if quoted:
        notes.append(
            "Every serverless dollar comes from a named estimator: Search Serverless performant, or the Observability or Security Serverless estimator. "
            "Search EIS is billed inference ECU and tokens at list; ML-node ECU remains separate and is not attributed to EIS. "
            "ECH dollars are the K360 billing cost field, summed as billed. No ARR ÷ commit rate is applied. Serverless is at estimator list."
        )
    if sz.get("ingest_source") == "diagnostics":
        notes.extend(sz.get("notes") or [])
        notes.insert(0, (
            "Ingest is bulk GB/day from the Elasticsearch diagnostic bundles. K360 does not provide a raw ingest total. "
            "Its backup GB/day is stored GB divided by an assumed retention (default 3 months), which prices stored-byte turnover "
            "and can sit far below the bulk payload the cluster accepted. An active deployment with no bundle adds no ingest. "
            "A deployment with no bill in the quote window is left out, and its last snapshot is not carried in. "
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

    # ---- ECH dollars are the K360 billing `cost` field as billed. ARR and commit are
    # kept for reference. ARR ÷ (commit × 365) is not applied to anything: on real
    # accounts it came out above $1 per ECU and put the ECH bill above the ARR itself.
    org_meta = (acc.get("meta") or {}).get("org") or {}
    arr = float(org_meta.get("ARR") or 0)
    commit = float((e.get("contract") or {}).get("commit_pace_ecu_per_day") or 0)
    contract_rate = {
        "arr": round(arr, 2) if arr else None,
        "commit_ecu_per_day": commit or None,
        "implied_usd_per_ecu": None,
        "basis": "k360_cost",
        "note": "ECH dollars are the K360 billing cost field, summed as billed. No ARR ÷ commit rate is applied. Serverless stays at estimator list.",
    }

    # ---- savings: the estimator read-back is the serverless dollar.
    # The deployment-scoped difference needs a reconciled daily bill. The read-back does not.
    if quoted:
        savings = {
            "ech_list_mo": None,
            "serverless_mo": round(svls_total_mo, 2),
            "list_mo": None,
            "list_yr": None,
            "list_pct": None,
        }
        if ech_comparison_available and (ech_mo or ech_ml_excluded):
            save_list_mo = ech_mo - svls_total_mo
            savings.update({
                "ech_list_mo": round(ech_mo, 2),
                "list_mo": round(save_list_mo, 2),
                "list_yr": round(save_list_mo * 12, 2),
                "list_pct": round(100 * save_list_mo / ech_mo, 1) if ech_mo else None,
            })
    else:
        savings = {"ech_list_mo": round(ech_mo, 2) if ech_comparison_available else None, "serverless_mo": None, "list_mo": None, "list_yr": None, "list_pct": None}
    savings["ech_basis"] = "k360_cost"
    if ech_ml_excluded is not None:
        savings["ech_ml_excluded_mo"] = ech_ml_excluded

    # ---- per-deployment standalone pricing + fragmentation
    sol_deps = (acc.get("solutions") or {}).get("by_deployment") or []
    per: list[dict] = []
    for d in sol_deps:
        if d.get("platform") == "serverless":
            continue
        gb = float(d.get("gb") or 0)
        sol = d.get("solution") or "search"
        year_ecu = float(d.get("ecu") or 0)
        window_mo = ((scope or {}).get("by_deployment_mo") or {}).get(d["name"])
        # With per-deployment months, this is the deployment's own part of the quote window. Without
        # them it is its lookback average at the fleet's window pace, which is only an approximation.
        ecu_mo = float(window_mo) if window_mo is not None else year_ecu / months * usage
        pace = (ecu_mo / year_ecu) if (window_mo is not None and year_ecu > 0) else (usage / months)
        ti = tiers_by_name.get(d["name"], {})
        t = ti.get("tiers") or {}
        fixed_mo = round(sum(v for k, v in t.items() if k in FIXED_TIERS) * pace, 2) if t else None
        per.append({
            "name": d["name"], "solution": sol, "stored_gb": round(gb, 1),
            "ech_ecu_mo": round(ecu_mo, 2), "ech_ecu_lookback": round(float(d.get("ecu") or 0), 2),
            "fixed_overhead_ecu_mo": fixed_mo,
            "overhead_pct": round(100 * fixed_mo / ecu_mo, 1) if fixed_mo is not None and ecu_mo > 0 else None,
            "ml_ecu_mo": round(float(t.get("ml") or 0) * pace, 2) if t else None,
            "zones": ti.get("zones"), "gb_known": gb > 0,
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
    if fragmentation["ech_ecu_mo"]:
        fragmentation["fixed_overhead_pct"] = round(100 * fragmentation["fixed_overhead_ecu_mo"] / fragmentation["ech_ecu_mo"], 1)
    if n_with_gb < len(per):
        if gaps:
            notes.append(f"{len(per) - n_with_gb} deployments have no size on the snapshot day. {n_moved} of them now report size under a sibling name that is already in the stored-GB total. The rest have no current size. All stay in the ECU totals.")
        else:
            notes.append(f"{len(per) - n_with_gb} deployment(s) have no stored GB: not sized, or no storage row. They stay in the roster ECU totals and out of the stored-GB total, and they are not given a serverless price.")

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
    quote_days = e.get("quote_lookback_days")
    if quote_days:
        scen = [(f"{int(quote_days)}-day lookback (estimator basis)", 1.0)]
    else:
        scen = [("Lookback-average fleet (estimator basis)", 1.0)]
    # Historical billing-pace sensitivities belong to the measured diagnostic quote only.
    # They do not describe or scale the assumption-based K360 stored-GB scenario.
    if sz.get("ingest_source") == "diagnostics":
        low = e.get("monthly_low") or {}
        if low.get("ecu") and ech_window_mo:
            scen.append((f"Quietest month {low.get('month')}", float(low["ecu"]) / ech_window_mo))
        complete = e.get("monthly_avg_complete_months")
        if quote_days and complete and ech_window_mo and abs(float(complete) - ech_window_mo) > 1:
            scen.append(("Complete-month average", float(complete) / ech_window_mo))
        elif not quote_days and e.get("last_30d_daily_avg") and ech_window_mo:
            scen.append(("Last 30 days run-rate", float(e["last_30d_daily_avg"]) * DAYS_PER_MONTH / ech_window_mo))
    eis_full = float((by_solution.get("search") or {}).get("eis_with_support_mo") or 0)
    eis_usage = float((by_solution.get("search") or {}).get("eis_mo") or 0)
    for label, k in scen:
        base = (svls_total_mo - eis_full) if svls_total_mo else 0.0
        svl_k = round(base * k + eis_full, 2) if svls_total_mo else None
        fleet_rows.append({
            "scenario": label,
            "scale": round(k, 3),
            "stored_gb": round(stored_gb * k, 1),
            "ech_ecu_mo": round(ech_mo * k, 2) if ech_comparison_available else None,
            "serverless_mo": svl_k,
            "eis_mo": round(eis_usage, 2) if eis_usage else None,
            "save_vs_list_mo": round(ech_mo * k - svl_k, 2) if svl_k is not None and ech_comparison_available else None,
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
        label = "Search Serverless, performant" if sol == "search" else f"{sol.title()} Serverless"
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
    # A Search line's monthly carries EIS. Its annual figures have to carry it too.
    if (by_solution.get("search") or {}).get("eis_with_support_mo"):
        for line in quotes:
            if line["solution"] == "search" and line.get("monthly") is not None:
                line["monthly_unrounded"] = float(line["monthly"])
                line["annual_year1"] = line["annual_steady_state"] = round(float(line["monthly"]) * 12, 2)
    recon = {"kind": "readbacks", "quotes": quotes, "total_mo": round(svls_total_mo, 2), "quotable": "estimator read-backs"} if quotes else None
    integrity = _integrity(acc, sz, by_solution, quotes, svls_total_mo, scope, priced_months)

    callouts: list[dict] = []
    if per:
        top = per[0]
        oh = f"{top['overhead_pct']}% fixed overhead" if top.get("overhead_pct") is not None else "fixed overhead not available"
        gb_bit = f"{top['stored_gb']:,.0f} GB" if top["gb_known"] else (f"its size now reports under {top['size_now_on']}" if top.get("size_now_on") else "no current size")
        callouts.append({"tone": "info", "title": f"Largest deployment: {top['name']}", "detail": f"{top['ech_ecu_mo']:,.0f} ECU/mo, {gb_bit}, {oh}. Deployments are not priced alone. The estimate is one project per solution in each region."})
    active_fixed = fragmentation.get("fixed_overhead_ecu_mo") or 0
    if active_fixed:
        quote_days = int(e["quote_lookback_days"]) if e.get("quote_lookback_days") else None
        pace = f" on the last {quote_days} billed days" if quote_days else ""
        inactive_n = (sz.get("diagnostics") or {}).get("inactive_excluded") or 0
        year_fixed = composition.get("fixed_overhead_mo")
        year_bit = ""
        if year_fixed and abs(float(year_fixed) - float(active_fixed)) > 1:
            year_bit = f" The full lookback is {float(year_fixed):,.0f} ECU/mo, {composition.get('fixed_overhead_pct')}% of that year."
            if inactive_n:
                word = "deployment" if int(inactive_n) == 1 else "deployments"
                year_bit = year_bit[:-1] + f", including {int(inactive_n)} {word} with no bill in the quote window."
        callouts.append({
            "tone": "success",
            "title": f"Fixed overhead is {active_fixed:,.0f} ECU/mo",
            "detail": (
                f"{fragmentation.get('fixed_overhead_pct')}% of the active deployments' ECU{pace}. "
                f"That is Fixed ECU/mo added across all {fragmentation.get('n_deployments')} active deployments. "
                "These are master, Kibana, coordinating, integrations, enterprise search, and APM. "
                "A serverless project does not bill those roles."
                + year_bit
            ),
        })
    if tail_block["n"] >= 8:
        callouts.append({
            "tone": "info",
            "title": f"{tail_block['n']} small deployments bill {tail_block['ech_ecu_mo']:,.0f} ECU/mo",
            "detail": (
                f"That is {tail_block['ech_share_pct']}% of ECU and {tail_block.get('data_share_pct')}% of stored data. "
                "It is their whole bill, not the fixed-overhead figure. "
                f"Fixed overhead on these {tail_block['n']} is {tail_block['fixed_overhead_ecu_mo']:,.0f} ECU/mo. "
                f"Merging them, if a quarter of their bill goes away, recovers about {tail_block['ech_consolidation_saving_mo']:,.0f} ECU/mo."
            ),
        })

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
        "basis": {
            "ech_monthly_ecu": round(ech_mo, 2),
            "ech_window_ecu": round(ech_window_mo, 2),
            "lookback_days": lookback_days,
            "quote_lookback_days": int(e["quote_lookback_days"]) if e.get("quote_lookback_days") else None,
            "stored_gb": round(stored_gb, 1),
            "deployments": len(per),
            "retention_months": {k: (v or {}).get("retention_months") for k, v in con.items()},
        },
        "source_precedence": sz.get("source_precedence") or coverage.get("source_precedence"),
        "ech_composition": composition,
        "serverless": serverless,
        "contract_rate": contract_rate,
        "savings": savings,
        "ech_recent_months": recent_ech_months(acc),
        "ech_recent_months_priced": priced_months,
        "estimate_months": 12,
        "ech_scope": scope,
        "ladder": [] if quoted else (_ladder_walk(metered) if metered else []),
        "metered_expansion": 1.0 if sz.get("ingest_source") == "diagnostics" else O11Y_METER_FACTOR,
        "metered_rates": observability_metered_rates(((acc.get("solutions") or {}).get("by_deployment") or []), 1.0 if sz.get("ingest_source") == "diagnostics" else O11Y_METER_FACTOR) if (sz.get("ingest_source") == "diagnostics") else [],
        "deployment_plan": _deployment_bars(sz, by_solution, notes),
        "deployment_plan_floor": int((sz.get("deployment_plan") or {}).get("floor") or 1),
        "per_cluster_plan": per_cluster_plan(sz, acc, infra) if sz.get("deployment_plan") and not (sz.get("deployment_plan") or {}).get("error") else None,
        "search_power": _search_power(sz, by_solution, acc),
        "fragmentation": fragmentation,
        "by_deployment_basis": "daily_deployment_billing" if (scope or {}).get("status") == "complete" else "lookback_at_window_pace",
        "architecture": architecture,
        "by_deployment": per[:TOP_N],
        "tail": tail_block,
        "overhead_buckets": buckets,
        "highest_overhead_deployments": [{"name": r["name"], "fixed_overhead_pct": round(100 * r["fixed_overhead_ecu_mo"] / r["ech_ecu_mo"], 1), "ech_ecu_mo": r["ech_ecu_mo"]} for r in highest_overhead],
        "sensitivity": {
            "retention": retention_rows,
            "retention_basis": {
                "policy_bulk_gb_day": (coverage or {}).get("retention_policy_bulk_gb_day"),
                "bulk_gb_day": (coverage or {}).get("bulk_gb_day"),
                "detail": "Share of bulk with a configured retention policy. Assumed 30-day store is not measured, and retention does not change the headline.",
            },
            "fleet": fleet_rows,
        },
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
        ech = s.get("ech_list_mo")
        ech_bit = f"${ech:,.0f}/mo" if isinstance(ech, (int, float)) else "unavailable"
        print(f"consolidation: ECH billed cost {ech_bit} · no estimator read-back")
    elif s.get("list_mo") is None:
        print(f"consolidation: serverless ${s['serverless_mo']:,.0f}/mo · deployment-scoped comparison unavailable")
    else:
        side = "above" if float(s["list_mo"]) < 0 else "below"
        print(f"consolidation: ECH billed cost ${s['ech_list_mo']:,.0f}/mo → serverless ${s['serverless_mo']:,.0f}/mo · ${abs(float(s['list_mo'])):,.0f}/mo {side} ({abs(float(s['list_pct']))}%)")


if __name__ == "__main__":
    main()
