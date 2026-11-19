"""Regional read-backs and integrity for one consolidation quote."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from consolidation_scope import (
    BILLING_WINDOW_MIN_SHARE, ESTIMATE_MONTHS, _clear_components, _money, _stash_model,
    e73_blocks_quote, ingest_echo, integrity_score, region_match_how, unpriced_reason,
)

def es3_estimator_python() -> str:
    """Interpreter the es3 notebook library runs under."""
    return os.environ.get("ES3_ESTIMATOR_PY") or str(Path.home() / ".config/es3-estimator/venv/bin/python")


def es3_estimator_missing() -> str | None:
    """The path, when the es3 interpreter is not installed. None when it is."""
    py = es3_estimator_python()
    return None if Path(py).is_file() else py


def _es3_region(payload: dict, failures: list[dict] | None = None) -> dict | None:
    """Re-estimate one region on es3. `failures` collects why a region went unpriced.

    A missing interpreter and an estimator that ran and failed both left this
    returning None, so a region could go unpriced for an environment reason that
    never reached the quote. The reason is now recorded.
    """
    def failed(code: str, detail: str) -> None:
        if failures is not None and not any(row.get("code") == code for row in failures):
            failures.append({"code": code, "detail": detail})

    py = es3_estimator_python()
    if not Path(py).is_file():
        failed(
            "es3_estimator_missing",
            f"the es3 estimator interpreter is missing at {py}, so a Search region could not be re-estimated "
            "and was priced as a stored-GB share or left unquoted. Set ES3_ESTIMATOR_PY or install it "
            "(see references/setup.md).",
        )
        return None
    try:
        proc = subprocess.run(
            [py, str(Path(__file__).with_name("es3_estimate.py"))],
            input=json.dumps(payload), text=True, capture_output=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        failed("es3_estimator_failed", f"the es3 estimator could not be run ({exc!r}), so a Search region was not re-estimated.")
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        failed("es3_estimator_failed", f"the es3 estimator exited {proc.returncode} for a Search region: {tail[0][:200]}")
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        failed("es3_estimator_failed", "the es3 estimator returned output that is not JSON, so a Search region was not re-estimated.")
        return None


def _saved_projects(acc: dict) -> dict:
    rows = ((acc.get("sizing") or {}).get("estimators") or {}).get("projects") or []
    return {
        (p.get("solution"), p.get("region")): p
        for p in rows
        if p.get("solution") and p.get("region") and p.get("role") != "measured_retention"
    }


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
    es3_failures: list[dict] = []
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
                if saved_rb and search_how in {"nearest", "none", "unknown"}:
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
                }, es3_failures)
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
            if saved_hit.get("readback") and how in {"nearest", "none", "unknown"}:
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
    if es3_failures:
        limits = (acc.setdefault("sizing", {}).setdefault("diagnostics", {})
                  .setdefault("estimation_limits", []))
        for row in es3_failures:
            if not any(isinstance(item, dict) and item.get("code") == row["code"] for item in limits):
                limits.append({**row, "severity": "high"})
            notes.append(row["detail"])
    return regional_quoted


def _eis_billed_context(acc: dict, months: float) -> float | None:
    """Fetched-period EIS ECU divided by the quote span. Context only, not a price."""
    rows = ((acc.get("ai_ml") or {}).get("billing_by_deployment")) or {}
    if not isinstance(rows, dict):
        return None
    total = 0.0
    seen = False
    for raw in rows.values():
        if not isinstance(raw, dict) or raw.get("eis_ecu") is None:
            continue
        try:
            total += float(raw["eis_ecu"])
        except (TypeError, ValueError):
            continue
        seen = True
    if not seen:
        return None
    span = float(months or 0) or 1.0
    return round(total / span, 2)


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
        rec["evidence"] += f" That Search total includes EIS at ${float(eis_mo):,.2f}/mo from billed inference ECU and tokens; ML capacity carried over from ECH ML nodes is a separate line."
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
            "retention_assumed_max_share": coverage.get("retention_assumed_max_share"),
            "retention_policy_within_threshold_gb_day": coverage.get("retention_policy_within_threshold_gb_day"),
            "bulk_short_window_gb_day": coverage.get("bulk_short_window_gb_day"),
            "bulk_restart_gb_day": coverage.get("bulk_restart_gb_day"),
            "unmeasured_bulk": unmeasured,
            "unrated": coverage.get("unrated"),
            "estimation_limits": coverage.get("estimation_limits") or [],
        },
        # The month written to every Observability and Security sheet, and whether an index
        # average set it. The K360 backup prices an assumed month by design, so it is n/a there.
        "retention_month": None if sz.get("ingest_source") != "diagnostics" else {
            "months": int((acc.get("meta") or {}).get("estimate_months") or ESTIMATE_MONTHS),
            "index_count": (acc.get("meta") or {}).get("retention_index_count"),
            "index_days": (acc.get("meta") or {}).get("retention_index_days"),
            "explicit_days": (acc.get("meta") or {}).get("retention_days"),
            "has_bulk": float((coverage or {}).get("bulk_gb_day") or 0) > 0,
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
            "names": [row["name"] for row in (scope.get("unpriced") or []) if not row.get("stays_on_ech")][:6],
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
    # A deployment billed on ECH with no diagnostic bundle has a cost but no validated price. Its months
    # ride beside the priced ones so the cover can show the cost greyed out. They never enter `cost`.
    unvalidated_names = [row["name"] for row in unpriced if row["reason"] == "no diagnostic bundle"]
    # Workloads in a region Serverless is not offered in keep billing on ECH. Their months ride beside the
    # priced ones so the cover can stack them as the ECH that carries over.
    stays_names = [row["name"] for row in unpriced if str(row["reason"]).startswith("region not offered")]
    for row in unpriced:
        row["unvalidated"] = row["name"] in unvalidated_names
    months = []
    for index, label in chosen:
        total = 0.0
        for name in priced_names:
            series = by_dep.get(name) or []
            if index < len(series):
                total += float(series[index] or 0)
        grey = 0.0
        for name in unvalidated_names:
            series = by_dep.get(name) or []
            if index < len(series):
                grey += float(series[index] or 0)
        stay = 0.0
        for name in stays_names:
            series = by_dep.get(name) or []
            if index < len(series):
                stay += float(series[index] or 0)
        months.append({"month": label, "cost": round(total, 2), "unvalidated": round(grey, 2), "stays_on_ech": round(stay, 2)})
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
        "stays_on_ech_names": stays_names,
        "unvalidated": {
            "names": unvalidated_names,
            "avg_mo": round(sum(row["avg_mo"] for row in unpriced if row["unvalidated"]), 2),
        },
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

