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

import datetime as dt
import sys
from pathlib import Path

from quote_checks import (
    BILLING_WINDOW_MIN_SHARE,
    e73_blocks_quote,
    ingest_echo,
    integrity_score,
    money as _money,
    region_match_how,
)
from skill_paths import find_skill

from constants import DAYS_PER_MONTH, ESTIMATE_MONTHS, O11Y_METER_FACTOR
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


def _window_daily_avg(ecu: dict, days: int, series: tuple[list[str], list[float]] | None = None) -> float | None:
    """Daily average over the last `days` billed days, or over all of them when there are fewer.

    `series` is the already-computed `billed_series`, so one caller does not rebuild it.
    """
    _labels, totals = series if series is not None else billed_series(ecu)
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
    # The billed series is the same list for every use below. Build it once.
    series = billed_series(ecu)
    labels, totals = series
    daily = _window_daily_avg(ecu, window, series)
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
    billed_days = len(totals)
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

    def price_o11y_serverless(daily_raw_gb: float, retention_months: float, *, meter_factor: float = O11Y_METER_FACTOR, support_uplift: float = 0.15) -> dict:
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
    # One map, not a scan per day. `.index` also silently picked the first of a repeated
    # label, which would have mapped two window days onto one column.
    position = {day: index for index, day in enumerate(labels)}
    window_indices = [position[day] for day in window_labels]

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


def comparison_deployment_names(scope: dict | None) -> set[str]:
    """ECH deployments whose billed cost is on the ECH side of the comparison.

    Under `--ech-scope priced` that is the priced set; under `all` it is every ECH
    deployment on the roster. Existing serverless projects are never in it. An
    unavailable scope has no comparison, so the set is empty.
    """
    if not scope or scope.get("status") != "complete":
        return set()
    names = {str(name) for name in (scope.get("by_deployment_mo") or {})}
    if scope.get("scope") == "priced":
        names -= {str(row.get("name")) for row in (scope.get("unpriced") or []) if row.get("name")}
    return names


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
    # build_consolidation runs more than once per run. This adds to the bars, so a second
    # pass over the same plan would price EIS twice. Today each pass re-reads the account
    # from disk, which hides it; the mark makes the function safe on its own.
    if plan.get("eis_applied"):
        return
    plan["eis_applied"] = amount
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
                # Default is one year. --retention-days writes another whole month, measured or not.
                from diag_inputs import ESTIMATE_MONTHS
                row["retention_months"] = int((acc.get("meta") or {}).get("estimate_months") or ESTIMATE_MONTHS)
        groups.append(row)
    order = {"search": 0, "observability": 1, "security": 2}
    groups.sort(key=lambda g: (order.get(g["solution"], 9), -g["ech_ecu_mo"]))
    return groups
