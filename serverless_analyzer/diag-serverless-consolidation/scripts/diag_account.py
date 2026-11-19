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

Only deployments with a bill in the quote window (the last 90 billed days by
default) are sized. A deployment with no bill in that window is left out, and
its last telemetry snapshot is not added to stored GB.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path

from diag_inputs import ESTIMATE_MONTHS, index_retention_months, whole_months

from constants import DAYS_PER_MONTH, RETENTION_ASSUMED_MAX_SHARE
from serverless_regions import serverless_supports


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


_sheet_months = whole_months  # one shared rounding rule, defined in diag_inputs


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
        if d.get("platform") != "serverless" and d.get("ingest_source") not in ("inactive", "unsupported_region")
    ]


def _dep_bulk_gb(dep: dict) -> float:
    if dep.get("bulk_gb_day_raw") is not None:
        return float(dep.get("bulk_gb_day_raw") or 0)
    return float(dep.get("daily_raw_gb") or 0)


MIX_KEYS = ("logs_gb_day", "k8s_logs_gb_day", "metrics_gb_day", "traces_gb_day")


def observability_mix(rows: list[dict], bulk_key: str = "daily_raw_gb") -> dict:
    """Logs, Kubernetes logs, metrics, and traces GB/day for a group of deployments.

    The product-derived split from the bundle's data stream names/index mode is
    kept in proportion to that row's bulk. Missing product classification is an
    error; no 90/10 mix is invented. `metrics_retention_days` is the
    metrics-weighted delete age for the TSDS retention row.
    """
    logs = k8s = metrics = traces = 0.0
    measured = 0.0
    weight = weighted = 0.0
    for row in rows or []:
        try:
            bulk = float(row.get(bulk_key) or 0)
        except (TypeError, ValueError):
            bulk = 0.0
        if bulk <= 0:
            continue
        parts = None
        if row.get("logs_gb_day") is not None:
            raw = [float(row.get(key) or 0) for key in MIX_KEYS]
            total = raw[0] + raw[2] + raw[3]
            if total > 0:
                parts = [value * bulk / total for value in raw]
        if parts is None:
            raise ValueError(f"diagnostic product mix is missing for {row.get('name') or 'a deployment'}")
        else:
            measured += bulk
        logs += parts[0]
        k8s += parts[1]
        metrics += parts[2]
        traces += parts[3]
        days = _measured_days(row.get("retention_days_metrics"))
        if days is not None and parts[2] > 0:
            weight += parts[2]
            weighted += parts[2] * days
    if measured:
        basis = "measured"
    else:
        basis = None
    return {
        "logs_gb_day": round(logs, 3),
        "k8s_logs_gb_day": round(k8s, 3),
        "metrics_gb_day": round(metrics, 3),
        "traces_gb_day": round(traces, 3),
        "k8s_logs_share": round(k8s / logs, 4) if logs > 0 else 0.0,
        "metrics_retention_days": round(weighted / weight, 3) if weight > 0 else None,
        "basis": basis,
    }


def _rollup_solutions(out: dict, clamped_notes: list[str]) -> None:
    """Rewrite each solution block from the deployment rows currently on the account."""
    sz = out.setdefault("sizing", {})
    con = sz.get("consolidated") or {}
    active_info = (out.get("deployments") or {}).get("active") or {}
    use_active = bool(active_info.get("month")) and bool(active_info.get("names"))
    ech = _ech_rows(out)
    totals = (out.get("solutions") or {}).get("totals") or {}
    explicit_days = (out.get("meta") or {}).get("retention_days")
    index_months = None if explicit_days else index_retention_months(ech)
    if index_months is not None:
        count = sum(int(d.get("retention_index_count") or 0) for d in ech)
        days = sum(float(d.get("retention_index_days_sum") or 0) for d in ech) / count
        meta = out.setdefault("meta", {})
        meta["estimate_months"] = index_months
        meta["retention_index_days"] = round(days, 3)
        meta["retention_index_count"] = count
        unit = "month" if index_months == 1 else "months"
        clamped_notes.append(
            f"Average retention across {count:,} indexes is {days:.1f} days. The estimate prices {index_months} {unit}."
        )
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
        # Index average, rounded up, unless --retention-days named a month. Otherwise 12.
        if explicit_days:
            written_months = int((out.get("meta") or {}).get("estimate_months") or ESTIMATE_MONTHS)
        elif index_months is not None:
            written_months = index_months
        else:
            written_months = ESTIMATE_MONTHS
        written = written_months if daily != 0 else None
        if index_months is None and measured is not None and abs(written_months - measured) > 0.05:
            if written_months == ESTIMATE_MONTHS:
                clamped_notes.append(f"{sol} retention measures {measured:.1f} months. The estimate is a one-year estimate, so {ESTIMATE_MONTHS} is written to the estimator.")
            else:
                clamped_notes.append(f"{sol} retention measures {measured:.1f} months. The estimate prices {written_months} months.")
        blk["deployments"] = len(group)
        # Deployments that were actually sized: a matched bundle, or priced from K360 on request.
        blk["sized_deployments"] = sum(1 for d in group if d.get("ingest_source") == "diagnostics")
        ecu_map = active_info.get("ecu_complete_month") or {}
        if use_active and ecu_map:
            blk["ecu_monthly_avg"] = round(sum(float(ecu_map.get(d["name"]) or 0) for d in group), 2)
        blk["stored_gb"] = round(stored, 2)
        blk["daily_raw_gb"] = round(daily, 2)
        blk.pop("retention_months", None)
        blk.pop("retention_months_measured", None)
        if written is not None:
            blk["retention_months"] = written
            if measured is not None:
                blk["retention_months_measured"] = round(measured, 2)
        # A one-year estimate does not need a measured age, so a solution with bulk is never skipped for lacking one.
        blk["retention_unmeasured"] = False
        blk["gb_estimated_from_ecu_share"] = False
        blk["has_workload"] = bool(stored > 0 or daily != 0 or (blk.get("ecu_lookback") or 0))
        blk["ingest_source"] = "diagnostics"
        blk["bulk_gb_day"] = round(sum(_dep_bulk_gb(dep) for dep in group), 3)
        blk.pop("compressioncalc_accounted_for", None)
        blk.pop("compression_ratio", None)
        if sol == "observability":
            try:
                mix = observability_mix(group)
            except ValueError as exc:
                blk["mix_basis"] = None
                blk["mix_error"] = str(exc)
                clamped_notes.append(f"{exc}. Observability was not given a split.")
                mix = None
            if mix is not None:
                blk["logs_gb_day"] = round(mix["logs_gb_day"], 2)
                blk["metrics_gb_day"] = round(mix["metrics_gb_day"], 2)
                blk["traces_gb_day"] = round(mix["traces_gb_day"], 2)
                blk["k8s_logs_gb_day"] = round(mix["k8s_logs_gb_day"], 2)
                blk["k8s_logs_share"] = mix["k8s_logs_share"]
                blk["mix_basis"] = mix["basis"]
                blk.pop("retention_months_metrics", None)
                if written is not None:
                    blk["retention_months_metrics"] = written
                if mix["basis"] and daily > 0:
                    clamped_notes.append(
                        f"Observability bulk is split by product classification ({mix['basis']}): "
                        f"{mix['logs_gb_day'] / daily * 100:.1f}% logs ({mix['k8s_logs_share'] * 100:.1f}% of them Kubernetes), "
                        f"{mix['metrics_gb_day'] / daily * 100:.1f}% metrics, {mix['traces_gb_day'] / daily * 100:.1f}% traces."
                    )
        elif sol == "security":
            blk["siem_gb_day"] = round(daily, 2)
        if totals.get(sol) is not None:
            totals[sol]["gb"] = round(stored, 2)
            if use_active:
                totals[sol]["deployments"] = len(group)
        con[sol] = blk
    sz["consolidated"] = con


UNMEASURED_MODES = ("exclude", "fail")
# Stored GB ÷ assumed retention is a raw rate. The estimators expand a raw rate to a metered one,
def apply_diagnostics(
    acc: dict, rows: list[dict], admin_names: set[str] | None = None, *,
    unmeasured: str = "exclude",
) -> dict:
    """Return a deep copy whose sizing inputs are the diagnostic measurements.

    `admin_names` is the set of deployment names the admin console lists today. When it is
    given, a billed deployment with no bundle whose name is not in that list is labelled
    decommissioned, not missing a diagnostic: there is nothing left to capture. It is not
    sized: no ingest, and its last stored GB is kept only as `k360_gb`.

    `unmeasured` says what an active deployment with no bundle does:

      exclude  not sized: no ingest and no stored GB. Its K360 size is kept as `k360_gb`.
      fail     sized as `exclude`; the run stops on those names after this returns.
    """
    if unmeasured not in UNMEASURED_MODES:
        raise ValueError(f"unmeasured must be one of {', '.join(UNMEASURED_MODES)}")
    admin_norm = None if admin_names is None else {_norm(n) for n in admin_names}
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
    decommissioned_names: list[str] = []
    inactive = 0
    inactive_bundles: list[str] = []
    dropped_gb = 0.0
    snapshot_gb = 0.0
    no_age: list[dict] = []  # bulk with no measured retention
    unrated_rows: list[dict] = []
    unrated_known = False
    active_info = (out.get("deployments") or {}).get("active") or {}
    active_names = set(active_info.get("names") or [])
    use_active = bool(active_info.get("month")) and bool(active_names)
    region_by_name = {
        d.get("name"): (d.get("provider"), d.get("region"))
        for d in ((out.get("cloud") or {}).get("by_deployment") or [])
        if d.get("name")
    }
    unsupported_region: list[dict] = []
    region_bundles: list[str] = []
    for dep in (out.get("solutions") or {}).get("by_deployment") or []:
        if dep.get("platform") == "serverless":
            continue
        name = dep.get("name") or ""
        if not (use_active and name not in active_names):
            prov, reg = region_by_name.get(name) or (None, None)
            if serverless_supports(prov, reg) is False:
                # Elastic Cloud Serverless is not offered in this region. The workload stays on ECH:
                # no ingest and no stored GB go to the estimators, and the ECH bill is not compared.
                hit = match(name)
                if hit:
                    region_bundles.append(hit["name"])
                dep["k360_gb"] = dep.get("gb")
                dep["gb"] = 0.0
                dep["daily_raw_gb"] = 0.0
                dep["ingest_source"] = "unsupported_region"
                dep["unsupported_region"] = {"provider": str(prov), "region": str(reg)}
                dep["field_sources"] = {
                    "gb": "unsupported_region", "k360_gb": "cloud_tracker_telemetry",
                    "daily_raw_gb": "unsupported_region", "ecu": "cloud_tracker_billing",
                    "region": "cloud_tracker_infrastructure",
                }
                unsupported_region.append({"name": name, "provider": str(prov), "region": str(reg)})
                continue
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
            dep["field_sources"] = {
                "gb": "quote_window_policy",
                "daily_raw_gb": "quote_window_policy",
                "retention_days": "quote_window_policy",
                "k360_gb": "cloud_tracker_telemetry",
                "ecu": "cloud_tracker_billing",
                "region": "cloud_tracker_infrastructure",
            }
            continue
        hit = match(name)
        if hit:
            matched += 1
            dep["gb"] = round(float(hit.get("store_gb") or 0), 2)
            dep["daily_raw_gb"] = round(float(hit.get("bulk_gb_day") or 0), 3)
            dep.pop("compression_ratio", None)
            dep.pop("compressioncalc_accounted_for", None)
            snapshot_gb += float(hit.get("snapshot_gb") or 0)
            split = hit.get("bulk_mix")
            for key in MIX_KEYS:
                dep.pop(key, None)
            if isinstance(split, dict):
                dep["logs_gb_day"] = float(split.get("logs") or 0)
                dep["k8s_logs_gb_day"] = float(split.get("k8s_logs") or 0)
                dep["metrics_gb_day"] = float(split.get("metrics") or 0)
                dep["traces_gb_day"] = float(split.get("traces") or 0)
            dep["retention_days_metrics"] = _measured_days(hit.get("retention_days_metrics"))
            dep["bulk_short_window_gb_day"] = hit.get("bulk_short_window_gb_day")
            dep["bulk_fallback_gb_day"] = hit.get("bulk_fallback_gb_day")
            dep["bulk_restart_gb_day"] = hit.get("bulk_restart_gb_day")
            dep["bulk_restart_indices"] = hit.get("bulk_restart_indices")
            dep["retention_known_share"] = hit.get("retention_known_share")
            # A row parsed before this field existed says nothing either way.
            if hit.get("unrated_indices") is not None:
                unrated_known = True
                dep["unrated_indices"] = int(hit.get("unrated_indices") or 0)
                dep["unrated_store_gb"] = round(float(hit.get("unrated_store_gb") or 0), 2)
                dep["unrated_bulk_gb"] = round(float(hit.get("unrated_bulk_gb") or 0), 2)
                if dep["unrated_indices"]:
                    unrated_rows.append({
                        "name": dep.get("name"),
                        "indices": dep["unrated_indices"],
                        "store_gb": dep["unrated_store_gb"],
                        "bulk_gb": dep["unrated_bulk_gb"],
                        "top": list(hit.get("unrated_top") or [])[:3],
                    })
            days = _measured_days(hit.get("retention_days"))
            if days is None:
                dep["retention_unmeasured"] = True
                dep["retention_days"] = None
                if float(hit.get("bulk_gb_day") or 0) > 0:
                    no_age.append({
                        "name": dep.get("name"),
                        "bulk_gb_day": round(float(hit.get("bulk_gb_day") or 0), 3),
                    })
            else:
                dep.pop("retention_unmeasured", None)
                dep["retention_days"] = round(days, 3)
            if hit.get("retention_index_count"):
                dep["retention_index_count"] = int(hit.get("retention_index_count") or 0)
                dep["retention_index_days_sum"] = float(hit.get("retention_index_days_sum") or 0)
            dep["retention_basis"] = hit.get("retention_basis")
            dep["retention_label"] = hit.get("retention_label")
            dep["ingest_source"] = "diagnostics"
            dep["field_sources"] = {
                "gb": "diagnostics",
                "daily_raw_gb": "diagnostics",
                "retention_days": "diagnostics",
                "retention_days_metrics": "diagnostics",
                "retention_basis": "diagnostics",
                "bulk_mix": "diagnostic_product_classification" if isinstance(split, dict) else "missing_product_classification",
                "ecu": "cloud_tracker_billing",
                "region": "cloud_tracker_infrastructure",
                "solution": "cloud_tracker_classification",
            }
            dep["diag_id"] = hit.get("id")
            up = hit.get("uptime_days_min")
            dep["uptime_days_min"] = up
            if up is not None and float(up) < 2:
                short.append({"name": dep["name"], "uptime_days": up})
        elif admin_norm is not None and _norm(name) not in admin_norm:
            if name:
                decommissioned_names.append(name)
            dep["k360_gb"] = dep.get("gb")
            dep["gb"] = 0.0
            dep["daily_raw_gb"] = 0.0
            dep["ingest_source"] = "decommissioned"
            dep["field_sources"] = {
                "gb": "unmeasured_policy", "k360_gb": "cloud_tracker_telemetry",
                "daily_raw_gb": "unmeasured_policy", "ecu": "cloud_tracker_billing",
                "region": "cloud_tracker_infrastructure",
            }
        else:
            fallback += 1
            if name:
                fallback_names.append(name)
            stored = float(dep.get("gb") or 0)
            sol = dep.get("solution") or "search"
            dep["k360_gb"] = dep.get("gb")
            dep["gb"] = 0.0
            dep["daily_raw_gb"] = 0.0
            dep["ingest_source"] = "active_no_bundle"
            dep["field_sources"] = {
                "gb": "unmeasured_policy", "k360_gb": "cloud_tracker_telemetry",
                "daily_raw_gb": "unmeasured_policy", "ecu": "cloud_tracker_billing",
                "region": "cloud_tracker_infrastructure",
            }

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
    # Diagnostic quotes allocate org Search docs/s using only diagnostic local store.
    # K360 sizes retained on unsized deployments are context only and cannot change an estimate.
    st["ingest_share_store_gb"] = stored_total
    st.setdefault("field_sources", {}).update({
        "data_stored_gb": "diagnostics",
        "ingest_share_store_gb": "diagnostic_local_primary_store_sum",
    })
    by_name = {d["name"]: d for d in ech}
    for row in st.get("by_deployment") or []:
        row["field_sources"] = {
            **(row.get("field_sources") or {}),
            "data_stored_gb": "cloud_tracker_telemetry",
            "docs_count": "cloud_tracker_telemetry",
            "avg_doc_kb": "cloud_tracker_telemetry",
        }
        src = by_name.get(row.get("name"))
        if src and src.get("ingest_source") == "diagnostics":
            row["data_stored_gb"] = src.get("gb")
            row["field_sources"]["data_stored_gb"] = "diagnostics"
    unmatched_diags = [r.get("name") for r in rows if r.get("name") not in used]
    skipped_bundles = set(inactive_bundles) | set(region_bundles)
    _known = [(float(r.get("store_gb") or 0), r.get("retention_known_share")) for r in rows if r.get("name") in used and r.get("name") not in skipped_bundles]
    _store = sum(gb for gb, _share in _known)
    blended_share = None
    if _store > 0 and all(share is not None for _gb, share in _known):
        blended_share = round(sum(gb * (1.0 - float(share)) for gb, share in _known) / _store, 4)
    active_rows = [r for r in rows if r.get("name") in used and r.get("name") not in skipped_bundles]
    # Every row with store at the 30-day default, including a `policy` row that stayed inside
    # the threshold. Counting it as policy-aged must not hide the assumption it still carries.
    assumed_retention_rows = [
        {
            "name": r.get("name"),
            "basis": r.get("retention_basis"),
            "assumed_share": r.get("retention_assumed_share"),
            "within_threshold": r.get("retention_basis") == "policy",
        }
        for r in active_rows
        if r.get("retention_basis") in ("assumed", "blended")
        or float(r.get("retention_assumed_share") or 0) > 0
    ]
    bulk_sum = sum(float(r.get("bulk_gb_day") or 0) for r in active_rows)
    active_store = sum(float(r.get("store_gb") or 0) for r in active_rows)
    unrated_rows.sort(key=lambda row: -(row["store_gb"] or 0))
    unrated_store = round(sum(row["store_gb"] for row in unrated_rows), 2)
    unrated = None
    if unrated_known:
        unrated = {
            "indices": sum(row["indices"] for row in unrated_rows),
            "store_gb": unrated_store,
            "bulk_gb": round(sum(row["bulk_gb"] for row in unrated_rows), 2),
            "share_of_store": round(unrated_store / active_store, 4) if active_store > 0 else None,
            "deployments": unrated_rows[:12],
        }
    coverage = {
        "bundles": len(rows),
        "matched": matched,
        "missing_diagnostics": fallback,
        "fallback_names": fallback_names[:12],
        "unmeasured_mode": unmeasured,
        "unsupported_region": unsupported_region[:24],
        "decommissioned": len(decommissioned_names),
        "decommissioned_names": decommissioned_names[:12],
        "unmatched_diagnostics": unmatched_diags[:12],
        "short_uptime": short,
        "bulk_gb_day": round(bulk_sum, 2),
        "store_gb": round(sum(float(r.get("store_gb") or 0) for r in rows if r.get("name") in used and r.get("name") not in skipped_bundles), 2),
        "snapshot_gb": round(snapshot_gb, 2),
        "retention_unmeasured": no_age,
        "retention_30d_assumed": assumed_retention_rows[:12],
        # Bulk on deployments a delete policy or data-stream retention ages, allowing up to
        # RETENTION_ASSUMED_MAX_SHARE of local store at the 30-day default. A deployment past
        # that share is blended or assumed and contributes nothing.
        "retention_policy_bulk_gb_day": round(sum(
            float(row.get("bulk_gb_day") or 0) for row in active_rows if row.get("retention_basis") == "policy"
        ), 3),
        "retention_assumed_max_share": RETENTION_ASSUMED_MAX_SHARE,
        # Bulk counted as policy-aged that still carries some store at the 30-day default.
        "retention_policy_within_threshold_gb_day": round(sum(
            float(row.get("bulk_gb_day") or 0) for row in active_rows
            if row.get("retention_basis") == "policy" and float(row.get("retention_assumed_share") or 0) > 0
        ), 3),
        "unrated": unrated,
        "bulk_short_window_gb_day": round(sum(float(r.get("bulk_short_window_gb_day") or 0) for r in active_rows), 3),
        "bulk_fallback_gb_day": round(sum(float(r.get("bulk_fallback_gb_day") or 0) for r in active_rows), 3),
        "bulk_restart_gb_day": round(sum(float(r.get("bulk_restart_gb_day") or 0) for r in active_rows), 3),
        "bulk_restart_indices": sum(int(r.get("bulk_restart_indices") or 0) for r in active_rows),
        "bulk_restart_deployments": [
            {"name": r.get("name"), "gb_day": round(float(r.get("bulk_restart_gb_day") or 0), 3)}
            for r in sorted(active_rows, key=lambda r: -float(r.get("bulk_restart_gb_day") or 0))
            if float(r.get("bulk_restart_gb_day") or 0) > 0
        ][:8],
        # Share of local store whose retention is set by a delete policy. The rest uses the 30-day default.
        "retention_blended_share": blended_share,
        "retention_clamped": clamped_notes,
        "active_month": active_info.get("month"),
        "active_month_partial": bool(active_info.get("partial")),
        "window_start": active_info.get("window_start"),
        "window_end": active_info.get("window_end"),
        "window_days": active_info.get("window_days"),
        # ECH deployments on the roster. A serverless project on the roster has no bundle to match.
        "active_deployments": (matched + fallback + len(decommissioned_names)) if use_active else None,
        "ecu_month": active_info.get("ecu_month"),
        "inactive_excluded": inactive,
        "inactive_snapshot_gb": round(dropped_gb, 2),
        "inactive_bundle_count": len(inactive_bundles),
        "inactive_bundles": inactive_bundles[:12],
    }
    source_precedence = {
        "diagnostic_authoritative": [
            "solutions.by_deployment.gb (matched local primary store)",
            "solutions.by_deployment.daily_raw_gb (matched diagnostic bulk)",
            "solutions.by_deployment.retention_days and retention_days_metrics",
            "measured Observability bulk mix",
            "storage.data_stored_gb and matched storage.by_deployment[].data_stored_gb",
            "storage.ingest_share_store_gb (diagnostic local store only; K360 sizes excluded)",
            "sizing.consolidated store, bulk, and retention aggregates",
        ],
        "cloud_tracker_authoritative": [
            "billing ECU and cost",
            "active billing-window roster",
            "Search QPS, ingest docs/s, active hours, and average document size",
            "cloud provider, region, tiers, and deployment metadata",
        ],
        "cloud_tracker_context_only": [
            "solutions.by_deployment.k360_gb (unmeasured deployment size; excluded from diagnostic estimator inputs)",
        ],
    }
    limitations = []
    if fallback:
        limitations.append({"code": "missing_diagnostics", "count": fallback, "names": fallback_names[:12], "severity": "high"})
    if no_age:
        limitations.append({"code": "retention_unmeasured", "count": len(no_age), "names": [r["name"] for r in no_age[:12]], "severity": "medium"})
    if assumed_retention_rows:
        limitations.append({"code": "retention_30d_assumed", "count": len(assumed_retention_rows), "names": [r["name"] for r in assumed_retention_rows[:12]], "severity": "medium"})
    if short:
        limitations.append({"code": "short_diagnostic_window", "count": len(short), "deployments": short[:12], "severity": "medium"})
    if float(coverage.get("bulk_fallback_gb_day") or 0) > 0:
        limitations.append({"code": "bulk_rate_fallback", "bulk_gb_day": coverage["bulk_fallback_gb_day"], "severity": "medium"})
    if unrated and unrated.get("indices"):
        limitations.append({"code": "unrated_ingest", "indices": unrated["indices"], "store_gb": unrated["store_gb"], "severity": "high"})
    # The written month is the index average. With no index counted it falls back to the
    # 12-month default, and retention sets the estimator's retained-data ladder, so say so.
    meta_out = out.get("meta") or {}
    retention_fallback = bool(
        bulk_sum > 0 and not meta_out.get("retention_days") and not meta_out.get("retention_index_count")
    )
    if retention_fallback:
        limitations.append({
            "code": "retention_month_fallback",
            "months": ESTIMATE_MONTHS,
            "detail": (
                f"No index retention age was counted, so every Observability and Security sheet was written at the "
                f"{ESTIMATE_MONTHS}-month default instead of the average retention across indexes. Retention sets the "
                "estimator's retained-data ladder, so that month is an assumption, not a measurement."
            ),
            "severity": "high",
        })
    coverage["source_precedence"] = source_precedence
    coverage["estimation_limits"] = limitations
    notes = [
        "Ingest GB/day is the write-index sum of index.primaries.bulk.est_bytes_per_day from the Elasticsearch diagnostic bundle. "
        "The month written to the estimator is the unweighted average retention across indexes with local primary store, rounded up to a whole month. Each index is aged by its ILM delete policy, else its data-stream retention, else 30 days. The size-weighted age stays a record and is not the written month. "
        "Those two are summed by solution and by region. Search disk and the Search ingest-share denominator use diagnostic local store only; K360 stored sizes are excluded from the diagnostic estimate. "
        "Search query rates use exact-window Cloud Tracker index telemetry. ECH ECU stays the K360 bill.",
    ]
    if short:
        names = ", ".join(f"{s['name']} ({s['uptime_days']}d)" for s in short[:8])
        notes.append(f"Nodes up for under 2 days: {names}. The bulk rate is still the index write phase.")
    if use_active:
        if active_info.get("window_start") and active_info.get("window_end"):
            notes.insert(0, (
                f"Sized {matched + fallback + len(decommissioned_names)} ECH deployments with a bill from {active_info['window_start']} through {active_info['window_end']} "
                f"({active_info.get('window_days')} days). "
                f"{inactive} deployments had no bill in that window. Their last stored snapshot, {dropped_gb:,.0f} GB, is not carried into the sizing."
            ))
        else:
            month = active_info.get("month")
            partial = " That month is partial." if active_info.get("partial") else ""
            notes.insert(0, (
                f"Sized {len(active_names)} deployments with a bill in {month}.{partial} "
                f"{inactive} deployments had no bill that month. Their last stored snapshot, {dropped_gb:,.0f} GB, is not carried into the sizing."
            ))
        if active_info.get("ecu_month") and active_info.get("window_end") and not str(active_info.get("window_end")).startswith(str(active_info.get("ecu_month"))):
            notes.append(f"ECU/mo on the sizing row is {active_info['ecu_month']} billing for those deployments, the last complete month.")
        elif active_info.get("partial") and active_info.get("ecu_month"):
            notes.append(f"ECU/mo on the sizing row is {active_info['ecu_month']} billing for those deployments, the last complete month.")
    if inactive_bundles:
        notes.append(f"{len(inactive_bundles)} diagnostic bundles belong to deployments with no bill in the quote window and are not priced.")
    if decommissioned_names:
        n = len(decommissioned_names)
        notes.append(
            f"{n} billed {'deployment is' if n == 1 else 'deployments are'} decommissioned: not in the admin console, so no diagnostic can be captured "
            f"({', '.join(decommissioned_names[:8])}). They add no ingest and no stored GB."
        )
    if fallback:
        noun = "deployment" if fallback == 1 else "deployments"
        verb = "has" if fallback == 1 else "have"
        adds = "It adds" if fallback == 1 else "They add"
        shown = ", ".join(fallback_names[:8])
        named = f" ({shown})" if shown else ""
        notes.append(
            f"{fallback} active {noun} {verb} no diagnostic bundle{named}. {adds} no ingest and no stored GB to the estimate. "
            "Pass --unmeasured fail to stop the run."
        )
    within = [row for row in assumed_retention_rows if row.get("within_threshold")]
    if within:
        shown = ", ".join(
            f"{row['name']} ({float(row.get('assumed_share') or 0) * 100:.1f}%)"
            for row in sorted(within, key=lambda r: -float(r.get("assumed_share") or 0))[:6]
        )
        noun = "deployment counts" if len(within) == 1 else "deployments count"
        notes.append(
            f"{len(within)} {noun} as policy-aged with some store still at the 30-day default, each inside the "
            f"{RETENTION_ASSUMED_MAX_SHARE * 100:.0f}% threshold ({shown}). One unconfigured index does not drop a "
            "configured cluster out of the measured-retention total, and the assumption is still named here."
        )
    if blended_share is not None and blended_share > RETENTION_ASSUMED_MAX_SHARE:
        notes.append(
            f"{blended_share * 100:.0f}% of local store has no delete policy or data-stream retention. That share uses the 30-day assumed default, not an observed retention duration."
        )
    elif assumed_retention_rows and not within:
        notes.append("No delete policy or data-stream retention was available for some local store; its retention uses the 30-day assumed default.")
    if retention_fallback:
        notes.append(
            f"No index retention age was counted, so the estimate prices the {ESTIMATE_MONTHS}-month default. "
            "The written month is normally the unweighted average retention across indexes, rounded up to a whole month."
        )
    restart_gb = sum(float(r.get("bulk_restart_gb_day") or 0) for r in rows if r.get("name") in used and r.get("name") not in skipped_bundles)
    if restart_gb > 0 and bulk_sum > 0 and restart_gb / bulk_sum > 0.05:
        notes.append(
            f"Primary shards of write indices restarted or moved after those indices were created, so their bulk counter is younger than the "
            f"write phase. Spread over the counter's own age, the bulk rate would be up to {restart_gb:,.1f} GB/day higher "
            f"({restart_gb / bulk_sum * 100:.0f}% of the quoted {bulk_sum:,.1f}). The quote keeps the write-phase rate (esdiag's per-index formula, with first-day pooling)."
        )
    if snapshot_gb > 0:
        notes.append(
            f"Searchable snapshots, {snapshot_gb:,.0f} GB, are left out of store and retention. The quote uses local primary store."
        )
    if no_age:
        names = ", ".join(row["name"] for row in no_age[:8])
        notes.append(
            f"{len(no_age)} deployments have bulk and no measured retention ({names}). They stay out of the weighted month."
        )
    if unrated and unrated["indices"]:
        names = ", ".join(f"{row['name']} ({row['store_gb']:,.0f} GB)" for row in unrated_rows[:6])
        pct = f", {unrated['share_of_store'] * 100:.1f}% of local store" if unrated.get("share_of_store") is not None else ""
        notes.append(
            f"{unrated['indices']} indices took bulk writes and have no ingest rate: {unrated['store_gb']:,.0f} GB of store{pct} ({names}). "
            "They are not a data stream, a write alias, or an ILM rollover, or their write index has no write phase. "
            "Their ingest is not in bulk GB/day, so the estimate can sit below the real rate."
        )
    if unmatched_diags:
        notes.append(f"{len(unmatched_diags)} diagnostic bundles did not match a billed deployment name and are not in the estimate.")
    notes.extend(clamped_notes)
    sz["consolidated"] = con
    sz["ingest_source"] = "diagnostics"
    sz["diagnostics"] = coverage
    sz["source_precedence"] = source_precedence
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
    print(f"wrote {args.out} matched={cov['matched']} missing_diagnostics={cov['missing_diagnostics']} bulk={cov['bulk_gb_day']} GB/day")


if __name__ == "__main__":
    main()
