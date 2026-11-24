#!/usr/bin/env python3
"""Print diagnostic-only Serverless inputs. Does not quote dollars.

Reads sizing_from_diags.json (a list of bundle rows). Each printed project is
Σ bulk_gb_day. The written month is 12 unless --retention-months overrides it.
A row with no measured retention is left out of the measured-age average.
Projects are packed inside one region and one solution. Observability cells
use Observability rows only, Security G4 uses Security bulk ÷ 1.66, and
Search disk uses Search store only. --each keeps deployments at or above
50 GB/day alone and pools the rest into one project.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from constants import DAYS_PER_MONTH, ESTIMATE_MONTHS, HOURS_PER_MONTH, SECURITY_EXPANSION
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




def index_retention_basis(rows: list[dict]) -> tuple[float | None, str | None, int]:
    """(days, weighting, unweighted rows): the mean index retention age and how it was weighted.

    Each index's age is the ILM delete age, else the data-stream retention. An index with neither has
    no age and is not counted. Weighting by local primary store keeps a swarm of small short-lived indexes from
    setting the month a large long-lived one is priced at.

    `weighting` is `store_weighted` or `per_index`. A row that carries the store fields but no local
    primary store (all searchable snapshots, say) has no weight to give and is left out of the
    weighted mean without turning the weighting off. Only a row from a bundle parsed before store
    weights existed, with no `retention_index_age_bytes` at all, makes the whole mean one vote per
    index, and the third value counts those rows so the caller can say so. None when no index was counted.
    """
    total_days = 0.0
    count = 0
    age_bytes = 0.0
    nbytes = 0.0
    legacy = 0
    for row in rows:
        try:
            n = int(row.get("retention_index_count") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            continue
        try:
            total_days += float(row.get("retention_index_days_sum") or 0)
        except (TypeError, ValueError):
            continue
        count += n
        if row.get("retention_index_age_bytes") is None:
            legacy += 1
            continue
        try:
            ab = float(row.get("retention_index_age_bytes") or 0)
            b = float(row.get("retention_index_bytes") or 0)
        except (TypeError, ValueError):
            continue
        if b > 0:
            age_bytes += ab
            nbytes += b
    if count <= 0:
        return None, None, 0
    if legacy == 0 and nbytes > 0:
        return age_bytes / nbytes, "store_weighted", 0
    return total_days / count, "per_index", legacy


def index_retention_days(rows: list[dict]) -> float | None:
    """Mean index retention age in days. `index_retention_basis` also says how it was weighted."""
    return index_retention_basis(rows)[0]


def index_retention_months(rows: list[dict]) -> int | None:
    """Store-weighted mean of index retention ages, rounded up to a whole month. None when no index was counted."""
    days = index_retention_days(rows)
    if days is None:
        return None
    return whole_months(days / DAYS_PER_MONTH)


def written_retention_months(rows: list[dict]) -> tuple[int | None, str]:
    """(whole month or None, basis) for the Observability and Security sheets when no month was named.

    The index average when indexes were counted. Else the ingest-weighted delete age the bundles
    measured (a bundle parsed before index counts existed carries only that). Else, when no
    retention could be ascertained at all, no month: `(None, "unascertained")`. No default is
    written, and a sheet with no month is not filled.
    """
    index = index_retention_months(rows)
    if index is not None:
        return index, "index_average"
    weight = 0.0
    weighted = 0.0
    for row in rows:
        days = _measured_days(row.get("retention_days"))
        if days is None:
            continue
        try:
            rate = abs(float(row.get("bulk_gb_day", row.get("daily_raw_gb")) or 0))
        except (TypeError, ValueError):
            rate = 0.0
        if rate <= 0:
            continue
        weight += rate
        weighted += rate * days
    if weight > 0:
        return whole_months((weighted / weight) / DAYS_PER_MONTH), "measured_age"
    return None, "unascertained"


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


def _cells(bulk: float, written: int | None, mix: dict, basis: str = "raw") -> tuple[dict, dict]:
    """Recorded cells. A diagnostic run uses Option 1. The K360 backup keeps Option 2."""
    if basis == "metered":
        o11y = {
            "G19": round((mix["logs"] + mix["traces"]) * HOURS_PER_MONTH / 24, 3),
            "G20": round(mix["metrics"] * HOURS_PER_MONTH / 24, 3),
            "G23": 0,
            "G24": "0%",
            "G25": 0,
            "G26": 0,
        }
    else:
        o11y = {
            "G23_logs_gb_day": round(mix["logs"] + mix["traces"], 3),
            "G24_k8s_logs_share": round(mix["k8s_logs"] / mix["logs"], 4) if mix["logs"] > 0 else 0,
            "G25_metrics_gb_day": round(mix["metrics"], 3),
        }
    if bulk:
        entered = round(bulk / SECURITY_EXPANSION, 4) if basis == "metered" else round(bulk, 3)
        security = {"G4_siem_gb_day": entered}
    else:
        security = {}
    if written:
        o11y["G11_retention_months"] = written
        o11y["G12_tsds_retention_months"] = written
        security["G6_retention_months"] = written
    return o11y, security


def summarize(rows: list[dict], retention_months: int | None = None, *, basis: str = "raw") -> dict:
    def _is(row: dict, solution: str) -> bool:
        return str(row.get("solution") or "observability") == solution

    o11y_rows = [row for row in rows if _is(row, "observability")]
    security_rows = [row for row in rows if _is(row, "security")]
    search_rows = [row for row in rows if _is(row, "search")]
    bulk = sum(_num(r, "bulk_gb_day") for r in rows)
    o11y_bulk = sum(_num(r, "bulk_gb_day") for r in o11y_rows)
    security_bulk = sum(_num(r, "bulk_gb_day") for r in security_rows)
    mix = {"logs": 0.0, "k8s_logs": 0.0, "metrics": 0.0, "traces": 0.0}
    search_store = sum(_num(r, "store_gb") for r in search_rows)
    store = sum(_num(r, "store_gb") for r in rows)
    weight = 0.0
    weight_bulk = 0.0
    unmeasured = []
    for row in rows:
        rate = _num(row, "bulk_gb_day")
        raw_mix = row.get("bulk_mix")
        if rate > 0 and _is(row, "observability"):
            if row.get("solution") == "observability" and not isinstance(raw_mix, dict):
                raise ValueError(f"product-derived Observability mix is missing for {row.get('name') or 'a deployment'}")
            if isinstance(raw_mix, dict):
                for kind in mix:
                    mix[kind] += float(raw_mix.get(kind) or 0)
        days = _measured_days(row.get("retention_days"))
        if days is None:
            if rate > 0:
                unmeasured.append({"name": row.get("name"), "bulk_gb_day": round(rate, 3)})
            continue
        weight_bulk += rate
        weight += rate * days
    days = (weight / weight_bulk) if weight_bulk else None
    months = (days / DAYS_PER_MONTH) if days is not None else None
    # The month set by the caller, else the index average, else the measured delete age, else none.
    written = None
    if bulk:
        if retention_months is not None:
            written = int(min(36, max(1, retention_months)))
        else:
            written = written_retention_months(rows)[0]
    o11y, _ignored = _cells(o11y_bulk, written if o11y_rows else None, mix, basis)
    _ignored, security = _cells(security_bulk, written if security_rows else None, mix, basis)
    short = [
        {"name": r["name"], "uptime_days": r.get("uptime_days_min")}
        for r in rows
        if r.get("uptime_days_min") is not None and float(r["uptime_days_min"]) < 2
    ]
    return {
        "clusters": len(rows),
        "names": [r["name"] for r in rows],
        "bulk_gb_day": round(bulk, 3),
        "bulk_mix_basis": "product-derived",
        "bulk_mix": {key: round(value, 3) for key, value in mix.items()},
        "bulk_times_days": round(weight, 1),
        "weighted_days": round(days, 2) if days is not None else None,
        "months_measured": round(months, 3) if months is not None else None,
        "months_written": written,
        "retention_unmeasured": unmeasured,
        "store_gb": round(store, 1),
        "observability_cells": o11y,
        "security_cells": security,
        "search_disk_gb": round(search_store, 1),
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


def _join_solution_region(rows: list[dict], account_path: Path | None) -> list[dict]:
    """Rows from a bundle file often omit solution and region. Join them, or stop."""
    missing = [row for row in rows if not row.get("solution") or not row.get("region")]
    if not missing:
        return rows
    if account_path is None:
        names = ", ".join(str(row.get("name") or "unnamed") for row in missing[:8])
        raise SystemExit(f"rows have no solution or region ({names}). Pass --account-json.")
    account = json.loads(account_path.read_text())
    by_name = {
        dep.get("name"): dep
        for dep in ((account.get("solutions") or {}).get("by_deployment") or [])
        if isinstance(dep, dict) and dep.get("name")
    }
    joined = []
    still = []
    for row in rows:
        item = dict(row)
        dep = by_name.get(item.get("name")) or {}
        item["solution"] = item.get("solution") or dep.get("solution")
        item["region"] = item.get("region") or dep.get("region")
        if not item.get("solution") or not item.get("region"):
            still.append(str(item.get("name") or "unnamed"))
        joined.append(item)
    if still:
        raise SystemExit("no solution or region for " + ", ".join(still[:8]))
    return joined


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--diags-json", required=True, help="sizing_from_diags.json")
    ap.add_argument("--account-json", default=None, help="Join solution and region onto rows that do not carry them")
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
    rows = _join_solution_region(load_rows(Path(args.diags_json)), Path(args.account_json) if args.account_json else None)
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
        buckets: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            buckets.setdefault(_pair_key(row), []).append(row)
        pairs = list(buckets.values())
        groups = _split_within_pairs(pairs, max(n, len(pairs)))
        projects = [summarize(g, args.retention_months, basis="metered") for g in groups]
        out["scenarios"].append({
            "projects": len(projects),
            "bulk_gb_day": round(sum(p["bulk_gb_day"] for p in projects), 3),
            "groups": projects,
        })
    if args.each:
        each = []
        buckets: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            buckets.setdefault(_pair_key(row), []).append(row)
        for group_rows in buckets.values():
            for group in pooled_deployments(group_rows):
                item = summarize(group, args.retention_months, basis="metered")
                if len(group) > 1:
                    item["pool"] = f"deployments under {POOL_UNDER_GB_DAY:g} GB/day"
                each.append(item)
        out["each"] = each
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
