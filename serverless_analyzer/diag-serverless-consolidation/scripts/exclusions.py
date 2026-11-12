"""Leave chosen deployments out of a consolidation run, by deployment id.

An excluded deployment is out of everything: billing ECU (and so the ECH bill on the cover),
diagnostic bulk, stored GB, the deployment count, the plan ceiling, and the estimate sheets.

Ids map to names through the diagnostic manifest (`manifest.json`, written by the admin pull)
or the admin deployment listing. Billing is monthly per deployment, so the ECU removed from a
day is that deployment's share of its month's org ECU. The brief and page 1 say so.
"""
from __future__ import annotations

import re
from datetime import date

ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def parse_ids(values) -> list[str]:
    """Deployment ids from repeated flags or comma lists. Anything not 32 hex characters stops the run."""
    out: list[str] = []
    for raw in values or []:
        for part in str(raw).replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if not ID_RE.match(part):
                raise SystemExit(f"--exclude-deployment {part!r} is not a deployment id (32 hex characters)")
            low = part.lower()
            if low not in out:
                out.append(low)
    return out


def resolve_names(ids: list[str], id_to_name: dict[str, str]) -> list[dict]:
    """[{id, name}] for every id. An id with no name is an error, never skipped."""
    known = {str(k).lower(): v for k, v in id_to_name.items() if k and v}
    missing = [i for i in ids if i not in known]
    if missing:
        raise SystemExit(
            "--exclude-deployment: no deployment has id " + ", ".join(missing)
            + ". Ids come from the diagnostic manifest or the admin deployment list."
        )
    return [{"id": i, "name": known[i]} for i in ids]


def manifest_names(manifest_rows: list[dict]) -> dict[str, str]:
    return {str(r.get("id")).lower(): r.get("name") for r in manifest_rows or [] if r.get("id") and r.get("name")}


def _month_keep(acc: dict, org: dict, names: set[str]) -> dict[str, float]:
    """Share of each month's org ECU that stays after the excluded deployments are removed."""
    totals = ((acc.get("ecu") or {}).get("monthly") or {})
    months = list(totals.get("months") or [])
    org_dep = ((org or {}).get("monthly") or {}).get("by_deployment") or {}
    removed = {m: 0.0 for m in months}
    for name in names:
        series = org_dep.get(name) or {}
        for m, v in zip(series.get("months") or [], series.get("ecu") or []):
            if m in removed:
                removed[m] += float(v or 0)
    keep: dict[str, float] = {}
    for m, tot in zip(months, totals.get("totals") or []):
        tot = float(tot or 0)
        keep[m] = max(0.0, 1.0 - removed.get(m, 0.0) / tot) if tot > 0 else 1.0
    return keep


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def apply_exclusions(acc: dict, org: dict, excluded: list[dict]) -> dict:
    """Remove the excluded deployments from the account in place. Returns the record kept in the brief."""
    names = {e["name"] for e in excluded}
    ecu = acc.setdefault("ecu", {})
    keep = _month_keep(acc, org, names)
    months = list((ecu.get("monthly") or {}).get("months") or [])
    partial = set((ecu.get("monthly") or {}).get("partial_months") or [])
    old_totals = [float(v or 0) for v in (ecu.get("monthly") or {}).get("totals") or []]
    new_totals = [round(t * keep.get(m, 1.0), 2) for m, t in zip(months, old_totals)]
    removed_ecu = round(sum(old_totals) - sum(new_totals), 2)
    if new_totals:
        ecu["monthly"]["totals"] = new_totals
    complete_old = [t for m, t in zip(months, old_totals) if m not in partial]
    complete_new = [t for m, t in zip(months, new_totals) if m not in partial]
    if complete_old and ecu.get("monthly_avg_complete_months"):
        ecu["monthly_avg_complete_months"] = round(_mean(complete_new), 2)
    if new_totals and ecu.get("monthly_avg_all") is not None:
        ecu["monthly_avg_all"] = round(_mean(new_totals), 2)
    for key in ("total_lookback", "total_lookback_ech_named"):
        if ecu.get(key) is not None:
            ecu[key] = round(sum(new_totals), 2)
    if new_totals:
        top = max(range(len(new_totals)), key=lambda i: new_totals[i])
        low = min(range(len(new_totals)), key=lambda i: new_totals[i])
        if ecu.get("monthly_peak"):
            ecu["monthly_peak"] = {"month": months[top], "ecu": new_totals[top]}
        if ecu.get("monthly_low"):
            ecu["monthly_low"] = {"month": months[low], "ecu": new_totals[low]}

    days = list(ecu.get("daily_days") or [])
    daily = [float(v or 0) for v in ecu.get("daily_totals") or []]
    if days and len(days) == len(daily):
        daily = [round(v * keep.get(d[:7], 1.0), 2) for d, v in zip(days, daily)]
        ecu["daily_totals"] = daily
        active = [v for v in daily if v > 0]
        ecu["daily_avg"] = round(_mean(daily), 2)
        ecu["daily_avg_active_days"] = round(_mean(active), 2) if active else 0
        last30 = daily[-30:]
        if last30:
            ecu["last_30d_total"] = round(sum(last30), 2)
            ecu["last_30d_daily_avg"] = round(_mean(last30), 2)
        peak = max(range(len(daily)), key=lambda i: daily[i])
        if ecu.get("daily_peak"):
            ecu["daily_peak"] = {"day": days[peak], "ecu": daily[peak]}
    for key in ("quote_ingest_scaled", "quote_lookback_days", "quote_daily_avg", "quote_monthly_ecu", "quote_usage_factor", "quote_ingest_factor"):
        ecu.pop(key, None)

    dep = acc.setdefault("deployments", {})
    present = [d for d in (acc.get("solutions") or {}).get("by_deployment") or [] if d.get("name") in names]
    ech_removed = sum(1 for d in present if d.get("platform") != "serverless")
    dep["count"] = max(0, int(dep.get("count") or 0) - len(present))
    dep["ech_count"] = max(0, int(dep.get("ech_count") or 0) - ech_removed)
    dep["top"] = [d for d in dep.get("top") or [] if d.get("name") not in names]
    active = dep.get("active") or {}
    if active.get("names"):
        in_active = [n for n in active["names"] if n in names]
        active["names"] = [n for n in active["names"] if n not in names]
        active["count"] = max(0, int(active.get("count") or 0) - len(in_active))
    sol = acc.setdefault("solutions", {})
    sol["by_deployment"] = [d for d in sol.get("by_deployment") or [] if d.get("name") not in names]
    st = acc.setdefault("storage", {})
    st["by_deployment"] = [d for d in st.get("by_deployment") or [] if d.get("name") not in names]
    st["size_gaps"] = [g for g in st.get("size_gaps") or [] if g.get("name") not in names]
    ai = acc.get("ai_ml")
    if isinstance(ai, dict) and ai.get("by_deployment"):
        ai["by_deployment"] = [d for d in ai["by_deployment"] if d.get("name") not in names]

    record = {
        "deployments": [dict(e) for e in excluded],
        "ecu_removed_over_lookback": removed_ecu,
        "method": "Each excluded deployment's billed ECU for a month ÷ the org's ECU for that month, removed from every day in that month.",
    }
    acc.setdefault("meta", {})["excluded_deployments"] = record
    return record


def run_key_suffix(excluded: list[dict]) -> str:
    return f"_excluding_{len(excluded)}"
