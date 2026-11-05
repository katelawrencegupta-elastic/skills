#!/usr/bin/env python3
"""Build the diagnostics copy of an account analysis.

ECH ECU, contract rate, regions, and the Search query window stay on the K360 bill.
Ingest GB/day and retention come from the diagnostic rows. Primary store size replaces
K360 stored GB where a bundle matched the deployment name. A deployment with no bundle
keeps its K360 size and the run's assumed retention, and is counted as a gap.

The copy is what the default sizing run reads. It does not replace the K360
account JSON. The K360 stored-GB back-calculation is the backup (--k360-ingest),
because K360 does not provide a raw ingest total.

Only deployments with a bill in the latest lookback month are sized. A
deployment with no bill that month is left out, and its last telemetry
snapshot is not added to stored GB.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import re
from pathlib import Path

DAYS_PER_MONTH = 30.416
RETENTION_MIN = 1.0
RETENTION_MAX = 36.0


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _clamp_months(months: float) -> float:
    return min(RETENTION_MAX, max(RETENTION_MIN, float(months)))


def _sheet_months(months: float) -> int:
    """Whole months, 1–36. Round up so the estimator's integer retention row matches."""
    clamped = _clamp_months(months)
    whole = math.ceil(clamped - 1e-9)
    return int(min(RETENTION_MAX, max(RETENTION_MIN, whole)))


def apply_diagnostics(acc: dict, rows: list[dict]) -> dict:
    """Return a deep copy whose sizing inputs are the diagnostic measurements."""
    out = copy.deepcopy(acc)
    by_exact = {r["name"]: r for r in rows if r.get("name")}
    by_norm = {_norm(r["name"]): r for r in rows if r.get("name")}
    used: set[str] = set()

    def match(name: str) -> dict | None:
        hit = by_exact.get(name) or by_norm.get(_norm(name))
        if hit and hit.get("name"):
            used.add(hit["name"])
        return hit

    sz = out.setdefault("sizing", {})
    con = sz.get("consolidated") or {}
    assumed = {
        "observability": float((con.get("observability") or {}).get("retention_months") or 3),
        "security": float((con.get("security") or {}).get("retention_months") or 3),
        "search": float((con.get("search") or {}).get("retention_months") or 3),
    }
    short: list[dict] = []
    matched = 0
    fallback = 0
    inactive = 0
    inactive_bundles: list[str] = []
    dropped_gb = 0.0
    active_info = (out.get("deployments") or {}).get("active") or {}
    active_names = set(active_info.get("names") or [])
    use_active = bool(active_info.get("month")) and bool(active_names)
    for dep in (out.get("solutions") or {}).get("by_deployment") or []:
        if dep.get("platform") == "serverless":
            continue
        sol = dep.get("solution") or "search"
        name = dep.get("name") or ""
        if use_active and name not in active_names:
            inactive += 1
            dropped_gb += float(dep.get("gb") or 0)
            hit = match(name)
            if hit:
                inactive_bundles.append(hit["name"])
            dep["gb"] = 0.0
            dep["daily_raw_gb"] = 0.0
            dep["retention_days"] = 0
            dep["ingest_source"] = "inactive"
            continue
        hit = match(name)
        if hit:
            matched += 1
            dep["gb"] = round(float(hit.get("store_gb") or 0), 2)
            dep["daily_raw_gb"] = round(float(hit.get("bulk_gb_day") or 0), 3)
            days = hit.get("retention_days")
            if not days or float(days) <= 0:
                days = assumed[sol] * DAYS_PER_MONTH
                dep["retention_unmeasured"] = True
            dep["retention_days"] = round(float(days), 1)
            dep["retention_basis"] = hit.get("retention_basis")
            dep["retention_label"] = hit.get("retention_label")
            dep["ingest_source"] = "diagnostics"
            dep["diag_id"] = hit.get("id")
            up = hit.get("uptime_days_min")
            dep["uptime_days_min"] = up
            if up is not None and float(up) < 2:
                short.append({"name": dep["name"], "uptime_days": up})
        else:
            fallback += 1
            dep["daily_raw_gb"] = 0.0
            dep["ingest_source"] = "active_no_bundle"

    ech = [
        d for d in (out.get("solutions") or {}).get("by_deployment") or []
        if d.get("platform") != "serverless" and d.get("ingest_source") != "inactive"
    ]
    totals = (out.get("solutions") or {}).get("totals") or {}
    clamped_notes: list[str] = []
    for sol in ("observability", "security", "search"):
        blk = dict(con.get(sol) or {})
        group = [d for d in ech if (d.get("solution") or "search") == sol]
        daily = sum(float(d.get("daily_raw_gb") or 0) for d in group)
        stored = sum(float(d.get("gb") or 0) for d in group)
        weighted = sum(float(d.get("daily_raw_gb") or 0) * float(d.get("retention_days") or 0) for d in group)
        measured = (weighted / daily / DAYS_PER_MONTH) if daily else assumed[sol]
        written = _sheet_months(measured)
        if abs(float(written) - measured) > 0.05:
            clamped_notes.append(f"{sol} retention measures {measured:.1f} months and is written to the estimator as {written}")
        blk["deployments"] = len(group)
        ecu_map = active_info.get("ecu_complete_month") or {}
        if use_active and ecu_map:
            blk["ecu_monthly_avg"] = round(sum(float(ecu_map.get(d["name"]) or 0) for d in group), 2)
        blk["stored_gb"] = round(stored, 2)
        blk["daily_raw_gb"] = round(daily, 2)
        blk["retention_months"] = written
        blk["retention_months_measured"] = round(measured, 2)
        blk["gb_estimated_from_ecu_share"] = False
        blk["has_workload"] = bool(stored > 0 or daily > 0 or (blk.get("ecu_lookback") or 0))
        blk["ingest_source"] = "diagnostics"
        if sol == "observability":
            blk["logs_gb_day"] = round(daily * 0.90, 2)
            blk["metrics_gb_day"] = round(daily * 0.10, 2)
        elif sol == "security":
            blk["siem_gb_day"] = round(daily, 2)
        if totals.get(sol) is not None:
            totals[sol]["gb"] = round(stored, 2)
            if use_active:
                totals[sol]["deployments"] = len(group)
        con[sol] = blk

    stored_total = round(sum(float(d.get("gb") or 0) for d in ech), 2)
    st = out.setdefault("storage", {})
    if use_active:
        kept = {d["name"] for d in ech}
        st["by_deployment"] = [r for r in (st.get("by_deployment") or []) if r.get("name") in kept]
        st["deployments_with_telemetry"] = len(st["by_deployment"])
        docs = sum(float(r.get("docs_count") or 0) for r in st["by_deployment"])
        st["docs_count"] = int(docs) if docs == int(docs) else docs
        gaps = [g for g in (st.get("size_gaps") or []) if g.get("name") in kept]
        st["size_gaps"] = gaps
        out["solutions"]["by_deployment"] = [
            d for d in (out.get("solutions") or {}).get("by_deployment") or []
            if d.get("platform") == "serverless" or d.get("ingest_source") != "inactive"
        ]
    st["data_stored_gb"] = stored_total
    st["data_stored_tb"] = round(stored_total / 1024, 2)
    by_name = {d["name"]: d for d in ech}
    for row in st.get("by_deployment") or []:
        src = by_name.get(row.get("name"))
        if src and src.get("ingest_source") == "diagnostics":
            row["data_stored_gb"] = src.get("gb")
    unmatched_diags = [r.get("name") for r in rows if r.get("name") not in used]
    coverage = {
        "bundles": len(rows),
        "matched": matched,
        "k360_fallback": fallback,
        "unmatched_diagnostics": unmatched_diags[:12],
        "short_uptime": short,
        "bulk_gb_day": round(sum(float(r.get("bulk_gb_day") or 0) for r in rows if r.get("name") in used and r.get("name") not in inactive_bundles), 2),
        "store_gb": round(sum(float(r.get("store_gb") or 0) for r in rows if r.get("name") in used and r.get("name") not in inactive_bundles), 2),
        "retention_clamped": clamped_notes,
        "active_month": active_info.get("month"),
        "active_month_partial": bool(active_info.get("partial")),
        "active_deployments": len(active_names) if use_active else None,
        "ecu_month": active_info.get("ecu_month"),
        "inactive_excluded": inactive,
        "inactive_snapshot_gb": round(dropped_gb, 2),
        "inactive_bundle_count": len(inactive_bundles),
        "inactive_bundles": inactive_bundles[:12],
    }
    notes = [
        "Ingest GB/day is the write-index sum of index.primaries.bulk.est_bytes_per_day from the Elasticsearch diagnostic bundle. "
        "Retention is the size-weighted ILM delete age, or the oldest index age when no delete policy covers the stored data, rounded up to a whole month for the estimator. "
        "Those two are summed by solution and by region. Search disk is the diagnostic store size. "
        "Search query rates stay the K360 workload window. ECH ECU stays the K360 bill.",
    ]
    if short:
        names = ", ".join(f"{s['name']} ({s['uptime_days']}d)" for s in short[:8])
        notes.append(f"Nodes up for under 2 days: {names}. The bulk rate is still the index write phase.")
    if use_active:
        month = active_info.get("month")
        partial = " That month is partial." if active_info.get("partial") else ""
        notes.insert(0, (
            f"Sized {len(active_names)} deployments with a bill in {month}.{partial} "
            f"{inactive} deployments had no bill that month. Their last stored snapshot, {dropped_gb:,.0f} GB, is not carried into the sizing."
        ))
        if active_info.get("partial") and active_info.get("ecu_month"):
            notes.append(f"ECU/mo on the sizing row is {active_info['ecu_month']} billing for those deployments, the last complete month.")
    if inactive_bundles:
        notes.append(f"{len(inactive_bundles)} diagnostic bundles belong to deployments with no bill in {active_info.get('month')} and are not priced.")
    if fallback:
        noun = "deployment" if fallback == 1 else "deployments"
        verb = "has" if fallback == 1 else "have"
        adds = "It adds" if fallback == 1 else "They add"
        notes.append(
            f"{fallback} active {noun} {verb} no diagnostic bundle. {adds} no ingest. "
            "Current stored GB stays; a decommissioned snapshot is not added."
        )
    if unmatched_diags:
        notes.append(f"{len(unmatched_diags)} diagnostic bundles did not match a billed deployment name and are not in the estimate.")
    notes.extend(clamped_notes)
    sz["consolidated"] = con
    sz["ingest_source"] = "diagnostics"
    sz["diagnostics"] = coverage
    sz["estimators"] = {}
    sz["notes"] = notes
    out["sizing"] = sz
    # The copied consolidation still describes the K360 inputs. The diagnostics
    # run builds its own after the estimators are filled.
    out.pop("consolidation", None)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-json", required=True)
    ap.add_argument("--diags-json", required=True, help="sizing_from_diags.json")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    acc = json.loads(Path(args.account_json).read_text())
    rows = json.loads(Path(args.diags_json).read_text())
    out = apply_diagnostics(acc, rows)
    Path(args.out).write_text(json.dumps(out, indent=2))
    cov = out["sizing"]["diagnostics"]
    print(f"wrote {args.out} matched={cov['matched']} fallback={cov['k360_fallback']} bulk={cov['bulk_gb_day']} GB/day")


if __name__ == "__main__":
    main()
