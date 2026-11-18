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

from constants import DAYS_PER_MONTH, ESTIMATE_MONTHS
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

from consolidation_scope import *  # noqa: F401,F403
from consolidation_regional import *  # noqa: F401,F403
from consolidation_eis import *  # noqa: F401,F403

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
        if "quotable" in str(search_eis.get("pricing") or ""):
            notes.append("Search is quoted without EIS.")
            context = _eis_billed_context(acc, months)
            if context is not None:
                notes.append(f"Inference billed on ECH, not priced on serverless: ${context:,.2f}/mo.")
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
            scen.append(("ECH billing pace applied to the estimate (illustrative)", float(low["ecu"]) / ech_window_mo))
        complete = e.get("monthly_avg_complete_months")
        if quote_days and complete and ech_window_mo and abs(float(complete) - ech_window_mo) > 1:
            scen.append(("ECH billing pace applied to the estimate (illustrative)", float(complete) / ech_window_mo))
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
            "note": "Bulk was measured once. This row only scales that measurement." if "illustrative" in label else None,
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
    measured_ages = [float(g["retention_months_measured"]) for g in region_groups if g.get("retention_months_measured") is not None]
    if measured_ages:
        shown = round(sum(measured_ages) / len(measured_ages), 1)
        written_months = int((acc.get("meta") or {}).get("estimate_months") or ESTIMATE_MONTHS)
        if not (acc.get("meta") or {}).get("retention_index_count") and abs(shown - written_months) > 0.05:
            notes.append(f"Retention measured at {shown:g} months; the estimate prices {written_months}.")
    measured_projects = [
        p for p in ((sz.get("estimators") or {}).get("projects") or [])
        if isinstance(p, dict) and p.get("role") == "measured_retention"
    ]
    if measured_projects:
        monthly = 0.0
        priced_extra = True
        for project in measured_projects:
            value = ((project.get("readback") or {}).get("monthly_steady_state"))
            if value is None:
                priced_extra = False
                break
            monthly += float(value)
        if priced_extra:
            retention_rows.append({
                "retention_months": round(sum(measured_ages) / len(measured_ages), 1) if measured_ages else None,
                "label": "At measured retention",
                "serverless_mo": round(monthly, 2),
                "used": False,
            })

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
    for row in integrity.get("checks") or []:
        if row.get("warning") and row.get("detail"):
            notes.append(f"Warning: {row['detail']}")

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
        "estimate_months": int((acc.get("meta") or {}).get("estimate_months") or 12),
        "retention_days": (acc.get("meta") or {}).get("retention_days"),
        "retention_index_days": (acc.get("meta") or {}).get("retention_index_days"),
        "retention_index_count": (acc.get("meta") or {}).get("retention_index_count"),
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
