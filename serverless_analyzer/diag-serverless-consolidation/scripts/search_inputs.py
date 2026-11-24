"""Exact Search workload per region: the proxy logs for search and diagnostics for ingest.

A region's Search project is the Search-classified deployments in that region. Their traffic comes
from `proxy_logs.search_workload`, queried for exactly those deployment ids, so nothing is scaled by
a store share. Ingest comes from the diagnostic bundles: bytes/sec is the measured bulk rate, the
average document is local primary store bytes over primary documents, and the active hours are the
whole 730-hour month, because a bundle holds counters and no hour-by-hour pattern.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime

import proxy_logs

ACTIVE_HOURS = proxy_logs.HOURS_PER_MONTH
BYTES_PER_GB = 1e9
SECONDS_PER_DAY = 86400.0
SOURCE = "service-proxy-requests"


def search_region_rows(acc: dict) -> dict[str, dict]:
    """Per region: the measured Search deployments, with their ids, store and bulk rate."""
    region_by_name = {
        d.get("name"): d.get("region")
        for d in ((acc.get("cloud") or {}).get("by_deployment") or []) if d.get("region")
    }
    rows: dict[str, dict] = {}
    for dep in (acc.get("solutions") or {}).get("by_deployment") or []:
        if dep.get("platform") == "serverless" or dep.get("ingest_source") == "unsupported_region":
            continue
        if (dep.get("solution") or "search") != "search" or not dep.get("deployment_id"):
            continue
        region = region_by_name.get(dep.get("name")) or "unknown region"
        row = rows.setdefault(region, {"names": [], "ids": [], "daily_raw_gb": 0.0, "store_bytes": 0, "docs_count": 0})
        row["names"].append(dep["name"])
        row["ids"].append(str(dep["deployment_id"]).lower())
        row["daily_raw_gb"] += float(dep.get("daily_raw_gb") or 0)
        row["store_bytes"] += int(dep.get("store_bytes") or 0)
        row["docs_count"] += int(dep.get("docs_count") or 0)
    return rows


def ingest_from_diagnostics(row: dict) -> dict:
    """docs/s, average document bytes and active hours from a region's bundles."""
    bytes_per_sec = row["daily_raw_gb"] * BYTES_PER_GB / SECONDS_PER_DAY
    if bytes_per_sec <= 0 or row["docs_count"] <= 0 or row["store_bytes"] <= 0:
        raise ValueError("the diagnostics show no bulk ingest, or no documents to size an average document from")
    avg_doc = row["store_bytes"] / row["docs_count"]
    return {
        "docs_per_sec": bytes_per_sec / avg_doc,
        "avg_doc_bytes": round(avg_doc, 1),
        "active_hours_mo": ACTIVE_HOURS,
        "bytes_per_sec": round(bytes_per_sec, 1),
        "source": "diagnostics",
    }


def _telemetry(status: str, window: dict | None, reason: str | None = None) -> dict:
    window = window or {}
    return {
        "status": status, "source": SOURCE, "reason": reason,
        "telemetry_start": window.get("window_start"), "telemetry_end": window.get("window_end"),
        "telemetry_days": window.get("window_days"),
    }


def regional_workloads(acc: dict, base: str, key: str, *, days: int = proxy_logs.DEFAULT_DAYS,
                       now: datetime | None = None, run=proxy_logs.run_esql, workers: int = 4,
                       deadline: float | None = None) -> dict:
    """The workload the estimators read: one exact block per region plus an org-wide sum.

    The org-wide block exists for the what-if deployment-count bars, which still split it by store
    share. It adds the regions' recommended QPS, takes the slowest region's query time and the mean hour share.

    `deadline` is a `time.monotonic()` instant shared by every region. A region still unread when it
    passes is recorded in `errors` with the time-budget reason, and the regions already read are kept.
    """
    regions: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for region, row in search_region_rows(acc).items():
        try:
            proxy = proxy_logs.search_workload(base, key, row["ids"], days=days, now=now, run=run, workers=workers, deadline=deadline)
            ingest = ingest_from_diagnostics(row)
        except (ValueError, RuntimeError) as exc:
            errors[region] = str(exc)
            regions[region] = {"quote_window_telemetry": _telemetry("incomplete", None, f"{region}: {exc}")}
            continue
        regions[region] = {
            "search": proxy["search"], "ingest": ingest, "source": SOURCE, "deployments": row["names"],
            "window_days": proxy["window_days"], "window_start": proxy["window_start"], "window_end": proxy["window_end"],
            "sampled_hours": proxy["sampled_hours"],
            "quote_window_telemetry": _telemetry("complete", proxy),
        }
    good = [b for b in regions.values() if b.get("search")]
    out: dict = {"source": SOURCE, "regions": regions, "errors": errors}
    if not good:
        reason = "; ".join(f"{r}: {e}" for r, e in errors.items()) or "no measured Search deployment has a deployment id"
        out["quote_window_telemetry"] = _telemetry("incomplete", None, reason)
        out["quote_window_fetch_error"] = reason
        return out

    def merged(band: str) -> dict:
        # A region idle in this band adds no QPS or query time, and counts as a 0 hour share.
        every = [b["search"].get(band) or {} for b in good]
        rows = [r for r in every if r.get("qps") is not None]
        share = round(sum(float(r.get("pct_hours_mo") or 0) for r in every) / len(every), 2)
        if not rows:
            return {"qps": None, "complexity_ms": None, "complexity": None, "pct_hours_mo": share}
        slowest = max(float(r.get("complexity_ms") or 0) for r in rows)
        return {
            "qps": round(sum(r["qps"] for r in rows), 4),
            "complexity_ms": slowest,
            "complexity": proxy_logs.complexity_of(slowest),
            "pct_hours_mo": share,
        }

    dps = sum(b["ingest"]["docs_per_sec"] for b in good)
    bps = sum(b["ingest"]["bytes_per_sec"] for b in good)
    first = good[0]
    out.update({
        "search": {"peak": merged("peak"), "nonpeak": merged("nonpeak"), "idle_pct_hours_mo": 0.0},
        "ingest": {"docs_per_sec": dps, "avg_doc_bytes": round(bps / dps, 1), "active_hours_mo": ACTIVE_HOURS, "source": "diagnostics"},
        "window_days": first["window_days"], "window_start": first["window_start"], "window_end": first["window_end"],
        "quote_window_telemetry": _telemetry("complete", first),
    })
    return out


def settings(env: dict) -> tuple[str, str]:
    """PROXY_LOGS_URL and PROXY_LOGS_API_KEY from .env, or the process environment."""
    merged = {k: env.get(k) or os.environ.get(k) for k in ("PROXY_LOGS_URL", "PROXY_LOGS_API_KEY")}
    return proxy_logs.proxy_settings(merged)


def regional_block(workload: dict | None, region: str) -> dict | None:
    """The exact block for one region, or None when the workload has none (the old org-wide scaling then applies)."""
    return ((workload or {}).get("regions") or {}).get(region)


def exact_inputs(workload: dict | None, region: str, stored_gb: float, org_gb: float) -> tuple[dict, float]:
    """(workload, org store) for one regional Search project.

    With an exact regional block the estimators take it as measured: the org store is set to the project's
    own store, so the share is 1 and nothing is scaled. Without one the org workload and org store go through unchanged.
    """
    block = regional_block(workload, region)
    if block is None:
        if isinstance((workload or {}).get("regions"), dict):
            # A proxy-log workload has a block for every Search region it tried. A region with none is a
            # mismatch (a region string that differs, or a deployment with no id), not a reason to price
            # this project on every region's traffic added together. Fail closed and say why.
            reason = f"Search in {region or 'the project'} has no measured proxy-log workload."
            return {"quote_window_telemetry": {"status": "incomplete", "source": SOURCE, "reason": reason}}, org_gb
        return workload or {}, org_gb
    return block, stored_gb


def project_inputs(workload: dict | None, region: str, stored_gb: float, region_search_gb: float, org_gb: float) -> tuple[dict | None, float, str | None]:
    """(workload, store the share is taken against, error) for one packed Search project in a region.

    A project takes its own region's measured workload, scaled by its share of that region's Search store.
    The project that holds the whole region gets exactly the inputs the headline sheet got, so the plan
    floor and the headline price the same thing. A proxy-log workload with no block for the region is an
    error, not the org-wide sum. A workload with no regional blocks at all (the K360 backup) keeps the
    old org-wide share.
    """
    regions = (workload or {}).get("regions")
    block = regional_block(workload, region)
    if block is not None and block.get("search"):
        return block, float(region_search_gb or stored_gb), None
    if isinstance(regions, dict):
        return None, 0.0, f"Search in {region or 'the project'} has no measured proxy-log workload."
    return workload or {}, org_gb, None
