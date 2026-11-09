#!/usr/bin/env python3
"""Build the slim account-analysis JSON from fetch_org.py output (+ k360_infra.py).

Pure transformation — no network. Importable (`build_account`) or CLI:

  python analyze.py --org-json /tmp/org.json --infra-json /tmp/infra.json --out account.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
import calendar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DAYS_PER_MONTH = 30.416
SIZING_THRESHOLD_PCT = 50.0
SIGNIFICANT_PCT = 20.0  # deployments-over-time change considered significant
SIGNIFICANT_ABS = 3

_O11Y_HINTS = ("observability", "o11y", "monitor", "apm", "synthetics", "uptime", "metrics", "logging", "logs", "log")
_SEC_HINTS = ("security", "siem", "soc", "cyberark", "infoblox", "endpoint", "threat", "defend")


# --------------------------------------------------------------------------- helpers
def slug(name: str) -> str:
    s = re.sub(r"[^\w\s\-]+", "", (name or "Customer").strip(), flags=re.UNICODE)
    return re.sub(r"[\s\-]+", "_", s).strip("_") or "Customer"


def run_key(customer: str, when: date) -> str:
    return f"{slug(customer)}_{when.isoformat()}"


def pct(part: float, whole: float, nd: int = 1) -> float:
    return round(100.0 * part / whole, nd) if whole else 0.0


def r2(x: float | None) -> float:
    return round(float(x or 0), 2)


_DECODE_INFERENCE = None
_KIND_LABEL = {
    "chat_input": "Input tokens",
    "chat_output": "Output tokens",
    "embed_dense": "Embedding tokens",
    "embed_sparse": "Sparse embedding tokens",
    "other": "Other inference SKUs",
}
_SOLUTIONS = ("search", "observability", "security")


def decode_inference_sku(sku: str) -> dict:
    """Same decoder as the org-report fetch. Model id is the SKU suffix, never invented."""
    global _DECODE_INFERENCE
    if _DECODE_INFERENCE is None:
        root = Path(os.environ.get("K360_ORG_SKILL_DIR") or Path.home() / ".cursor/skills/k360-org-report")
        scripts = root / "scripts" if (root / "scripts" / "fetch_org.py").is_file() else root
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from fetch_org import decode_inference_sku as _dec

        _DECODE_INFERENCE = _dec
    return _DECODE_INFERENCE(sku)


def _sku_scope(sku: str) -> str:
    low = (sku or "").lower()
    if low.startswith("serverless."):
        return "serverless"
    if low.startswith("global."):
        return "global"
    return "deployment"


def _annotate_sku(raw: dict) -> dict | None:
    ecu = float(raw.get("ecu") or 0)
    qty = float(raw.get("qty") if raw.get("qty") is not None else raw.get("million_tokens") or 0)
    if round(ecu, 4) <= 0 and round(qty, 4) <= 0:
        return None
    dec = decode_inference_sku(str(raw.get("sku") or ""))
    return {
        **dec,
        "ecu": round(ecu, 4),
        "qty": round(qty, 6),
        "million_tokens": round(qty, 6),
        "ecu_per_million": round(ecu / qty, 4) if qty else None,
        "scope": _sku_scope(str(raw.get("sku") or "")),
    }


def _aggregate_token_rows(skus: list[dict]) -> list[dict]:
    buckets: dict[tuple, dict] = {}
    for s in skus:
        key = (s.get("kind"), s.get("model") or "", s.get("scope") or _sku_scope(s.get("sku") or ""))
        b = buckets.get(key)
        if b is None:
            b = {
                "kind": s.get("kind"),
                "kind_label": _KIND_LABEL.get(s.get("kind") or "", "Tokens"),
                "model": s.get("model"),
                "model_label": s.get("model_label"),
                "scope": key[2],
                "million_tokens": 0.0,
                "ecu": 0.0,
            }
            buckets[key] = b
        b["million_tokens"] += float(s.get("million_tokens") or 0)
        b["ecu"] += float(s.get("ecu") or 0)
    rows = []
    for b in buckets.values():
        qty, ecu = b["million_tokens"], b["ecu"]
        b["million_tokens"] = round(qty, 4)
        b["ecu"] = round(ecu, 4)
        if b["million_tokens"] <= 0 and b["ecu"] <= 0:
            continue
        b["ecu_per_million"] = round(ecu / qty, 2) if qty else None
        rows.append(b)
    rows.sort(key=lambda r: (-r["ecu"], -r["million_tokens"], r.get("model_label") or ""))
    return rows


def _token_meter(org: dict, dep_skus: list[tuple[str, str, list[dict]]]) -> dict:
    """Org bill by model, plus a solution split taken from each deployment's own SKUs.

    Search consolidation must use the search split. Observability and Security
    tokens stay on this meter and are not added to the Search estimate.
    """
    org_skus = [s for raw in ((org.get("ai") or {}).get("inference_skus") or []) if (s := _annotate_sku(raw))]
    rows = _aggregate_token_rows(org_skus)
    million = round(sum(s["million_tokens"] for s in org_skus), 4)
    ecu = round(sum(s["ecu"] for s in org_skus), 4)
    by_kind: dict[str, dict] = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], {"kind": r["kind"], "label": r["kind_label"], "million_tokens": 0.0, "ecu": 0.0})
        k["million_tokens"] += r["million_tokens"]
        k["ecu"] += r["ecu"]
    kind_rows = []
    billed = (org.get("ai") or {}).get("inference_billed") or {}
    billed_ecu = float(billed.get("ecu") or 0)
    billed_tokens = float(billed.get("million_tokens") or 0)
    resid_ecu = round(billed_ecu - ecu, 4)
    resid_tokens = round(billed_tokens - million, 4)
    if resid_ecu >= 0.05 or resid_tokens >= 0.05:
        rows.append({
            "kind": "other",
            "kind_label": _KIND_LABEL["other"],
            "model": None,
            "model_label": None,
            "scope": "other",
            "million_tokens": round(max(resid_tokens, 0.0), 4),
            "ecu": round(max(resid_ecu, 0.0), 4),
            "ecu_per_million": round(resid_ecu / resid_tokens, 2) if resid_tokens > 0 else None,
        })
        million = round(billed_tokens, 4)
        ecu = round(billed_ecu, 2)
        other = by_kind.setdefault("other", {"kind": "other", "label": _KIND_LABEL["other"], "million_tokens": 0.0, "ecu": 0.0})
        other["million_tokens"] += max(resid_tokens, 0.0)
        other["ecu"] += max(resid_ecu, 0.0)
        notes_extra = (
            f"{max(resid_tokens, 0):.2f} million tokens ({max(resid_ecu, 0):.2f} ECU) sit on inference SKUs outside the named list. "
            "They are metered with no model name and are not added to a solution estimate."
        )
    else:
        notes_extra = None
    for k in ("chat_input", "chat_output", "embed_dense", "embed_sparse", "other"):
        b = by_kind.get(k)
        if not b:
            continue
        qty, kecu = b["million_tokens"], b["ecu"]
        kind_rows.append({
            "kind": k,
            "label": b["label"],
            "million_tokens": round(qty, 4),
            "ecu": round(kecu, 4),
            "ecu_per_million": round(kecu / qty, 2) if qty else None,
        })
    by_solution: dict[str, dict] = {}
    search_flat: list[dict] = []
    dep_ecu = 0.0
    dep_tokens = 0.0
    for _name, sol, skus in dep_skus:
        if not skus:
            continue
        slot = by_solution.setdefault(sol, {"million_tokens": 0.0, "ecu": 0.0})
        for s in skus:
            slot["million_tokens"] += s["million_tokens"]
            slot["ecu"] += s["ecu"]
            dep_ecu += s["ecu"]
            dep_tokens += s["million_tokens"]
            if sol == "search":
                search_flat.append(s)
    for sol, slot in by_solution.items():
        slot["million_tokens"] = round(slot["million_tokens"], 4)
        slot["ecu"] = round(slot["ecu"], 2)
    gap_ecu = round(ecu - dep_ecu, 2)
    gap_tokens = round(million - dep_tokens, 4)
    if abs(gap_ecu) >= 0.05 or abs(gap_tokens) >= 0.05:
        by_solution["unattributed"] = {"million_tokens": gap_tokens, "ecu": gap_ecu}
    notes = [
        "quantity on inference SKUs is millions of tokens. ECU per million is cost ÷ quantity, the rate already on the bill.",
        "A chat SKU with no model suffix is metered, and the model name is left blank.",
    ]
    if notes_extra:
        notes.append(notes_extra)
    if abs(gap_ecu) >= 0.05 or abs(gap_tokens) >= 0.05:
        notes.append(
            f"{gap_tokens:.2f} million tokens ({gap_ecu:.2f} ECU) are on the org meter but not on a named deployment. They stay in the meter total, under unattributed, and are not added to a solution estimate."
        )
    type_ecu = float((org.get("ai") or {}).get("billing_ecu_total") or 0)
    if type_ecu - ecu >= 0.05:
        notes.append(f"{type_ecu - ecu:.2f} ECU of genai billing has no inference SKU, so it is not in the token meter.")
    return {
        "unit": "million tokens",
        "million_tokens": million,
        "ecu": round(ecu, 2),
        "by_kind": kind_rows,
        "by_solution": by_solution,
        "rows": rows,
        "search_rows": _aggregate_token_rows(search_flat),
        "notes": notes,
    }


def ver_tuple(v: str | None) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(v or ""))
    return tuple(int(p) for p in parts[:3]) or (0,)


def tokens(name: str) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", (name or "").lower())) - {""}


_COMP_ORDER = (
    "datahot", "datawarm", "datacold", "datafrozen", "master", "ml", "coordinating", "ingest",
    "kibana", "enterprisesearch", "integrationsserver",
)
_COMP_LABEL = {
    "datahot": "hot", "datawarm": "warm", "datacold": "cold", "datafrozen": "frozen",
    "master": "master", "ml": "ml", "coordinating": "coord", "ingest": "ingest",
    "kibana": "kibana", "enterprisesearch": "ent-search", "integrationsserver": "integrations",
}


def component_summary(dep: dict | None) -> str:
    """hot 8 GB ×2 · ml 8 GB ×3. Counts are one node per billed zone."""
    comps = (dep or {}).get("components") or []

    def order(c: dict) -> int:
        tier = c.get("tier") or ""
        return _COMP_ORDER.index(tier) if tier in _COMP_ORDER else 50

    parts = []
    for c in sorted(comps, key=order):
        nodes = c.get("nodes")
        if not isinstance(nodes, (int, float)) or nodes <= 0:
            continue
        label = _COMP_LABEL.get(c.get("tier"), c.get("tier") or "node")
        ram = c.get("ram_gb")
        ram_s = f"{ram:g} GB " if isinstance(ram, (int, float)) and ram > 0 else ""
        parts.append(f"{label} {ram_s}×{int(nodes)}")
    return " · ".join(parts)


def display_solution(info: dict | None, dep: dict | None = None) -> str:
    """Classified solution. A use_case that is already one of the three names is the fallback."""
    sol = str((info or {}).get("solution") or "").lower()
    if sol in _SOLUTIONS:
        return sol
    use = str((info or {}).get("use_case") or (dep or {}).get("use_case") or "").lower()
    return use if use in _SOLUTIONS else "unknown"


def _hint(name: str, hints: tuple[str, ...]) -> bool:
    toks = tokens(name)
    low = (name or "").lower()
    return any(h in toks for h in hints) or any(len(h) >= 5 and h in low for h in hints)


def solution_for(dep: dict) -> tuple[str, str]:
    """→ (observability|security|search, method)."""
    uc = str(dep.get("use_case") or "").lower()
    fam = dep.get("families") or {}
    ecu = float(dep.get("ecu") or 0)
    if uc.startswith("observ") or uc in ("logging", "logs", "apm", "metrics"):
        return "observability", "use_case"
    if uc.startswith("sec"):
        return "security", "use_case"
    if uc in ("search", "enterprise_search", "enterprise search", "app_search", "workplace_search"):
        return "search", "use_case"
    if ecu and float(fam.get("observability") or 0) > 0.5 * ecu:
        return "observability", "billing_family"
    if float(fam.get("security") or 0) > 0:
        return "security", "billing_family"
    if _hint(dep.get("name") or "", _SEC_HINTS):
        return "security", "name_hint"
    if _hint(dep.get("name") or "", _O11Y_HINTS):
        return "observability", "name_hint"
    return "search", "default"


def sanitize(obj):
    """Replace non-finite floats (K360 emits inf consumption_rate) so JSON stays strict."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


def _trim_trailing_zeros(days: list[str], vals: list[float]) -> tuple[list[str], list[float]]:
    d, v = list(days), list(vals)
    while v and (v[-1] or 0) == 0:
        d.pop()
        v.pop()
    return d, v


# --------------------------------------------------------------------------- sections
def serverless_projects(org: dict, infra: dict | None) -> list[dict]:
    """Serverless projects recovered by k360_infra (hex-id names fetch_org.py drops)."""
    named = {d["name"] for d in (org.get("deployments") or {}).get("items") or []}
    return [p for p in ((infra or {}).get("serverless") or {}).get("projects") or [] if p["project_id"] not in named]


def platform_section(org: dict, infra: dict | None) -> dict:
    deps = org.get("deployments") or {}
    svl_projects = serverless_projects(org, infra)
    ech_n = int(deps.get("ech_count") or 0)
    svl_n = int(deps.get("serverless_count") or 0) + len(svl_projects)
    ech_e = float(deps.get("ech_ecu") or 0)
    svl_e = float(deps.get("serverless_ecu") or 0) + sum(p["ecu"] for p in svl_projects)
    tot = ech_e + svl_e
    if ech_n and svl_n:
        label = "Serverless + ECH (hybrid)"
    elif svl_n:
        label = "Serverless"
    elif ech_n:
        label = "ECH"
    else:
        label = "No billed deployments"
    share = pct(ech_e, tot)
    svl_block = ((infra or {}).get("serverless") or {})
    return {
        "label": label,
        "ech_count": ech_n,
        "serverless_count": svl_n,
        "ech_ecu": r2(ech_e),
        "serverless_ecu": r2(svl_e),
        "total_ecu": r2(tot),
        "ech_share_pct": share,
        "serverless_share_pct": pct(svl_e, tot),
        "sizing_threshold_pct": SIZING_THRESHOLD_PCT,
        "sizing_triggered": share > SIZING_THRESHOLD_PCT,
        "serverless": {
            "by_solution": svl_block.get("by_solution") or {},
            "by_month": svl_block.get("by_month") or [],
            "types": svl_block.get("types") or [],
            "projects": [
                {k: p.get(k) for k in ("project_id", "solution", "template", "provider", "ecu", "created", "last_bill")}
                for p in sorted(svl_projects, key=lambda x: -x["ecu"])[:25]
            ],
            "note": "Serverless projects bill under 32-char hex ids with serverless.* SKUs; recovered via k360_infra.py",
        },
    }


def cloud_section(org: dict, infra: dict | None) -> dict:
    inf = infra or {}
    base = org.get("infrastructure") or {}
    summary = dict(inf.get("summary") or {})
    for k in ("provider", "region", "hot_instance"):
        if not summary.get(k):
            summary[k] = (base.get("summary") or {}).get(k)
    summary["template"] = (base.get("summary") or {}).get("template")
    templates = base.get("templates") or []
    by_dep = inf.get("by_deployment") or []
    # merge version/template/platform into per-dep rows
    meta = {d["name"]: d for d in (org.get("deployments") or {}).get("items") or []}
    rows = []
    for r in by_dep:
        m = meta.get(r["name"]) or {}
        rows.append(
            {
                **r,
                "version": m.get("version"),
                "template": m.get("template"),
                "platform": m.get("platform"),
                "use_case": m.get("use_case"),
            }
        )
    return {
        "summary": summary,
        "providers": inf.get("providers") or base.get("providers") or [],
        "regions": inf.get("regions") or base.get("regions") or [],
        "tiers": inf.get("tiers") or [],
        "instances": inf.get("instances") or [],
        "zones": inf.get("zones") or [],
        "plans": inf.get("plans") or [],
        "templates": templates,
        "capacity_ecu": inf.get("capacity_ecu"),
        "non_capacity": inf.get("non_capacity") or [],
        "non_capacity_ecu": inf.get("non_capacity_ecu"),
        "billing_types": (inf.get("billing_types") or [])[:12],
        "by_deployment": rows,
        "single_zone": [
            {"name": r["name"], "ecu": round(float(r.get("ecu") or 0), 2), "version": r.get("version"), "region": r.get("region")}
            for r in sorted(rows, key=lambda x: -float(x.get("ecu") or 0))
            if r.get("zones") == 1
        ],
    }


def ecu_section(org: dict, infra: dict | None, platform: dict) -> dict:
    chart = org.get("chart") or {}
    monthly = org.get("monthly") or {}
    totals = org.get("totals") or {}
    bd = org.get("burndown") or {}
    today = datetime.now(timezone.utc).date()

    # monthly totals = Σ named deployments per month + serverless projects (hex ids)
    months = list(monthly.get("months") or [])
    by_dep = monthly.get("by_deployment") or {}
    m_tot = [0.0] * len(months)
    for d in by_dep.values():
        for i, v in enumerate(d.get("ecu") or []):
            if i < len(m_tot):
                m_tot[i] += float(v or 0)
    svl_month = {m["month"]: float(m["ecu"] or 0) for m in (((infra or {}).get("serverless") or {}).get("by_month") or [])}
    svl_extra = float(platform.get("serverless_ecu") or 0) - float((org.get("deployments") or {}).get("serverless_ecu") or 0)
    if svl_extra > 0:
        for i, m in enumerate(months):
            m_tot[i] += svl_month.get(m, 0.0)
    m_tot = [round(v, 2) for v in m_tot]
    total_ecu = float(totals.get("ecu") or 0) + max(svl_extra, 0.0)
    rng = org.get("range") or {}
    start = rng.get("start") or ""
    cur_ym = today.strftime("%Y-%m")
    partial = {m for m in months if m == cur_ym or (start and m == start[:7] and not start.endswith("-01"))}
    complete = [(m, v) for m, v in zip(months, m_tot) if m not in partial and v > 0]
    peak = max(complete, key=lambda x: x[1]) if complete else (max(zip(months, m_tot), key=lambda x: x[1]) if months else (None, 0))
    low = min(complete, key=lambda x: x[1]) if complete else (None, 0)
    avg_complete = round(sum(v for _, v in complete) / len(complete), 2) if complete else None

    # daily
    days = list(chart.get("days") or [])
    tot = [float(x or 0) for x in (chart.get("total") or [])]
    grain = chart.get("grain") or "day"
    days_t, tot_t = _trim_trailing_zeros(days, tot)
    if grain == "day" and tot_t:
        daily_avg = round(sum(tot_t) / len(tot_t), 2)
        active = [v for v in tot_t if v > 0]
        daily_avg_active = round(sum(active) / len(active), 2) if active else 0.0
        i = max(range(len(tot_t)), key=lambda k: tot_t[k])
        daily_peak = {"day": days_t[i], "ecu": round(tot_t[i], 2)}
        last30 = tot_t[-30:]
        prev30 = tot_t[-60:-30]
        last_30d = round(sum(last30), 2)
        last_30d_avg = round(sum(last30) / len(last30), 2) if last30 else None
        prev_30d_avg = round(sum(prev30) / len(prev30), 2) if prev30 else None
        # weekly rollup for charts
        wk_days, wk_tot = [], []
        for k in range(0, len(days_t), 7):
            wk_days.append(days_t[k])
            wk_tot.append(round(sum(tot_t[k : k + 7]), 2))
    else:
        n_days = int(rng.get("days") or len(days_t) * 7 or 365)
        daily_avg = round(sum(tot_t) / n_days, 2) if tot_t else None
        daily_avg_active = None
        daily_peak = None
        last_30d = None
        last_30d_avg = bd.get("daily_30d")
        prev_30d_avg = None
        wk_days, wk_tot = days_t, [round(v, 2) for v in tot_t]

    trend = None
    if last_30d_avg and daily_avg:
        ratio = last_30d_avg / daily_avg
        trend = {
            "last_30d_vs_lookback_avg_pct": round((ratio - 1) * 100, 1),
            "last_30d_vs_prev_30d_pct": round((last_30d_avg / prev_30d_avg - 1) * 100, 1) if prev_30d_avg else None,
            "direction": "accelerating" if ratio > 1.15 else ("decelerating" if ratio < 0.85 else "steady"),
        }

    return {
        "lookback_days": rng.get("days"),
        "range": rng,
        "total_lookback": r2(total_ecu),
        "total_lookback_ech_named": r2(totals.get("ecu")),
        "total_lookback_serverless": r2(platform.get("serverless_ecu")),
        "months_in_lookback": totals.get("months"),
        "monthly": {"months": months, "totals": m_tot, "partial_months": sorted(partial)},
        "monthly_peak": {"month": peak[0], "ecu": r2(peak[1])} if peak and peak[0] else None,
        "monthly_low": {"month": low[0], "ecu": r2(low[1])} if low and low[0] else None,
        "monthly_avg_all": r2(total_ecu / max(int(totals.get("months") or len(months) or 1), 1)),
        "monthly_avg_complete_months": avg_complete,
        "daily_avg": daily_avg,
        "daily_avg_active_days": daily_avg_active,
        "daily_peak": daily_peak,
        "last_30d_total": last_30d,
        "last_30d_daily_avg": last_30d_avg,
        "trend": trend,
        "weekly_series": {"weeks": wk_days, "totals": wk_tot},
        "contract": {
            "commit_pace_ecu_per_day": bd.get("commit"),
            "required_pace_ecu_per_day": bd.get("required"),
            "consumption_rate": bd.get("consumption_rate"),
            "ecu_balance": bd.get("ecu_balance"),
            "runway_days": bd.get("runway_days"),
            "runout_date": bd.get("runout_date"),
            "nearest_contract_end": bd.get("nearest_contract_end"),
            "annual_projection": bd.get("annual_projection"),
        },
        "notes": [
            "ECU totals cover the lookback window (billing index), not contract-to-date",
            "monthly_peak / monthly_low exclude partial months",
            "quote $ only from estimator read-backs",
        ],
    }


def deployments_section(org: dict, ecu_sec: dict, infra: dict | None) -> dict:
    deps = org.get("deployments") or {}
    items = deps.get("items") or []
    svl_projects = serverless_projects(org, infra)
    by_ver_ecu: Counter[str] = Counter()
    by_ver_n: Counter[str] = Counter()
    for d in items:
        v = str(d.get("version") or "unknown")
        by_ver_n[v] += 1
        by_ver_ecu[v] += float(d.get("ecu") or 0)
    for p in svl_projects:
        by_ver_n["serverless"] += 1
        by_ver_ecu["serverless"] += float(p.get("ecu") or 0)
    tot_ecu = sum(by_ver_ecu.values())
    by_version = sorted(
        (
            {"version": v, "count": n, "ecu": r2(by_ver_ecu[v]), "ecu_pct": pct(by_ver_ecu[v], tot_ecu)}
            for v, n in by_ver_n.items()
        ),
        key=lambda x: (-x["count"], -x["ecu"]),
    )
    known = [v for v in by_ver_n if v not in ("unknown", "serverless")]
    latest = max(known, key=ver_tuple) if known else None
    # Reference = newest version (≥2 deps) within the newest *major* that holds ≥10% of
    # the fleet — a single canary on a newer release doesn't flag the whole fleet, but a
    # major that is genuinely being adopted does.
    n_deps = sum(by_ver_n.values())
    major_n: Counter[int] = Counter()
    for v in known:
        major_n[ver_tuple(v)[0]] += by_ver_n[v]
    adopted_majors = [m for m, n in major_n.items() if n >= max(2, round(0.10 * n_deps))]
    ref_major = max(adopted_majors) if adopted_majors else (ver_tuple(latest)[0] if latest else 0)
    adopted = [v for v in known if ver_tuple(v)[0] == ref_major and by_ver_n[v] >= 2]
    reference = max(adopted, key=ver_tuple) if adopted else (max((v for v in known if ver_tuple(v)[0] == ref_major), key=ver_tuple, default=latest))
    rt = ver_tuple(reference) if reference else (0,)
    majors: Counter[str] = Counter()
    for v, n in by_ver_n.items():
        majors[f"{ver_tuple(v)[0]}.x" if v not in ("unknown", "serverless") else v] += n
    lagging = []
    for v, n in by_ver_n.items():
        if v in ("unknown", "serverless"):
            continue
        t = ver_tuple(v)
        behind_major = t[0] < rt[0]
        behind_minor = t[0] == rt[0] and len(t) > 1 and len(rt) > 1 and (rt[1] - t[1]) >= 3
        eol = t[0] <= 7
        if behind_major or behind_minor or eol:
            lagging.append(
                {
                    "version": v,
                    "count": n,
                    "ecu": r2(by_ver_ecu[v]),
                    "reason": "EOL major" if eol else ("major behind" if behind_major else "≥3 minors behind"),
                }
            )
    lagging.sort(key=lambda x: (ver_tuple(x["version"])))

    # over time + trend
    ot = deps.get("over_time") or {}
    mo = ot.get("month") or {}
    periods = list(mo.get("periods") or [])
    counts = [int(c or 0) for c in (mo.get("count") or [])]
    partial = set((ecu_sec.get("monthly") or {}).get("partial_months") or [])
    comp = [(p, c) for p, c in zip(periods, counts) if p not in partial and c > 0]
    trend = {"flag": "insufficient data", "note": "need ≥4 complete months"}
    if len(comp) >= 4:
        k = min(3, len(comp) // 2)
        first = sum(c for _, c in comp[:k]) / k
        last = sum(c for _, c in comp[-k:]) / k
        delta = last - first
        dpct = round(100.0 * delta / first, 1) if first else 0.0
        if abs(dpct) >= SIGNIFICANT_PCT and abs(delta) >= SIGNIFICANT_ABS:
            flag = "significant increase" if delta > 0 else "significant reduction"
        elif abs(dpct) >= 10:
            flag = "moderate increase" if delta > 0 else "moderate reduction"
        else:
            flag = "stable"
        mom = []
        for (p0, c0), (p1, c1) in zip(comp, comp[1:]):
            mom.append((p1, c1 - c0))
        biggest = max(mom, key=lambda x: abs(x[1])) if mom else None
        trend = {
            "flag": flag,
            "window_months": k,
            "first_period": [comp[0][0], comp[k - 1][0]],
            "first_avg": round(first, 1),
            "last_period": [comp[-k][0], comp[-1][0]],
            "last_avg": round(last, 1),
            "delta": round(delta, 1),
            "delta_pct": dpct,
            "peak": {"month": max(comp, key=lambda x: x[1])[0], "count": max(c for _, c in comp)},
            "largest_mom_change": {"month": biggest[0], "delta": biggest[1]} if biggest else None,
        }

    top = sorted(items, key=lambda d: -float(d.get("ecu") or 0))[:15]
    return {
        "count": int(deps.get("count") or 0) + len(svl_projects),
        "ech_count": int(deps.get("ech_count") or 0),
        "serverless_count": int(deps.get("serverless_count") or 0) + len(svl_projects),
        "by_version": by_version,
        "version_summary": {
            "latest_in_fleet": latest,
            "reference_version": reference,
            "majors": dict(majors.most_common()),
            "lagging": lagging,
            "lagging_count": sum(x["count"] for x in lagging),
            "lagging_ecu_pct": pct(sum(x["ecu"] for x in lagging), tot_ecu),
        },
        "over_time": {
            "month": {"periods": periods, "count": counts},
            "week": ot.get("week") or {},
            "serverless_month": [{"month": m["month"], "projects": m["projects"]} for m in (((infra or {}).get("serverless") or {}).get("by_month") or [])],
            "note": (ot.get("note") or "") + " · ECH named deployments only; serverless projects per month in serverless_month",
        },
        "trend": trend,
        "top": [
            {
                "name": d["name"],
                "version": d.get("version"),
                "platform": d.get("platform"),
                "use_case": d.get("use_case"),
                "template": d.get("template"),
                "ecu": r2(d.get("ecu")),
                "ecu_pct": pct(float(d.get("ecu") or 0), tot_ecu),
                "ecu_monthly_avg": d.get("ecu_monthly_avg"),
                "months_active": d.get("months_active"),
            }
            for d in top
        ],
    }


def storage_section(org: dict, cloud: dict) -> dict:
    st = org.get("storage") or {}
    deps = {d["name"]: d for d in (org.get("deployments") or {}).get("items") or []}
    rows = []
    for r in st.get("by_deployment") or []:
        d = deps.get(r["name"]) or {}
        gb = float(r.get("data_stored_gb") or 0)
        mo = float(d.get("ecu_monthly_avg_active") or d.get("ecu_monthly_avg") or 0)
        rows.append(
            {
                **{k: r.get(k) for k in ("name", "as_of", "data_stored_gb", "docs_count", "avg_doc_kb")},
                "ecu_monthly_avg": r2(mo),
                "ecu_per_gb_mo": round(mo / gb, 3) if gb else None,
            }
        )
    rows.sort(key=lambda x: -(x["data_stored_gb"] or 0))
    total_gb = float(st.get("data_stored_gb") or 0)
    top_share = pct(rows[0]["data_stored_gb"], total_gb) if rows and total_gb else 0.0
    tiers = {t["tier"]: t for t in cloud.get("tiers") or []}
    cap = float(cloud.get("capacity_ecu") or 0)
    tier_mix = {
        t: {"ecu": tiers[t]["ecu"], "pct": tiers[t]["pct"]} for t in ("datahot", "datawarm", "datacold", "datafrozen") if t in tiers
    }
    org_mo = float((org.get("totals") or {}).get("ecu_monthly_avg") or 0)
    stale_days = None
    if st.get("as_of"):
        try:
            stale_days = (datetime.now(timezone.utc).date() - date.fromisoformat(str(st["as_of"])[:10])).days
        except ValueError:
            stale_days = None
    notes = list(st.get("notes") or [])
    if stale_days is not None and stale_days > 45:
        notes.insert(0, f"Telemetry snapshot is {stale_days} days old — storage figures may be stale")
    return {
        "as_of": st.get("as_of"),
        "telemetry_age_days": stale_days,
        "deployments_with_telemetry": st.get("deployments_with_telemetry"),
        "data_stored_gb": r2(total_gb),
        "data_stored_tb": round(total_gb / 1024, 2),
        "docs_count": st.get("docs_count"),
        "avg_doc_kb": st.get("avg_doc_kb"),
        "ecu_per_gb_mo_org": round(org_mo / total_gb, 3) if total_gb else None,
        "top_deployment_share_pct": top_share,
        "tier_mix": tier_mix,
        "has_frozen": "datafrozen" in tiers,
        "has_warm_or_cold": any(t in tiers for t in ("datawarm", "datacold")),
        "frozen_pct_of_capacity": tiers.get("datafrozen", {}).get("pct", 0.0) if cap else 0.0,
        "snapshot_storage_ecu": next((t["ecu"] for t in cloud.get("non_capacity") or [] if t["type"] == "snapshot-storage"), 0.0),
        "by_deployment": rows,
        "monthly": st.get("monthly") or {},
        "notes": notes,
    }


def solutions_section(org: dict, infra: dict | None) -> dict:
    items = (org.get("deployments") or {}).get("items") or []
    stor = {s["name"]: float(s.get("data_stored_gb") or 0) for s in (org.get("storage") or {}).get("by_deployment") or []}
    agg = {k: {"deployments": 0, "ecu": 0.0, "gb": 0.0, "gb_known_deps": 0, "ech_ecu": 0.0, "serverless_ecu": 0.0} for k in ("observability", "search", "security")}
    rows = []
    methods: Counter[str] = Counter()
    for p in serverless_projects(org, infra):
        a = agg[p["solution"]]
        a["deployments"] += 1
        a["ecu"] += float(p["ecu"] or 0)
        a["serverless_ecu"] += float(p["ecu"] or 0)
        methods["serverless_template"] += 1
        rows.append(
            {
                "name": p["project_id"],
                "solution": p["solution"],
                "method": "serverless_template",
                "platform": "serverless",
                "use_case": p.get("template"),
                "ecu": r2(p["ecu"]),
                "ecu_monthly_avg": None,
                "gb": 0.0,
                "ml_ecu": 0,
                "inference_ecu": r2(sum(v for k, v in (p.get("types") or {}).items() if k == "inference")),
            }
        )
    for d in items:
        sol, method = solution_for(d)
        ecu = float(d.get("ecu") or 0)
        gb = stor.get(d["name"], 0.0)
        a = agg[sol]
        a["deployments"] += 1
        a["ecu"] += ecu
        a["gb"] += gb
        if (d.get("platform") or "ech") == "serverless":
            a["serverless_ecu"] += ecu
        else:
            a["ech_ecu"] += ecu
        if d["name"] in stor:
            a["gb_known_deps"] += 1
        methods[method] += 1
        rows.append(
            {
                "name": d["name"],
                "solution": sol,
                "method": method,
                "platform": d.get("platform"),
                "use_case": d.get("use_case"),
                "ecu": r2(ecu),
                "ecu_monthly_avg": d.get("ecu_monthly_avg"),
                "gb": round(gb, 2),
                "ml_ecu": d.get("ml_ecu") or 0,
                "inference_ecu": (d.get("ai") or {}).get("inference_ecu") or 0,
            }
        )
    tot = sum(a["ecu"] for a in agg.values())
    fam = org.get("families") or {}
    ml = org.get("ml") or {}
    ai = org.get("ai") or {}
    out = {
        k: {
            "deployments": v["deployments"],
            "ecu": r2(v["ecu"]),
            "ecu_pct": pct(v["ecu"], tot),
            "ecu_monthly_avg": r2(v["ecu"] / max(int((org.get("totals") or {}).get("months") or 1), 1)),
            "ech_ecu": r2(v["ech_ecu"]),
            "serverless_ecu": r2(v["serverless_ecu"]),
            "gb": round(v["gb"], 2),
            "gb_known_deps": v["gb_known_deps"],
        }
        for k, v in agg.items()
    }
    rows.sort(key=lambda x: -x["ecu"])
    return {
        "method": "serverless template → use_case (features) → billing family → name hints → default search",
        "method_counts": dict(methods),
        "totals": out,
        "cross_cutting": {
            "genai_eis_inference_ecu": r2(ai.get("billing_ecu_total")),
            "ml_node_ecu": r2(ml.get("total_ecu")),
            "note": "EIS inference + ML node ECU are included in their deployment's solution total (shown here as 'of which')",
        },
        "billing_families_raw": {k: r2(v) for k, v in fam.items()},
        "offload_by_kind": (org.get("offload") or {}).get("by_kind"),
        "by_deployment": rows,
    }


def ai_ml_section(org: dict) -> dict:
    ml = org.get("ml") or {}
    ai = org.get("ai") or {}
    items = {d["name"]: d for d in (org.get("deployments") or {}).get("items") or []}
    rows = {}
    dep_skus: list[tuple[str, str, list[dict]]] = []
    for d in items.values():
        a = d.get("ai") or {}
        ml_e = float(d.get("ml_ecu") or 0)
        inf_e = float(a.get("inference_ecu") or 0)
        flags = {k: bool(a.get(k)) for k in ("elser", "vector_mappings", "genai_connector", "playground", "ai_assistant", "attack_discovery")}
        skus = [s for raw in (a.get("inference_skus") or []) if (s := _annotate_sku(raw))]
        sol, _method = solution_for(d)
        dep_skus.append((d["name"], sol, skus))
        model_ids = sorted({s["model"] for s in skus if s.get("model")})
        model_labels = []
        for mid in model_ids:
            label = next((s.get("model_label") or mid for s in skus if s.get("model") == mid), mid)
            if label not in model_labels:
                model_labels.append(label)
        million = round(sum(s["million_tokens"] for s in skus), 4)
        if ml_e <= 0 and inf_e <= 0 and not any(flags.values()) and not model_ids and not skus:
            continue
        tot = ml_e + inf_e
        rows[d["name"]] = {
            "name": d["name"],
            "version": d.get("version"),
            "solution": sol,
            "ecu": r2(d.get("ecu")),
            "ml_node_ecu": r2(ml_e),
            "eis_inference_ecu": round(inf_e, 4),
            "million_tokens": million,
            "ai_ecu_total": round(tot, 2),
            "ai_pct_of_dep": pct(tot, float(d.get("ecu") or 0)),
            "models": model_ids,
            "model_labels": model_labels,
            "ml_skus": [s.get("label") for s in (d.get("ml_skus") or [])[:4]],
            **flags,
        }
    by_dep = sorted(rows.values(), key=lambda x: (-x["ai_ecu_total"], x["name"]))
    org_skus = [s for raw in (ai.get("inference_skus") or []) if (s := _annotate_sku(raw))]
    meter = _token_meter(org, dep_skus)
    counts: Counter[str] = Counter()
    labels: dict[str, str] = {}
    unnamed = 0
    for row in by_dep:
        if not row.get("vector_mappings"):
            continue
        if row.get("elser"):
            counts["ELSER (in-cluster)"] += 1
            labels["ELSER (in-cluster)"] = "ELSER (in-cluster)"
        if row.get("models"):
            for mid, label in zip(row["models"], row.get("model_labels") or row["models"]):
                counts[mid] += 1
                labels[mid] = label
        elif not row.get("elser"):
            counts["(unnamed — vectors only)"] += 1
            labels["(unnamed — vectors only)"] = "(unnamed — vectors only)"
            unnamed += 1
    org_ecu = float((org.get("totals") or {}).get("ecu") or 0)
    ml_tot = float(ml.get("total_ecu") or 0)
    inf_tot = float(ai.get("billing_ecu_total") or 0)
    return {
        "ml_node_ecu": r2(ml_tot),
        "ml_node_pct_of_org": pct(ml_tot, org_ecu, 2),
        "eis_inference_ecu": r2(inf_tot),
        "eis_inference_pct_of_org": pct(inf_tot, org_ecu, 3),
        "ai_ecu_total": r2(ml_tot + inf_tot),
        "feature_counts": {k: ai.get(k) for k in ("elser", "vector_mappings", "genai_connector", "playground", "ai_assistant", "attack_discovery")},
        "models": sorted({s["model"] for s in org_skus if s.get("model")}),
        "model_counts": [
            {"model": labels.get(m, m), "deployments": c} for m, c in counts.most_common()
        ],
        "vector_deployments": sum(1 for row in by_dep if row.get("vector_mappings")),
        "unnamed_vector_models": unnamed,
        "inference_skus": org_skus,
        "token_meter": meter,
        "ml_skus": (ml.get("skus") or [])[:8],
        "by_deployment": by_dep,
        "notes": (ml.get("notes") or []) + (ai.get("notes") or []),
    }


# --------------------------------------------------------------------------- analysis
def _rec(title: str, severity: str, evidence: str, action: str, impact: str | None = None) -> dict:
    return {"title": title, "severity": severity, "evidence": evidence, "action": action, "impact": impact}


_SEV = {"high": 0, "medium": 1, "low": 2}


def performance_recs(acc: dict) -> list[dict]:
    recs: list[dict] = []
    deps, cloud, stor, wl, aiml = acc["deployments"], acc["cloud"], acc["storage"], acc["workload"], acc["ai_ml"]
    vs = deps["version_summary"]
    if vs["lagging_count"]:
        eol = [x for x in vs["lagging"] if x["reason"] == "EOL major"]
        sev = "high" if eol else "medium"
        recs.append(
            _rec(
                f"Upgrade {vs['lagging_count']} deployment(s) on lagging stack versions",
                sev,
                ", ".join(f"{x['version']}×{x['count']} ({x['reason']})" for x in vs["lagging"][:6])
                + f"; fleet reference {vs['reference_version']} (latest {vs['latest_in_fleet']}); {vs['lagging_ecu_pct']}% of ECU",
                "Plan rolling upgrades to the fleet's current minor; prioritise EOL majors. Newer versions bring LogsDB/TSDS, better vector search and ES|QL performance.",
                "performance + supportability",
            )
        )
    search = wl.get("search") or {}
    pk = search.get("peak") or {}
    if pk.get("complexity") == "High":
        recs.append(
            _rec(
                "High average query cost at peak",
                "high",
                f"peak complexity {pk.get('complexity_ms')} ms/query @ {pk.get('qps')} QPS",
                "Profile top queries (slow log / Profile API); review aggregations, wildcard/regex, script fields; add request cache / query-time filters.",
                "latency",
            )
        )
    elif pk.get("complexity") == "Medium" and (pk.get("qps") or 0) > 1000:
        recs.append(
            _rec(
                "Medium query cost at high QPS",
                "medium",
                f"{pk.get('complexity_ms')} ms/query @ {pk.get('qps')} QPS peak",
                "Review shard sizing (target 10–50 GB) and index sorting; consider dedicated coordinating nodes if not present.",
                "latency / headroom",
            )
        )
    adk = float(stor.get("avg_doc_kb") or 0)
    if adk and adk < 0.3:
        recs.append(
            _rec(
                "Very small documents (high per-doc overhead)",
                "medium",
                f"org avg doc {adk} KB across {stor.get('docs_count')} docs",
                "Where data is metrics/logs: adopt TSDS (metrics) and LogsDB index mode (logs) to cut index overhead; review mappings for unused fields.",
                "indexing + storage efficiency",
            )
        )
    elif adk > 4:
        recs.append(
            _rec(
                "Large average document size",
                "low",
                f"org avg doc {adk} KB",
                "Check for over-indexed fields (`index: false` for non-searched fields, `_source` synthetic mode where supported), trim payloads at ingest.",
                "indexing throughput",
            )
        )
    if stor.get("data_stored_tb", 0) >= 5 and not stor.get("has_frozen") and not stor.get("has_warm_or_cold"):
        recs.append(
            _rec(
                "All data on the hot tier",
                "high",
                f"{stor['data_stored_tb']} TB stored; no warm/cold/frozen capacity SKUs billed",
                "Introduce ILM with a frozen tier (searchable snapshots) for data older than the hot query window; keeps hot nodes for ingest + recent search.",
                "hot-node headroom + cost",
            )
        )
    if (stor.get("top_deployment_share_pct") or 0) >= 60 and (deps.get("count") or 0) > 3:
        top = stor["by_deployment"][0]
        recs.append(
            _rec(
                "Storage concentrated in one deployment",
                "medium",
                f"{top['name']} holds {stor['top_deployment_share_pct']}% of org data ({top['data_stored_gb']} GB)",
                "Review shard count/size and ILM on this cluster; consider splitting by data stream / tenant or offloading cold data to frozen.",
                "resilience + recovery time",
            )
        )
    inst = [i for i in cloud.get("instances") or [] if i.get("tier") == "datahot"]
    if len(inst) >= 3:
        recs.append(
            _rec(
                "Mixed hot-tier instance families",
                "low",
                "; ".join(f"{i['instance']} {i['pct']}%" for i in inst[:4]),
                "Standardise on the current-generation family for the dominant workload (CPU-optimised for search, storage-optimised for logs) to simplify capacity planning.",
                "consistency",
            )
        )
    z1_deps = cloud.get("single_zone") or []
    if z1_deps:
        z1 = next((z for z in cloud.get("zones") or [] if z.get("zones") == 1), None)
        names = ", ".join(r["name"] for r in z1_deps[:6])
        more = f" and {len(z1_deps) - 6} more" if len(z1_deps) > 6 else ""
        recs.append(
            _rec(
                f"Single-zone deployments: {len(z1_deps)}",
                "medium" if (z1 and z1.get("pct", 0) >= 10) or len(z1_deps) >= 3 else "low",
                f"{len(z1_deps)} deployment(s) billed in 1 availability zone"
                + (f" ({z1['pct']}% of capacity ECU)" if z1 else "")
                + f": {names}{more}",
                "Move production deployments to 2–3 zones for HA; keep single-zone for dev/test only.",
                "availability",
            )
        )
    for row in aiml.get("by_deployment") or []:
        if row["ml_node_ecu"] > 0 and not (row["elser"] or row["vector_mappings"] or row["models"]):
            recs.append(
                _rec(
                    f"ML nodes billed without ML/vector features: {row['name']}",
                    "low",
                    f"ML node ECU {row['ml_node_ecu']} ({row['ai_pct_of_dep']}% of deployment); no ELSER / vectors / EIS models detected",
                    "Confirm anomaly-detection or inference jobs are running; otherwise scale ML nodes down.",
                    "right-sizing",
                )
            )
            break
    ing = wl.get("ingest") or {}
    if ing.get("active_hours_mo") and ing["active_hours_mo"] < 400:
        recs.append(
            _rec(
                "Bursty ingest pattern",
                "low",
                f"ingest active ~{ing['active_hours_mo']} h/month",
                "Enable autoscaling on hot tier or schedule bulk loads; serverless ingest pricing would track the bursts.",
                "headroom",
            )
        )
    recs.sort(key=lambda r: _SEV[r["severity"]])
    return recs[:5]


# Billed rate of global.inference-chat-input_anthropic-claude-4-5-haiku.
HAIKU_INPUT_ECU_PER_M = 1.50
# Target for the prompt cut: system prompt, history, and retrieved passages.
INPUT_PROMPT_KEEP = 0.5


def _input_model_label(sku: dict) -> str:
    if sku.get("model_label"):
        return str(sku["model_label"])
    name = str(sku.get("sku") or "").lower()
    if "haiku" in name:
        return "Claude Haiku"
    if "sonnet" in name:
        return "Claude Sonnet"
    if "rainbow-sprinkles" in name:
        return "Elastic Managed LLM"
    if name.endswith("chat-input") or "chat-input_" not in name and "chat-input" in name:
        return "Chat"
    return str(sku.get("label") or "Chat")


def _chat_input_lines(acc: dict) -> list[dict]:
    """Chat-input lines from the token meter. Rates are recomputed from ECU and quantity."""
    rows = []
    for r in ((acc.get("ai_ml") or {}).get("token_meter") or {}).get("rows") or []:
        if r.get("kind") != "chat_input":
            continue
        qty = float(r.get("million_tokens") or 0)
        ecu = float(r.get("ecu") or 0)
        if qty <= 0 or ecu <= 0:
            continue
        label = r.get("model_label") or "Chat"
        name = str(r.get("model") or label)
        rows.append({"label": label, "sku": name, "qty": qty, "ecu": ecu, "rate": ecu / qty})
    if rows:
        return rows
    for sku in (acc.get("ai_ml") or {}).get("inference_skus") or []:
        if sku.get("kind") != "chat_input":
            continue
        qty = float(sku.get("million_tokens") if sku.get("million_tokens") is not None else sku.get("qty") or 0)
        ecu = float(sku.get("ecu") or 0)
        if qty <= 0 or ecu <= 0:
            continue
        name = str(sku.get("sku") or sku.get("label") or "")
        rows.append({"label": _input_model_label(sku), "sku": name, "qty": qty, "ecu": ecu, "rate": ecu / qty})
    return rows


def _input_cost_rec(acc: dict) -> dict | None:
    """Shorter prompts, then Haiku for what does not need the larger model.

    The prompt cut keeps half the input tokens. Tokens still on a model billed
    above the Haiku input rate are repriced at that rate. Haiku tokens only
    get the prompt cut. Quantities come from the token meter.
    """
    rows = _chat_input_lines(acc)
    haiku_rate = None
    haiku_ecu = -1.0
    for r in rows:
        name = f"{r.get('sku') or ''} {r.get('label') or ''}"
        # Several Haiku lines can be on the bill. The rate used for the cut is the largest line.
        if "haiku" in name.lower() and r["ecu"] > haiku_ecu:
            haiku_rate = r["rate"]
            haiku_ecu = r["ecu"]
    if not rows:
        return None
    target = haiku_rate if haiku_rate is not None else HAIKU_INPUT_ECU_PER_M
    total_ecu = sum(r["ecu"] for r in rows)
    if total_ecu < 10:
        return None
    kept_ecu = 0.0
    for r in rows:
        kept_qty = r["qty"] * INPUT_PROMPT_KEEP
        kept_rate = target if r["rate"] > target + 0.05 else r["rate"]
        kept_ecu += kept_qty * kept_rate
    save = total_ecu - kept_ecu
    if save < 1:
        return None
    months = max(float((acc.get("ecu") or {}).get("lookback_days") or 365) / 30.416, 1.0)
    deps = (acc.get("ai_ml") or {}).get("by_deployment") or []
    top = max(deps, key=lambda d: float(d.get("eis_inference_ecu") or 0)) if deps else None
    where = ""
    if top and float(top.get("eis_inference_ecu") or 0) > 0:
        where = f" {top['name']} holds {float(top['eis_inference_ecu']):,.0f} ECU of inference"
        if top.get("playground"):
            where += ", with Playground on"
        where += "."
    pricey = [r for r in rows if r["rate"] > target + 0.05 and r["ecu"] >= 1]
    pricey.sort(key=lambda r: -r["ecu"])
    bits = ", ".join(f"{r['label']} {r['qty']:.1f} million tokens at {r['rate']:.2f} ECU/million ({r['ecu']:,.0f} ECU)" for r in pricey[:2])
    rate_src = "the Haiku rate already on this bill" if haiku_rate is not None else "the Haiku input rate"
    evidence = f"Chat input is {total_ecu:,.0f} ECU over the lookback.{where}"
    if bits:
        evidence += f" {bits}."
    evidence += f" What remains is priced at {target:.2f} ECU per million, {rate_src}."
    return _rec(
        "Cut chat input cost: shorter prompts, then Haiku",
        "medium" if save >= 20 else "low",
        evidence,
        "Halve the text sent on each call: the system prompt, conversation history, and retrieved passages. Send the remaining calls that do not need the larger model to Claude Haiku.",
        f"~{save:,.0f} ECU over the lookback (~{save / months:,.0f} ECU/mo). Half the input tokens are dropped, then any tokens that were on a larger model are priced at the Haiku rate.",
    )


def cost_recs(acc: dict, org: dict) -> list[dict]:
    recs: list[dict] = []
    plat, cloud, stor, deps, aiml, ecu = acc["platform"], acc["cloud"], acc["storage"], acc["deployments"], acc["ai_ml"], acc["ecu"]
    fs = ((org.get("offload") or {}).get("full_split")) or {}
    org_mo = float(ecu.get("monthly_avg_complete_months") or ecu.get("monthly_avg_all") or 0)
    if plat["ech_count"] and fs.get("save_vs_ech_yr") and fs["save_vs_ech_yr"] > 0:
        sol_tot = acc["solutions"]["totals"]
        dominant = max(sol_tot, key=lambda k: sol_tot[k]["ecu"])
        target = {
            "observability": "Observability Serverless (Complete)",
            "security": "Security Serverless (Complete)",
            "search": "Search Serverless",
        }[dominant]
        ev = (
            f"{dominant} = {sol_tot[dominant]['ecu_pct']}% of ECU; org-report ballpark: multi-project serverless "
            f"≈ ${fs['serverless_total_mo']:,.0f}/mo vs ECH {fs['full_ech_mo']:,.0f} ECU/mo (directional — kind split uses name hints)"
        )
        recs.append(
            _rec(
                f"Move {dominant} workloads to {target}",
                "high",
                ev,
                "Quote from the copied estimators in the sizing folder (ECH>50% rule); migrate the dominant slice first (hybrid), keep latency-sensitive search on ECH; never put all data on ES3.",
                f"≈ ${fs['save_vs_ech_yr']:,.0f}/yr full split (ballpark, pre-estimator)",
            )
        )
    nc = {t["type"]: t for t in cloud.get("non_capacity") or []}
    inter = nc.get("data-transfer-inter-node")
    if inter and inter.get("pct", 0) >= 5:
        recs.append(
            _rec(
                "Inter-node data transfer is a material cost line",
                "high" if inter["pct"] >= 10 else "medium",
                f"data-transfer-inter-node = {inter['ecu']:,.0f} ECU ({inter['pct']}% of spend)",
                "Reduce cross-zone chatter: fewer/larger shards, avoid over-replication (replicas=1), batch bulk requests, review CCR/CCS fan-out; check for hot-spotting.",
                f"up to ~{inter['ecu'] * 0.3 / max(int(ecu.get('months_in_lookback') or 12), 1):,.0f} ECU/mo at 30% reduction",
            )
        )
    out = nc.get("data-transfer-out")
    if out and out.get("pct", 0) >= 3:
        recs.append(
            _rec(
                "Egress (data-transfer-out) above typical",
                "medium",
                f"{out['ecu']:,.0f} ECU ({out['pct']}%)",
                "Co-locate consumers with the deployment region, compress responses, use `_source` filtering / `fields` to shrink payloads.",
                "egress",
            )
        )
    if stor.get("data_stored_tb", 0) >= 2 and (stor.get("frozen_pct_of_capacity") or 0) < 5:
        recs.append(
            _rec(
                "Shift aged data to the frozen tier",
                "high" if stor["data_stored_tb"] >= 20 else "medium",
                f"{stor['data_stored_tb']} TB stored; frozen = {stor.get('frozen_pct_of_capacity', 0)}% of capacity ECU; hot = {(stor.get('tier_mix') or {}).get('datahot', {}).get('pct', 0)}%",
                "ILM: hot 7–30 d → frozen (searchable snapshots) for retention. Frozen is typically 5–10× cheaper per GB than hot.",
                "storage",
            )
        )
    items = (org.get("deployments") or {}).get("items") or []
    n_months = max(int((org.get("totals") or {}).get("months") or 1), 1)
    small = [d for d in items if float(d.get("ecu") or 0) < 0.01 * float((org.get("totals") or {}).get("ecu") or 0)]
    if len(small) >= 8:
        s_ecu = sum(float(d.get("ecu") or 0) for d in small)
        recs.append(
            _rec(
                f"Consolidate {len(small)} small deployments (<1% of spend each)",
                "medium",
                f"together {s_ecu:,.0f} ECU over lookback (~{s_ecu / n_months:,.0f}/mo); each carries master/Kibana overhead",
                "Merge dev/test and low-traffic tenants into shared deployments (or serverless projects) to remove per-deployment fixed overhead.",
                f"~{s_ecu * 0.25 / n_months:,.0f} ECU/mo at 25% overhead removal",
            )
        )
    stale = [d for d in items if (d.get("months_active") or 0) >= 2 and float(d.get("ecu_monthly_avg_active") or 0) > 0 and float(d.get("ecu_monthly_avg_active") or 0) < 0.002 * org_mo]
    if stale:
        recs.append(
            _rec(
                f"Review {len(stale)} near-idle deployment(s)",
                "low",
                ", ".join(d["name"] for d in stale[:5]) + (" …" if len(stale) > 5 else ""),
                "Confirm ownership; decommission or snapshot-and-delete if unused.",
                "housekeeping",
            )
        )
    for row in aiml.get("by_deployment") or []:
        if row["ml_node_ecu"] > 0 and not (row["elser"] or row["vector_mappings"] or row["models"]) and row["ai_pct_of_dep"] >= 5:
            recs.append(
                _rec(
                    f"ML node spend without detected ML usage: {row['name']}",
                    "medium",
                    f"{row['ml_node_ecu']:,.0f} ECU ML nodes = {row['ai_pct_of_dep']}% of deployment",
                    "Validate running ML jobs; downsize or remove ML nodes, or move inference to EIS (pay-per-token).",
                    f"~{row['ml_node_ecu'] / n_months:,.0f} ECU/mo",
                )
            )
            break
    vs = deps["version_summary"]
    if vs["lagging_count"] and stor.get("data_stored_tb", 0) >= 1:
        recs.append(
            _rec(
                "Upgrade to unlock LogsDB / TSDS storage savings",
                "medium",
                f"{vs['lagging_count']} deployment(s) behind {vs['reference_version']}; {stor['data_stored_tb']} TB stored",
                "LogsDB index mode (8.17+/9.x) and TSDS reduce logs/metrics storage ~50–65%; pair with synthetic _source.",
                "storage + indexing",
            )
        )
    snap = stor.get("snapshot_storage_ecu") or 0
    if snap and ecu.get("total_lookback") and snap / ecu["total_lookback"] >= 0.02:
        recs.append(
            _rec(
                "Snapshot storage growing",
                "low",
                f"snapshot-storage {snap:,.0f} ECU ({pct(snap, ecu['total_lookback'])}% of spend)",
                "Tighten SLM retention (e.g. 7 daily / 4 weekly); delete orphaned repositories.",
                "storage",
            )
        )
    recs.sort(key=lambda r: _SEV[r["severity"]])
    input_rec = _input_cost_rec(acc)
    if not input_rec:
        return recs[:5]
    return recs[:4] + [input_rec]


def burndown_analysis(acc: dict, org: dict) -> dict:
    bd = org.get("burndown") or {}
    ecu = acc["ecu"]
    flags: list[dict] = []
    rate = float(bd.get("consumption_rate") or 0)
    rate_ok = math.isfinite(rate)
    if not rate_ok or (not bd.get("commit") and bd.get("daily_30d")):
        rate = 0.0
        flags.append({"severity": "medium", "flag": "No commit pace in K360", "detail": f"pace.commit is 0/empty (K360 rate = {bd.get('consumption_rate')}); actual {bd.get('daily_30d')} ECU/day — check contract data in the org pivot (PAYG / expired / missing contract?)"})
    if rate > 1.0:
        flags.append({"severity": "high", "flag": "Burning faster than commit", "detail": f"consumption_rate {rate:.2f}× (actual {bd.get('daily_30d')} vs commit pace {bd.get('commit')} ECU/day)"})
    elif 0 < rate < 0.6:
        flags.append({"severity": "medium", "flag": "Under-consuming commit", "detail": f"consumption_rate {rate:.2f}× — risk of unused prepaid ECU at term end; expansion/use-case conversation"})
    if bd.get("runout_date") and bd.get("nearest_contract_end") and bd["runout_date"] < bd["nearest_contract_end"]:
        flags.append({"severity": "high", "flag": "Projected runout before contract end", "detail": f"runout {bd['runout_date']} < contract end {bd['nearest_contract_end']}"})
    rw = bd.get("runway_days")
    if rw is not None and rw < 90:
        flags.append({"severity": "high" if rw < 45 else "medium", "flag": f"Runway {rw} days", "detail": f"ECU balance {bd.get('ecu_balance')}; required pace {bd.get('required')} ECU/day"})
    tr = ecu.get("trend") or {}
    if tr.get("direction") == "accelerating":
        flags.append({"severity": "medium", "flag": "Consumption accelerating", "detail": f"last 30d daily avg {ecu.get('last_30d_daily_avg')} is {tr.get('last_30d_vs_lookback_avg_pct')}% above lookback average"})
    elif tr.get("direction") == "decelerating":
        flags.append({"severity": "low", "flag": "Consumption decelerating", "detail": f"last 30d daily avg {ecu.get('last_30d_daily_avg')} is {abs(tr.get('last_30d_vs_lookback_avg_pct') or 0)}% below lookback average"})
    mp = ecu.get("monthly_peak") or {}
    avg = ecu.get("monthly_avg_complete_months") or 0
    if mp.get("ecu") and avg and mp["ecu"] > 1.5 * avg:
        flags.append({"severity": "low", "flag": "Spiky monthly consumption", "detail": f"peak month {mp['month']} = {mp['ecu']:,.0f} ECU vs avg {avg:,.0f}"})
    dt = acc["deployments"].get("trend") or {}
    if dt.get("flag", "").startswith("significant"):
        flags.append({"severity": "low", "flag": f"Deployment count: {dt['flag']}", "detail": f"{dt.get('first_avg')} → {dt.get('last_avg')} ({dt.get('delta_pct')}%)"})
    level = bd.get("level") or ("OK" if not flags else "WATCH")
    if not rate_ok:
        level = "WATCH (no commit pace)"
    return {
        "level": level,
        "k360_reasons": [str(r) for r in (bd.get("reasons") or [])],
        "flags": flags,
        "consumption_rate": round(rate, 2) if rate_ok else None,
        "runway_days": rw,
        "runout_date": bd.get("runout_date"),
        "nearest_contract_end": bd.get("nearest_contract_end"),
        "ecu_balance": bd.get("ecu_balance"),
        "daily_30d": bd.get("daily_30d"),
        "annual_projection": bd.get("annual_projection"),
        "commit_pace": bd.get("commit"),
        "required_pace": bd.get("required"),
        "series": burndown_series(ecu, bd),
    }


def _next_month(ym: str) -> str:
    y, m = (int(x) for x in ym.split("-")[:2])
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}"


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def year_projections(acc: dict, when: date) -> dict:
    """ECU 3 and 6 months ahead at the trailing year's complete-month pace.

    Partial months stay out of the average. Dollars use ARR ÷ (commit × 365)
    when both exist, and are omitted otherwise. This pace is not the last-30-day rate.
    """
    ecu = acc.get("ecu") or {}
    bd = (acc.get("analysis") or {}).get("burndown") or {}
    monthly = ecu.get("monthly") or {}
    months = [str(m) for m in (monthly.get("months") or [])]
    totals = [float(v or 0) for v in (monthly.get("totals") or [])]
    partial = {str(m) for m in (monthly.get("partial_months") or [])}
    complete_n = sum(1 for m, v in zip(months, totals) if m not in partial and v > 0)
    lookback_days = int(ecu.get("lookback_days") or 0)
    org = (acc.get("meta") or {}).get("org") or {}
    arr = float(org.get("ARR") or 0)
    commit = float(bd.get("commit_pace") or 0)
    rate = (arr / (commit * 365.0)) if arr > 0 and commit > 0 else None
    avg = ecu.get("monthly_avg_complete_months")
    if not avg or float(avg) <= 0:
        return {
            "basis": "unavailable",
            "lookback_days": lookback_days or None,
            "complete_months": complete_n,
            "horizons": [],
            "forward": [],
            "note": "No complete-month average in the lookback, so 3- and 6-month projections were not computed.",
        }
    avg = float(avg)
    daily = avg / DAYS_PER_MONTH
    balance = bd.get("ecu_balance")
    balance_f = float(balance) if balance is not None else None

    def horizon(n: int) -> dict:
        ecu_out = round(avg * n, 2)
        row = {
            "months": n,
            "as_of": _add_months(when, n).isoformat(),
            "ecu": ecu_out,
            "balance": round(max(balance_f - ecu_out, 0.0), 2) if balance_f is not None else None,
        }
        if rate is not None:
            row["usd"] = round(ecu_out * rate, 2)
        return row

    forward = []
    bal = balance_f
    ym = when.strftime("%Y-%m")
    for _ in range(6):
        ym = _next_month(ym)
        if bal is not None:
            bal = max(bal - avg, 0.0)
        step = {"month": ym, "ecu": round(avg, 2), "balance": round(bal, 2) if bal is not None else None}
        if rate is not None:
            step["usd"] = round(avg * rate, 2)
        forward.append(step)

    runway = round(balance_f / daily) if balance_f is not None and daily > 0 else None
    dollar = "Dollars use ARR ÷ (commit ECU/day × 365), applied to projected ECU only." if rate else "No ARR or commit pace, so the projection is ECU only."
    return {
        "basis": "complete-month average of the trailing year",
        "lookback_days": lookback_days,
        "complete_months": complete_n,
        "partial_months": sorted(partial),
        "ecu_per_month": round(avg, 2),
        "ecu_per_day": round(daily, 2),
        "usd_per_ecu": round(rate, 3) if rate else None,
        "runway_days": runway,
        "runout_date": (when + timedelta(days=runway)).isoformat() if runway is not None else None,
        "horizons": [horizon(3), horizon(6)],
        "forward": forward,
        "note": (
            f"Next 3 and 6 months hold the trailing-year complete-month average "
            f"({avg:,.0f} ECU/mo over {complete_n} complete months, {lookback_days}-day lookback). "
            f"Partial months are excluded from the average. {dollar}"
        ),
    }


def burndown_series(ecu: dict, bd: dict) -> dict | None:
    """Implied remaining balance over the lookback, then a forward burndown.

    History walks back from the current balance by each month's billed ECU
    (top-ups during the window are not visible). The forecast continues from
    today at the last-30-day rate and, separately, at the commit pace, out to
    contract end.
    """
    mo = ecu.get("monthly") or {}
    months = [str(m) for m in (mo.get("months") or [])]
    totals = [float(v or 0) for v in (mo.get("totals") or [])]
    balance = bd.get("ecu_balance")
    if balance is None or not months or len(totals) != len(months):
        return None
    balance = float(balance)
    end = balance
    hist: list[float] = []
    for v in reversed(totals):
        hist.append(end)
        end += v
    hist.reverse()
    daily = float(bd.get("daily_30d") or 0)
    commit = float(bd.get("commit_pace") or bd.get("commit") or 0)
    step, commit_step = daily * DAYS_PER_MONTH, commit * DAYS_PER_MONTH
    contract = str(bd.get("nearest_contract_end") or "")[:7]
    fut: list[str] = []
    at_rate: list[float] = []
    at_commit: list[float] = []
    b = c = balance
    ym = months[-1]
    for _ in range(18):
        ym = _next_month(ym)
        b = max(b - step, 0.0)
        c = max(c - commit_step, 0.0)
        fut.append(ym)
        at_rate.append(round(b))
        at_commit.append(round(c))
        if (contract and ym >= contract) or (not contract and b <= 0 and c <= 0):
            break
    return {
        "months": months + fut,
        "forecast_from": months[-1],
        "implied_balance": [round(v) for v in hist] + at_rate,
        "commit_pace_balance": [None] * (len(months) - 1) + [round(balance)] + at_commit,
        "note": "History = current ECU balance plus ECU billed after each month (mid-window top-ups are not visible). Forecast from the last month: last-30-day rate, and the commit pace, through contract end.",
    }


# --------------------------------------------------------------------------- sizing inputs
def sizing_inputs(acc: dict, org: dict, retention_months: float, security_retention_months: float) -> dict:
    sol = acc["solutions"]["totals"]
    org_gb = float(acc["storage"].get("data_stored_gb") or 0)
    org_ecu = float(acc["ecu"].get("total_lookback") or 0)
    n_months = max(int(acc["ecu"].get("months_in_lookback") or 1), 1)

    def block(key: str, months: float) -> dict:
        s = sol[key]
        gb = float(s["gb"])
        est = False
        if gb <= 0 and s["ecu"] > 0 and org_gb > 0 and org_ecu > 0:
            gb = org_gb * s["ecu"] / org_ecu
            est = True
        daily = gb / (months * DAYS_PER_MONTH) if gb else 0.0
        return {
            "deployments": s["deployments"],
            "ecu_lookback": s["ecu"],
            "ech_ecu": s.get("ech_ecu"),
            "serverless_ecu": s.get("serverless_ecu"),
            "ecu_monthly_avg": round(s["ecu"] / n_months, 2),
            "stored_gb": round(gb, 2),
            "gb_estimated_from_ecu_share": est,
            "retention_months": months,
            "daily_raw_gb": round(daily, 1),
            "has_workload": s["ecu"] > 0 or gb > 0,
        }

    o = block("observability", retention_months)
    o["logs_gb_day"] = round(o["daily_raw_gb"] * 0.90)
    o["metrics_gb_day"] = round(o["daily_raw_gb"] * 0.10)
    s = block("security", security_retention_months)
    s["siem_gb_day"] = round(s["daily_raw_gb"])
    se = block("search", retention_months)
    se["workload"] = acc["workload"]
    cs = acc["cloud"]["summary"]
    return {
        "triggered": acc["platform"]["sizing_triggered"],
        "threshold_pct": SIZING_THRESHOLD_PCT,
        "ech_share_pct": acc["platform"]["ech_share_pct"],
        "provider": cs.get("provider"),
        "region": cs.get("region"),
        "support_tier": "Premium",
        "feature_tier": "Complete",
        "consolidated": {"observability": o, "security": s, "search": se},
        "notes": [
            f"K360 backup only: K360 does not provide a raw ingest total, so daily_raw_gb = stored_gb / (retention_months × {DAYS_PER_MONTH}). Confirm retention with the account team. This can sit far below measured bulk.",
            "Observability split 90% logs / 10% metrics; Security = SIEM ingest; Search is priced by es3-estimator during sizing (on-demand is the quotable profile)",
            "gb_estimated_from_ecu_share = no telemetry for that slice; GB prorated by ECU share",
            "stored_gb comes from ECH telemetry — existing serverless projects contribute ECU but no GB",
        ],
        "estimators": {},
    }


# --------------------------------------------------------------------------- main build
def active_deployment_set(org: dict) -> dict:
    """Deployments with billing ECU in the latest lookback month.

    A deployment with no bill that month is out of the sizing. Its last
    telemetry snapshot is not a source of stored GB. ECU for the sized set is
    the last complete month, so a partial final month does not shrink the bill.
    """
    monthly = org.get("monthly") or {}
    months = list(monthly.get("months") or [])
    by_dep = monthly.get("by_deployment") or {}
    if not months:
        return {"month": None, "partial": False, "ecu_month": None, "count": 0, "names": [], "ecu_complete_month": {}}
    latest = months[-1]
    rng = org.get("range") or {}
    start = str(rng.get("start") or "")
    cur_ym = datetime.now(timezone.utc).date().strftime("%Y-%m")
    partial_months = {m for m in months if m == cur_ym or (start and m == start[:7] and not start.endswith("-01"))}
    complete = [m for m in months if m not in partial_months]
    ecu_month = complete[-1] if complete else latest

    def ecu_in(series: dict, month: str) -> float:
        labels = list(series.get("months") or months)
        values = series.get("ecu") or []
        if month not in labels:
            return 0.0
        i = labels.index(month)
        return float(values[i] or 0) if i < len(values) else 0.0

    names = []
    ecu_complete: dict[str, float] = {}
    for name, series in by_dep.items():
        if not isinstance(series, dict) or ecu_in(series, latest) <= 0:
            continue
        names.append(name)
        ecu_complete[name] = round(ecu_in(series, ecu_month), 2)
    names.sort()
    return {
        "month": latest,
        "partial": latest in partial_months,
        "ecu_month": ecu_month,
        "count": len(names),
        "names": names,
        "ecu_complete_month": ecu_complete,
    }


def build_account(org: dict, infra: dict | None, *, when: date | None = None, retention_months: float = 3.0, security_retention_months: float | None = None) -> dict:
    when = when or datetime.now(timezone.utc).date()
    customer = (org.get("organization") or {}).get("name") or "Customer"
    key = run_key(customer, when)
    acc: dict = {
        "meta": {
            "customer": customer,
            "customer_slug": slug(customer),
            "run_key": key,
            "date": when.isoformat(),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "lookback": org.get("range"),
            "org": {k: v for k, v in (org.get("organization") or {}).items() if k in ("id", "AE", "ARR", "segment", "geo", "region", "contract", "customer_type")},
            "license": org.get("license"),
        }
    }
    acc["platform"] = platform_section(org, infra)
    acc["cloud"] = cloud_section(org, infra)
    acc["ecu"] = ecu_section(org, infra, acc["platform"])
    acc["deployments"] = deployments_section(org, acc["ecu"], infra)
    acc["deployments"]["active"] = active_deployment_set(org)
    acc["storage"] = storage_section(org, acc["cloud"])
    acc["solutions"] = solutions_section(org, infra)
    acc["ai_ml"] = ai_ml_section(org)
    acc["workload"] = org.get("workload") or {}
    acc["analysis"] = {
        "performance": performance_recs(acc),
        "cost": cost_recs(acc, org),
        "burndown": burndown_analysis(acc, org),
    }
    acc["analysis"]["projections"] = year_projections(acc, when)
    acc["serverless_ballpark"] = {
        k: v for k, v in (((org.get("offload") or {}).get("full_split")) or {}).items() if k != "lines"
    } | {"lines": [{k: v for k, v in l.items() if k != "detail"} for l in (((org.get("offload") or {}).get("full_split")) or {}).get("lines") or []]}
    acc["sizing"] = sizing_inputs(acc, org, retention_months, security_retention_months or retention_months)
    return sanitize(acc)


def _projection_bit(acc: dict) -> str:
    proj = (acc.get("analysis") or {}).get("projections") or {}
    horizons = {h.get("months"): h for h in (proj.get("horizons") or [])}
    h3, h6 = horizons.get(3), horizons.get(6)
    if not h3 or not h6:
        return ""
    pace = float(proj.get("ecu_per_month") or 0)

    def piece(h: dict, label: str) -> str:
        text = f"{label} {float(h.get('ecu') or 0):,.0f} ECU"
        if h.get("usd") is not None:
            text += f" (${float(h['usd']):,.0f})"
        return text

    return f" · year pace {pace:,.0f} ECU/mo → {piece(h3, '3 mo')}, {piece(h6, '6 mo')}"


def chat_bullets(acc: dict) -> list[str]:
    p, e, d, s, b = acc["platform"], acc["ecu"], acc["deployments"], acc["storage"], acc["analysis"]["burndown"]
    sol = acc["solutions"]["totals"]
    cs = acc["cloud"]["summary"]
    meter = (acc.get("ai_ml") or {}).get("token_meter") or {}
    token_bit = ""
    if meter.get("rows"):
        qty = float(meter.get("million_tokens") or 0)
        qty_s = f"{qty:,.2f}" if abs(qty) >= 0.1 else f"{qty:,.4f}"
        token_bit = f"; EIS {qty_s} million tokens ({float(meter.get('ecu') or 0):,.2f} ECU)"
    out = [
        f"{acc['meta']['customer']}: {p['label']} — {p['ech_count']} ECH / {p['serverless_count']} serverless deployments; "
        f"{cs.get('provider')} {cs.get('region')} ({cs.get('hot_instance')}, {cs.get('zones')} AZ); ECH share {p['ech_share_pct']}%",
        f"ECU: {e['total_lookback']:,.0f} over {e.get('lookback_days')}d · peak month {(e.get('monthly_peak') or {}).get('month')} = {((e.get('monthly_peak') or {}).get('ecu') or 0):,.0f} · daily avg {e.get('daily_avg') or 0:,.0f}",
        f"Deployments: {d['count']} ({d['trend'].get('flag')}); versions {', '.join(f'{v['version']}×{v['count']}' for v in d['by_version'][:4])}",
        f"Storage {s['data_stored_tb']} TB (as of {s['as_of']}); ECU by solution: O11y {sol['observability']['ecu_pct']}% / Search {sol['search']['ecu_pct']}% / Security {sol['security']['ecu_pct']}%{token_bit}",
        f"Burndown {b['level']}: " + ("; ".join(f['flag'] for f in b['flags'][:3]) or "no flags") + _projection_bit(acc),
    ]
    sz = acc.get("sizing") or {}
    if sz.get("triggered"):
        est = sz.get("estimators") or {}
        parts = []
        for k in ("observability", "security", "search"):
            x = est.get(k) or {}
            if x.get("url") or x.get("readback"):
                rb = x.get("readback") or {}
                mo = rb.get("monthly_steady_state")
                if isinstance(mo, (int, float)):
                    shown = f"${mo:,.0f}/mo"
                elif isinstance(mo, str) and mo not in ("", "None"):
                    shown = mo if "/mo" in mo else f"{mo}/mo"
                else:
                    shown = rb.get("annual_year1") or "see estimate"
                    if isinstance(shown, (int, float)):
                        shown = f"Y1 ${shown:,.0f}"
                parts.append(f"{k.title()} → {shown}")
        c = acc.get("consolidation") or {}
        cs_ = c.get("savings") or {}
        if cs_.get("cost_mo") is not None:
            parts.append(f"consolidation: ECH at the contract rate is ${cs_['ech_contract_mo']:,.0f}/mo, and serverless is ${cs_['cost_mo']:,.0f}/mo below that")
        elif cs_.get("list_mo") is not None:
            parts.append(f"consolidation is ${cs_['list_mo']:,.0f}/mo ({cs_.get('list_pct')}%) below list")
        k360_sav = ((acc.get("k360_ingest") or {}).get("savings") or {})
        if k360_sav.get("serverless_mo") is not None:
            parts.append(f"K360 backup (stored GB ÷ assumed retention) ${float(k360_sav['serverless_mo']):,.0f}/mo")
        out.append("Sizing (ECH >50%): " + ("; ".join(parts) if parts else "estimator copies pending") + (f" · folder {sz.get('drive_folder', {}).get('url')}" if sz.get("drive_folder") else ""))
    return out[:6]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org-json", required=True)
    ap.add_argument("--infra-json", default=None)
    ap.add_argument("--out", default="-")
    ap.add_argument("--date", default=None)
    ap.add_argument("--retention-months", type=float, default=3.0)
    ap.add_argument("--security-retention-months", type=float, default=None)
    args = ap.parse_args()
    org = json.loads(Path(args.org_json).read_text())
    infra = json.loads(Path(args.infra_json).read_text()) if args.infra_json else None
    when = date.fromisoformat(args.date) if args.date else None
    acc = build_account(org, infra, when=when, retention_months=args.retention_months, security_retention_months=args.security_retention_months)
    text = json.dumps(acc, indent=2)
    if args.out == "-":
        print(text)
    else:
        Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(text)} bytes)")


if __name__ == "__main__":
    main()
