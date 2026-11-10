#!/usr/bin/env python3
"""Build the diagnostics copy of an account analysis.

ECH ECU, contract rate, regions, and the Search query window stay on the K360 bill.
Ingest GB/day and retention come from the diagnostic rows. Local primary store replaces
K360 stored GB where a bundle matched the deployment name. Searchable snapshots stay
out of that store. A deployment with no bundle keeps its K360 size and adds no ingest.
A missing retention is named and left out of the weighted month. It is not replaced
with an assumed retention.

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
COMPRESSIONCALC = "compressioncalc_accounted_for"
COMPRESSION_FORMULA = (
    "compressioncalc_accounted_for is write-index bulk GB/day multiplied by 0.60. "
    "On-disk bytes are not used for that GB/day. "
    "Observability and Security write this ingest. "
    "Search re-estimates local store multiplied by 0.60."
)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _clamp_months(months: float) -> float:
    return min(RETENTION_MAX, max(RETENTION_MIN, float(months)))


def _sheet_months(months: float) -> int:
    """Whole months, 1–36. Round up so the estimator's integer retention row matches.

    A remainder under 0.001 month is the 30.416 truncation, not another month.
    365 / 30.416 is 12.000263 and stays 12. A real extra day still rounds up.
    """
    months = float(months)
    whole = math.floor(months)
    if whole >= 1 and 0 <= (months - whole) < 0.001:
        months = float(whole)
    clamped = _clamp_months(months)
    whole = math.ceil(clamped - 1e-9)
    return int(min(RETENTION_MAX, max(RETENTION_MIN, whole)))


def _measured_days(value) -> float | None:
    """A retention age counts only when the bundle measured it. Zero and missing do not."""
    try:
        days = float(value)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return days


def _ech_rows(out: dict) -> list[dict]:
    return [
        d for d in (out.get("solutions") or {}).get("by_deployment") or []
        if d.get("platform") != "serverless" and d.get("ingest_source") != "inactive"
    ]


def _dep_bulk_gb(dep: dict) -> float:
    if dep.get("bulk_gb_day_raw") is not None:
        return float(dep.get("bulk_gb_day_raw") or 0)
    return float(dep.get("daily_raw_gb") or 0)


def _accounted_from_bulk(bulk: float) -> tuple[float, float | None]:
    """Second estimate: keep 60% of bulk. On-disk bytes are not used."""
    from admin_diags import COMPRESSION_RATIO

    if bulk <= 0:
        return 0.0, None
    return round(float(bulk) * COMPRESSION_RATIO, 3), COMPRESSION_RATIO


def _compression_fields(group: list[dict]) -> dict:
    """Bulk and compressioncalc_accounted_for. The ratio is 0.60 whenever bulk is above 0."""
    raw = sum(_dep_bulk_gb(dep) for dep in group)
    accounted, ratio = _accounted_from_bulk(raw)
    return {
        "bulk_gb_day": round(raw, 3),
        COMPRESSIONCALC: accounted,
        "compression_ratio": ratio,
    }


def _rollup_solutions(out: dict, clamped_notes: list[str]) -> None:
    """Rewrite each solution block from the deployment rows currently on the account."""
    sz = out.setdefault("sizing", {})
    con = sz.get("consolidated") or {}
    active_info = (out.get("deployments") or {}).get("active") or {}
    use_active = bool(active_info.get("month")) and bool(active_info.get("names"))
    ech = _ech_rows(out)
    totals = (out.get("solutions") or {}).get("totals") or {}
    for sol in ("observability", "security", "search"):
        blk = dict(con.get(sol) or {})
        group = [d for d in ech if (d.get("solution") or "search") == sol]
        daily = sum(float(d.get("daily_raw_gb") or 0) for d in group)
        stored = sum(float(d.get("gb") or 0) for d in group)
        weighed = [
            (abs(float(d.get("daily_raw_gb") or 0)), days)
            for d in group
            if (days := _measured_days(d.get("retention_days"))) is not None and float(d.get("daily_raw_gb") or 0) != 0
        ]
        weight_bulk = sum(rate for rate, _days in weighed)
        weighted = sum(rate * days for rate, days in weighed)
        measured = (weighted / weight_bulk / DAYS_PER_MONTH) if weight_bulk else None
        written = _sheet_months(measured) if measured is not None else None
        if written is not None and abs(float(written) - measured) > 0.05:
            clamped_notes.append(f"{sol} retention measures {measured:.1f} months and is written to the estimator as {written}")
        blk["deployments"] = len(group)
        ecu_map = active_info.get("ecu_complete_month") or {}
        if use_active and ecu_map:
            blk["ecu_monthly_avg"] = round(sum(float(ecu_map.get(d["name"]) or 0) for d in group), 2)
        blk["stored_gb"] = round(stored, 2)
        blk["daily_raw_gb"] = round(daily, 2)
        blk.pop("retention_months", None)
        blk.pop("retention_months_measured", None)
        if written is not None:
            blk["retention_months"] = written
            blk["retention_months_measured"] = round(measured, 2)
        blk["retention_unmeasured"] = bool(daily != 0 and written is None)
        if blk["retention_unmeasured"]:
            clamped_notes.append(f"{sol} has bulk and no measured retention, so its sheet is not filled.")
        blk["gb_estimated_from_ecu_share"] = False
        blk["has_workload"] = bool(stored > 0 or daily != 0 or (blk.get("ecu_lookback") or 0))
        blk["ingest_source"] = "diagnostics"
        blk.update(_compression_fields(group))
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
    sz["consolidated"] = con


def compressioncalc_account(acc: dict) -> dict:
    """Copy whose Observability and Security ingest is compressioncalc_accounted_for.

    That figure is bulk GB/day × 0.60. On-disk bytes are not used for that GB/day.
    Search keeps its measured store on this copy. The compression sizing multiplies
    that store by 0.60 when it re-estimates Search.
    """
    out = copy.deepcopy(acc)
    for dep in (out.get("solutions") or {}).get("by_deployment") or []:
        if dep.get("platform") == "serverless" or dep.get("ingest_source") == "inactive":
            continue
        if (dep.get("solution") or "search") == "search":
            continue
        raw = float(dep.get("daily_raw_gb") or 0)
        dep["bulk_gb_day_raw"] = round(raw, 3)
        dep["daily_raw_gb"] = round(float(dep.get(COMPRESSIONCALC) or 0), 3)
    clamped: list[str] = []
    _rollup_solutions(out, clamped)
    sz = out.setdefault("sizing", {})
    sz["ingest_basis"] = COMPRESSIONCALC
    prior = (acc.get("sizing") or {}).get("estimators") or {}
    kept = [p for p in (prior.get("projects") or []) if p.get("solution") == "search"]
    estimators = {"projects": kept}
    if prior.get("search"):
        estimators["search"] = copy.deepcopy(prior["search"])
    sz["estimators"] = estimators
    out.pop("consolidation", None)
    stale = ("written to the estimator", "sheet is not filled")
    notes = [n for n in (sz.get("notes") or []) if not any(phrase in n for phrase in stale)]
    notes.insert(0, COMPRESSION_FORMULA)
    notes.extend(clamped)
    sz["notes"] = notes
    return out


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
    short: list[dict] = []
    matched = 0
    fallback = 0
    fallback_names: list[str] = []
    inactive = 0
    inactive_bundles: list[str] = []
    dropped_gb = 0.0
    snapshot_gb = 0.0
    unmeasured: list[dict] = []
    active_info = (out.get("deployments") or {}).get("active") or {}
    active_names = set(active_info.get("names") or [])
    use_active = bool(active_info.get("month")) and bool(active_names)
    for dep in (out.get("solutions") or {}).get("by_deployment") or []:
        if dep.get("platform") == "serverless":
            continue
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
            accounted, ratio = _accounted_from_bulk(float(dep["daily_raw_gb"]))
            dep["compression_ratio"] = ratio
            dep[COMPRESSIONCALC] = accounted
            snapshot_gb += float(hit.get("snapshot_gb") or 0)
            days = _measured_days(hit.get("retention_days"))
            if days is None:
                dep["retention_unmeasured"] = True
                dep["retention_days"] = None
                if float(hit.get("bulk_gb_day") or 0) > 0:
                    unmeasured.append({
                        "name": dep.get("name"),
                        "bulk_gb_day": round(float(hit.get("bulk_gb_day") or 0), 3),
                    })
            else:
                dep.pop("retention_unmeasured", None)
                dep["retention_days"] = round(days, 3)
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
            if name:
                fallback_names.append(name)
            dep["daily_raw_gb"] = 0.0
            dep["ingest_source"] = "active_no_bundle"

    ech = _ech_rows(out)
    clamped_notes: list[str] = []
    _rollup_solutions(out, clamped_notes)
    con = out["sizing"]["consolidated"]

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
    active_rows = [r for r in rows if r.get("name") in used and r.get("name") not in inactive_bundles]
    bulk_sum = sum(float(r.get("bulk_gb_day") or 0) for r in active_rows)
    accounted_sum, share = _accounted_from_bulk(bulk_sum)
    coverage = {
        "bundles": len(rows),
        "matched": matched,
        "k360_fallback": fallback,
        "fallback_names": fallback_names[:12],
        "unmatched_diagnostics": unmatched_diags[:12],
        "short_uptime": short,
        "bulk_gb_day": round(bulk_sum, 2),
        COMPRESSIONCALC: round(accounted_sum, 3),
        "compression_ratio": None if share is None else round(share, 3),
        "store_gb": round(sum(float(r.get("store_gb") or 0) for r in rows if r.get("name") in used and r.get("name") not in inactive_bundles), 2),
        "snapshot_gb": round(snapshot_gb, 2),
        "retention_unmeasured": unmeasured,
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
        "Retention is the size-weighted ILM delete age on local primary store, or the oldest index age when no delete policy covers that store, rounded up to a whole month for the estimator. "
        "A bundle with no measured age is left out of that average. "
        "Those two are summed by solution and by region. Search disk is the local diagnostic store. "
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
        shown = ", ".join(fallback_names[:8])
        named = f" ({shown})" if shown else ""
        notes.append(
            f"{fallback} active {noun} {verb} no diagnostic bundle{named}. {adds} no ingest. "
            "Current stored GB stays; a decommissioned snapshot is not added."
        )
    if snapshot_gb > 0:
        notes.append(
            f"Searchable snapshots, {snapshot_gb:,.0f} GB, are left out of store and retention. The quote uses local primary store."
        )
    if unmeasured:
        names = ", ".join(row["name"] for row in unmeasured[:8])
        notes.append(
            f"{len(unmeasured)} deployments have bulk and no measured retention ({names}). They stay out of the weighted month."
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
    from privacy import seal_text
    seal_text(Path(args.out), json.dumps(out, indent=2))
    cov = out["sizing"]["diagnostics"]
    print(f"wrote {args.out} matched={cov['matched']} fallback={cov['k360_fallback']} bulk={cov['bulk_gb_day']} GB/day")


if __name__ == "__main__":
    main()
