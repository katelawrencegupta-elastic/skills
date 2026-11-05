#!/usr/bin/env python3
"""Print diagnostic-only Serverless inputs. Does not quote dollars.

Reads sizing_from_diags.json (a list of bundle rows). Each printed project is
Σ bulk_gb_day and the bulk-weighted retention. Stored GB is used only to pack
rows into N groups. --each keeps deployments at or above 50 GB/day alone and
pools the rest into one project. The estimator month is always a whole number,
rounded up. Every project also carries a 30-day read, written as 1 month.
--retention-months replaces the headline month and leaves bulk measured.
No K360 stored-GB backfill is added.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

DAYS_PER_MONTH = 30.416
# 30 / 30.416 is under 1, and the estimator only prices whole months, so a
# 30-day read is written as 1.
THIRTY_DAY_MONTHS = 1
# First Observability ingest tier. A deployment under this, priced alone, never
# leaves the top $/GB band. Pool those rows into one project.
POOL_UNDER_GB_DAY = 50.0


def _num(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def load_rows(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        raw = raw.get("rows") or raw.get("deployments") or []
    rows = []
    for row in raw:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        rows.append(row)
    if not rows:
        raise SystemExit(f"no diagnostic rows in {path}")
    return rows


def whole_months(months: float) -> int:
    """Estimator retention is an integer 1–36. Round up."""
    clamped = min(36.0, max(1.0, float(months)))
    return int(min(36, max(1, math.ceil(clamped - 1e-9))))


def _cells(bulk: float, written: int) -> tuple[dict, dict]:
    return (
        {
            "G21_logs_gb_day": round(bulk * 0.90, 3),
            "G23_metrics_gb_day": round(bulk * 0.10, 3),
            "G11_retention_months": written,
        },
        {
            "G4_siem_gb_day": round(bulk, 3),
            "G6_retention_months": written,
        },
    )


def summarize(rows: list[dict], retention_months: int | None = None) -> dict:
    bulk = sum(_num(r, "bulk_gb_day") for r in rows)
    weight = sum(_num(r, "bulk_gb_day") * _num(r, "retention_days") for r in rows)
    store = sum(_num(r, "store_gb") for r in rows)
    days = (weight / bulk) if bulk else 0.0
    months = days / DAYS_PER_MONTH
    written = whole_months(months) if bulk else 0
    if retention_months is not None and bulk:
        written = int(min(36, max(1, retention_months)))
    o11y, security = _cells(bulk, written)
    thirty = THIRTY_DAY_MONTHS if bulk else 0
    o11y_30, security_30 = _cells(bulk, thirty)
    short = [
        {"name": r["name"], "uptime_days": r.get("uptime_days_min")}
        for r in rows
        if r.get("uptime_days_min") is not None and float(r["uptime_days_min"]) < 2
    ]
    return {
        "clusters": len(rows),
        "names": [r["name"] for r in rows],
        "bulk_gb_day": round(bulk, 3),
        "bulk_times_days": round(weight, 1),
        "weighted_days": round(days, 2),
        "months_measured": round(months, 3),
        "months_written": written,
        "store_gb": round(store, 1),
        "observability_cells": o11y,
        "security_cells": security,
        "thirty_day": {
            "retention_days": 30,
            "months_written": thirty,
            "observability_cells": o11y_30,
            "security_cells": security_30,
        },
        "search_disk_gb": round(store, 1),
        "short_uptime": short,
    }


def pooled_deployments(rows: list[dict], threshold: float = POOL_UNDER_GB_DAY) -> list[list[dict]]:
    """One project per deployment at or above `threshold` GB/day.

    Every deployment under that line is a single pooled project. The pool is
    priced on Σ bulk, so combined ingest can step into cheaper tiers.
    """
    large = [r for r in rows if _num(r, "bulk_gb_day") >= threshold]
    small = [r for r in rows if _num(r, "bulk_gb_day") < threshold]
    groups = [[r] for r in sorted(large, key=lambda r: -_num(r, "bulk_gb_day"))]
    if small:
        groups.append(sorted(small, key=lambda r: -_num(r, "bulk_gb_day")))
    return groups


def pack(rows: list[dict], n: int) -> list[list[dict]]:
    """Largest store_gb into the lightest bin. Price still uses bulk, not store."""
    bins: list[dict] = [{"store": 0.0, "rows": []} for _ in range(n)]
    for row in sorted(rows, key=lambda r: -_num(r, "store_gb")):
        target = min(bins, key=lambda b: (b["store"], len(b["rows"])))
        target["rows"].append(row)
        target["store"] += _num(row, "store_gb")
    groups = [b["rows"] for b in bins if b["rows"]]
    groups.sort(key=lambda g: -sum(_num(r, "bulk_gb_day") for r in g))
    return groups


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--diags-json", required=True, help="sizing_from_diags.json")
    ap.add_argument(
        "--retention-months",
        type=int,
        default=None,
        help="Write this whole month on every project (1–36). Bulk stays the measured rate. Measured months stay in the output.",
    )
    ap.add_argument("--projects", default="1", help="Comma-separated pack counts, e.g. 1,2,3,4,5")
    ap.add_argument(
        "--each",
        action="store_true",
        help=(
            "Also print per-deployment projects. A deployment at or above "
            f"{POOL_UNDER_GB_DAY:g} GB/day stays alone. Deployments under that "
            "are one pooled project."
        ),
    )
    args = ap.parse_args()
    if args.retention_months is not None and not 1 <= args.retention_months <= 36:
        raise SystemExit("--retention-months must be from 1 to 36")
    rows = load_rows(Path(args.diags_json))
    counts = []
    for part in args.projects.split(","):
        part = part.strip()
        if not part:
            continue
        n = int(part)
        if n < 1:
            raise SystemExit("--projects values must be >= 1")
        counts.append(n)
    out: dict = {"source": "diagnostics", "rows": len(rows), "scenarios": []}
    for n in counts:
        groups = [rows] if n == 1 else pack(rows, n)
        projects = [summarize(g, args.retention_months) for g in groups]
        out["scenarios"].append({
            "projects": len(projects),
            "bulk_gb_day": round(sum(p["bulk_gb_day"] for p in projects), 3),
            "groups": projects,
        })
    if args.each:
        each = []
        for group in pooled_deployments(rows):
            item = summarize(group, args.retention_months)
            if len(group) > 1:
                item["pool"] = f"deployments under {POOL_UNDER_GB_DAY:g} GB/day"
            each.append(item)
        out["each"] = each
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
