#!/usr/bin/env python3
"""Print diagnostic-only Serverless inputs. Does not quote dollars.

Reads sizing_from_diags.json (a list of bundle rows). Each printed project is
Σ bulk_gb_day and the bulk-weighted measured retention. A row with no measured
retention is left out of that average. Stored GB is used only to pack rows
into N groups. --each keeps deployments at or above 50 GB/day alone and
pools the rest into one project. The estimator month is a whole number,
rounded up, and is omitted when nothing was measured.
--retention-months replaces the headline month and leaves bulk measured.
No K360 stored-GB backfill is added.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

DAYS_PER_MONTH = 30.416
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
    """Estimator retention is an integer 1–36. Round up.

    A remainder under 0.001 month is the 30.416 truncation, not another month.
    365 / 30.416 is 12.000263 and stays 12. A real extra day still rounds up.
    """
    months = float(months)
    whole = math.floor(months)
    if whole >= 1 and 0 <= (months - whole) < 0.001:
        months = float(whole)
    clamped = min(36.0, max(1.0, months))
    return int(min(36, max(1, math.ceil(clamped - 1e-9))))


def _measured_days(value) -> float | None:
    """A retention age counts only when the bundle measured it. Zero and missing do not."""
    try:
        days = float(value)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return days


def _cells(bulk: float, written: int | None) -> tuple[dict, dict]:
    o11y = {
        "G23_logs_gb_day": round(bulk * 0.90, 3),
        "G25_metrics_gb_day": round(bulk * 0.10, 3),
    }
    security = {"G4_siem_gb_day": round(bulk, 3)}
    if written:
        o11y["G11_retention_months"] = written
        o11y["G12_tsds_retention_months"] = written
        security["G6_retention_months"] = written
    return o11y, security


def summarize(rows: list[dict], retention_months: int | None = None) -> dict:
    bulk = sum(_num(r, "bulk_gb_day") for r in rows)
    store = sum(_num(r, "store_gb") for r in rows)
    weight = 0.0
    weight_bulk = 0.0
    unmeasured = []
    for row in rows:
        rate = _num(row, "bulk_gb_day")
        days = _measured_days(row.get("retention_days"))
        if days is None:
            if rate > 0:
                unmeasured.append({"name": row.get("name"), "bulk_gb_day": round(rate, 3)})
            continue
        weight_bulk += rate
        weight += rate * days
    days = (weight / weight_bulk) if weight_bulk else None
    months = (days / DAYS_PER_MONTH) if days is not None else None
    written = whole_months(months) if months is not None else None
    if retention_months is not None and bulk:
        written = int(min(36, max(1, retention_months)))
    o11y, security = _cells(bulk, written)
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
        "weighted_days": round(days, 2) if days is not None else None,
        "months_measured": round(months, 3) if months is not None else None,
        "months_written": written,
        "retention_unmeasured": unmeasured,
        "store_gb": round(store, 1),
        "observability_cells": o11y,
        "security_cells": security,
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


def _pair_key(row: dict) -> tuple[str, str]:
    return (str(row.get("region") or "unknown"), str(row.get("solution") or "observability"))


def plan_floor_for_rows(rows: list[dict]) -> int:
    """One serverless project per feature per region. Never more than the deployment count."""
    n = len(rows)
    logical = len({_pair_key(row) for row in rows}) or 1
    if n <= 0:
        return logical
    return min(logical, n)


def reduction_counts(ceiling: int, floor: int) -> list[int]:
    """Ceiling, then round(previous × 0.75) until the floor. Half-to-even. The series stops at the floor."""
    if ceiling <= 0:
        return []
    floor_n = min(max(int(floor), 1), ceiling)
    counts = [ceiling]
    current = ceiling
    while current > floor_n:
        nxt = int(round(current * 0.75))
        if nxt >= current:
            nxt = current - 1
        if nxt <= floor_n:
            if counts[-1] != floor_n:
                counts.append(floor_n)
            break
        counts.append(nxt)
        current = nxt
    return counts


def _split_within_pairs(pairs: list[list[dict]], count: int) -> list[list[dict]]:
    """`count` projects, at least one per region and feature. Extras stay inside that pair."""
    slots = [1] * len(pairs)
    extras = max(0, count - len(pairs))
    order = sorted(range(len(pairs)), key=lambda i: (-len(pairs[i]), -sum(_num(r, "store_gb") for r in pairs[i])))
    for index in order:
        if extras <= 0:
            break
        room = len(pairs[index]) - slots[index]
        give = min(room, extras)
        slots[index] += give
        extras -= give
    groups: list[list[dict]] = []
    for pair, nslot in zip(pairs, slots):
        if nslot >= len(pair):
            groups.extend([[row] for row in pair])
        elif nslot <= 1:
            groups.append(list(pair))
        else:
            groups.extend(pack(pair, nslot))
    return groups


def region_feature_plans(rows: list[dict]) -> list[dict]:
    """Ceiling is one project per ECH deployment. Each later bar is 25% fewer, until one per region and feature."""
    n = len(rows)
    buckets: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        buckets.setdefault(_pair_key(row), []).append(row)
    pairs = sorted(buckets.values(), key=lambda g: -sum(_num(r, "store_gb") for r in g))
    floor = plan_floor_for_rows(rows)
    counts = reduction_counts(n, floor)
    plans = []
    previous = None
    for step, count in enumerate(counts):
        if n == 0 or count <= 0:
            groups = []
            label = "Ceiling"
        elif count >= n:
            groups = [[row] for row in sorted(rows, key=lambda r: -_num(r, "bulk_gb_day"))]
            label = "Ceiling"
        elif count <= len(pairs):
            groups = [list(pair) for pair in pairs]
            label = "Floor"
        else:
            groups = _split_within_pairs(pairs, count)
            label = "−25%"
        plans.append({
            "cut_pct": step,
            "label": label,
            "projects": len(groups) if groups else count,
            "from_projects": previous,
            "groups": groups,
            "floor": floor,
        })
        previous = len(groups) if groups else count
    return plans


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
