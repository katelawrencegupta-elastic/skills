#!/usr/bin/env python3
"""Emit slim canvas + chat summary JSON from a full org_report.json.

Keeps the agent from loading ~250KB payloads into context.

  python emit_canvas_data.py --org-json /tmp/org_report.json --out-dir serverless_analyzer/diag_artifacts/Acme_2026-11-02
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

from report_mode import (
    CANVAS_LAYOUT,
    add_report_arg,
    canvas_suggestion,
    normalize_report,
    pdf_name,
)
from skill_paths import artifacts_root


def _slug(name: str) -> str:
    import re

    s = re.sub(r"[^\w\s\-]+", "", (name or "Customer").strip(), flags=re.UNICODE)
    return re.sub(r"[\s\-]+", "_", s).strip("_") or "Customer"


def _top(items: list, n: int, key: str = "ecu") -> list:
    return sorted(items or [], key=lambda x: -(x.get(key) or x.get("ecu_monthly_avg") or 0))[:n]


def _trim_dep(d: dict) -> dict:
    keep = (
        "name",
        "version",
        "template",
        "provider",
        "region",
        "platform",
        "use_case",
        "ecu",
        "ecu_monthly_avg",
        "ecu_monthly_avg_active",
        "months_active",
        "families",
        "ml_ecu",
    )
    return {k: d[k] for k in keep if k in d}


def _trim_storage_row(r: dict) -> dict:
    keep = ("name", "data_stored_gb", "docs_count", "avg_doc_kb", "ecu_monthly_avg")
    return {k: r[k] for k in keep if k in r}


def _trim_ml(ml: dict) -> dict:
    if not ml:
        return {}
    out = {
        k: ml[k]
        for k in (
            "total_ecu",
            "pct_of_org",
            "deployments_with_ml",
            "elser_deps",
            "instances",
            "regions",
            "skus",
            "notes",
        )
        if k in ml
    }
    by = ml.get("by_deployment") or []
    if isinstance(by, list):
        out["by_deployment"] = by[:10]
    vec = ml.get("vectors") or {}
    if isinstance(vec, dict):
        slim_v = {k: vec[k] for k in ("count", "dims", "notes", "total") if k in vec}
        items = vec.get("by_deployment") or vec.get("items") or []
        if isinstance(items, list):
            slim_v["items"] = items[:10]
        out["vectors"] = slim_v
    return out


def _trim_ai(ai: dict) -> dict:
    if not ai:
        return {}
    out = {
        k: ai[k]
        for k in (
            "elser",
            "genai_connector",
            "playground",
            "vector_mappings",
            "ai_assistant",
            "attack_discovery",
            "billing_ecu_total",
            "notes",
        )
        if k in ai
    }
    if "billing_ecu_by_type" in ai:
        out["billing_ecu_by_type"] = ai["billing_ecu_by_type"]
    if "inference_skus" in ai:
        out["inference_skus"] = (ai["inference_skus"] or [])[:10]
    if "models" in ai:
        out["models"] = (ai["models"] or [])[:10]
    by = ai.get("by_deployment") or []
    if isinstance(by, list):
        out["by_deployment"] = by[:10]
    return out


def _trim_monthly(monthly: dict, top_n: int = 8) -> dict:
    if not monthly:
        return {}
    out = {
        k: monthly[k]
        for k in ("months", "series_names", "ecu_series")
        if k in monthly
    }
    # Keep only top series by last non-zero / sum to shrink embed
    names = list(out.get("series_names") or [])
    series = list(out.get("ecu_series") or [])
    if names and series and len(names) == len(series):
        scored = []
        for i, name in enumerate(names):
            scored.append((sum(series[i] or []), i, name))
        scored.sort(reverse=True)
        keep_idx = sorted(i for _, i, _ in scored[:top_n])
        # Always keep a residual "Other" if we drop series
        if len(scored) > top_n:
            other = [0.0] * len(series[0] if series else [])
            drop = {i for _, i, _ in scored[top_n:]}
            for i in drop:
                for t, v in enumerate(series[i] or []):
                    other[t] += v or 0
            out["series_names"] = [names[i] for i in keep_idx] + ["Other"]
            out["ecu_series"] = [[round(v, 2) for v in (series[i] or [])] for i in keep_idx] + [
                [round(v, 2) for v in other]
            ]
        else:
            out["series_names"] = names
            out["ecu_series"] = series
    by = monthly.get("by_deployment") or []
    if isinstance(by, list):
        out["by_deployment"] = by[:top_n]
    return out


def _trim_chart(chart: dict, max_points: int = 52) -> dict:
    if not chart:
        return {}
    days = chart.get("days") or []
    total = chart.get("total") or []
    names = chart.get("series_names") or []
    series = chart.get("ecu_series") or []
    # If daily and long, downsample by keeping weekly-ish stride
    if len(days) > max_points:
        step = max(1, len(days) // max_points)
        idx = list(range(0, len(days), step))
        if idx[-1] != len(days) - 1:
            idx.append(len(days) - 1)
        days = [days[i] for i in idx]
        total = [total[i] for i in idx] if total else total
        series = [[row[i] for i in idx] for row in series] if series else series
    # Top 5 named series + Other
    if names and series and len(names) == len(series) and len(names) > 5:
        scored = sorted(
            ((sum(series[i] or []), i, names[i]) for i in range(len(names))),
            reverse=True,
        )
        keep = sorted(i for _, i, _ in scored[:5])
        other = [0.0] * len(series[0])
        for _, i, _ in scored[5:]:
            for t, v in enumerate(series[i] or []):
                other[t] += v or 0
        names = [names[i] for i in keep] + ["Other"]
        series = [series[i] for i in keep] + [[round(v, 2) for v in other]]
    return {
        "grain": chart.get("grain"),
        "days": days,
        "series_names": names,
        "ecu_series": series,
        "total": total,
    }


def build_canvas(data: dict, top_deps: int = 12) -> dict:
    org = data.get("organization") or {}
    deps = data.get("deployments") or {}
    items = [_trim_dep(x) for x in _top(deps.get("items") or [], top_deps, "ecu_monthly_avg")]
    storage = data.get("storage") or {}
    storage_out = {
        k: storage[k]
        for k in (
            "as_of",
            "deployments_with_telemetry",
            "data_stored_gb",
            "docs_count",
            "avg_doc_kb",
            "calculated_data_stored_gb",
            "notes",
        )
        if k in storage
    }
    by_st = storage.get("by_deployment") or []
    if isinstance(by_st, list):
        storage_out["by_deployment"] = [
            _trim_storage_row(r) for r in _top(by_st, 15, "data_stored_gb")
        ]

    infra = data.get("infrastructure") or {}
    infra_out = {
        k: infra[k]
        for k in (
            "providers",
            "regions",
            "hot_instance_types",
            "templates",
            "summary",
            "capacity_by_provider",
            "capacity_by_tier",
        )
        if k in infra
    }

    return {
        "org": org.get("name") or data.get("org"),
        "org_id": org.get("id"),
        "geo": org.get("geo") or org.get("region"),
        "segment": org.get("segment"),
        "ae": (org.get("ae") or org.get("account_executive")),
        "hv": org.get("hv") or org.get("high_value_contacts") or [],
        "arr": org.get("arr"),
        "customer_type": org.get("customer_type"),
        "license": (data.get("license") or org.get("license")),
        "range": data.get("range"),
        "generated_at": data.get("generated_at"),
        "total_ecu": (data.get("totals") or {}).get("ecu") or data.get("total_ecu"),
        "ecu_monthly_avg": data.get("ecu_monthly_avg")
        or (data.get("totals") or {}).get("ecu_monthly_avg"),
        "months_n": data.get("months_n") or (data.get("totals") or {}).get("months_n"),
        "burndown": data.get("burndown") or {},
        "deployments": {
            "count": deps.get("count"),
            "ech": deps.get("ech_count", deps.get("ech")),
            "serverless": deps.get("serverless_count", deps.get("serverless")),
            "by_version": (deps.get("by_version") or [])[:12],
            "over_time": deps.get("over_time"),
        },
        "families": data.get("families"),
        "ai": _trim_ai(data.get("ai") or {}),
        "ml": _trim_ml(data.get("ml") or {}),
        "items": items,
        "chart": _trim_chart(data.get("chart") or {}),
        "monthly": _trim_monthly(data.get("monthly") or {}),
        "infrastructure": infra_out,
        "storage": storage_out,
        "workload": data.get("workload") or {},
    }


def build_offload(data: dict) -> dict:
    off = data.get("offload") or {}
    keep = ("by_kind", "full_split", "hybrid", "all_es3", "notes", "analysis")
    out = {k: off[k] for k in keep if k in off}
    ea = off.get("estimator_artifacts")
    if ea:
        # Drop bulky nested sheet cell dumps if any; keep paths/urls
        out["estimator_artifacts"] = {
            k: ea[k]
            for k in (
                "run_key",
                "local_folder",
                "drive_folder",
                "pdf",
                "pdf_expected",
                "artifacts",
                "date",
                "customer",
            )
            if k in ea
        }
    return out


def build_summary(data: dict, canvas: dict, offload: dict, paths: dict, report: str) -> dict:
    bd = canvas.get("burndown") or {}
    fs = (offload.get("full_split") or {})
    return {
        "org": canvas.get("org"),
        "report": report,
        "canvas_layout": CANVAS_LAYOUT[report],
        "canvas_file_hint": paths.get("canvas_file_hint"),
        "generated_at": canvas.get("generated_at"),
        "burndown_level": bd.get("level"),
        "runout_date": bd.get("runout_date"),
        "runway_days": bd.get("runway_days"),
        "contract_end": bd.get("nearest_contract_end"),
        "annual_projection": bd.get("annual_projection"),
        "arr": canvas.get("arr"),
        "deps": (canvas.get("deployments") or {}).get("count"),
        "full_split_save_mo": fs.get("save_vs_ech_mo"),
        "full_split_save_yr": fs.get("save_vs_ech_yr"),
        "serverless_total_mo": fs.get("serverless_total_mo"),
        "full_ech_mo": fs.get("full_ech_mo"),
        "all_es3_mo": (fs.get("vs_all_es3") or offload.get("all_es3") or {}).get("all_es3_mo")
        or (offload.get("all_es3") or {}).get("mo"),
        "paths": paths,
        "chat_bullets_hint": [
            f"Report: {report} · {CANVAS_LAYOUT[report]}",
            f"Overage/runway: {bd.get('level')} · runout {bd.get('runout_date')} ({bd.get('runway_days')}d)",
            f"Projection ~${bd.get('annual_projection')} vs ARR ~${canvas.get('arr')}",
            f"Serverless full_split save ~${fs.get('save_vs_ech_mo')}/mo (~${fs.get('save_vs_ech_yr')}/yr)",
            f"Artifacts: {paths.get('folder')}",
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org-json", required=True)
    ap.add_argument("--out-dir", default=None, help="Artifacts folder (default: <artifacts-root>/<slug>_<date>)")
    ap.add_argument("--artifacts-root", default=None, help="Parent of <customer>_<date>/ (default: serverless_analyzer/diag_artifacts)")
    ap.add_argument("--top-deps", type=int, default=12)
    add_report_arg(ap)
    args = ap.parse_args()
    report = normalize_report(args.report)

    org_path = Path(args.org_json)
    data = json.loads(org_path.read_text())
    org = data.get("organization") or {}
    name = org.get("name") or data.get("org") or "Customer"
    when = date.today()
    # Prefer generated_at date if present
    ga = (data.get("generated_at") or "")[:10]
    if ga:
        try:
            when = date.fromisoformat(ga)
        except ValueError:
            pass
    slug = _slug(name)
    key = f"{slug}_{when.isoformat()}"

    if args.out_dir:
        out_dir = Path(args.out_dir)
    elif args.artifacts_root:
        out_dir = Path(args.artifacts_root) / key
    else:
        out_dir = org_path.parent
        if out_dir == Path("/tmp") or out_dir.resolve() == Path("/tmp"):
            out_dir = artifacts_root() / key
    out_dir.mkdir(parents=True, exist_ok=True)

    canvas = build_canvas(data, top_deps=args.top_deps)
    offload = build_offload(data)
    canvas_path = out_dir / f"{key}_canvas.json"
    offload_path = out_dir / f"{key}_offload.json"
    summary_path = out_dir / f"{key}_summary.json"

    canvas_path.write_text(json.dumps(canvas, separators=(",", ":")))
    offload_path.write_text(json.dumps(offload, separators=(",", ":")))

    paths = {
        "folder": str(out_dir),
        "org_json": str(org_path),
        "canvas": str(canvas_path),
        "offload": str(offload_path),
        "summary": str(summary_path),
        "pdf_expected": str(out_dir / pdf_name(key, report)),
        "run_key": key,
        "report": report,
        "canvas_file_hint": canvas_suggestion(slug, report),
    }
    summary = build_summary(data, canvas, offload, paths, report)
    summary_path.write_text(json.dumps(summary, indent=2))

    print(
        json.dumps(
            {
                "run_key": key,
                "report": report,
                "canvas_file_hint": paths["canvas_file_hint"],
                "canvas_bytes": canvas_path.stat().st_size,
                "offload_bytes": offload_path.stat().st_size,
                "summary": str(summary_path),
                "canvas": str(canvas_path),
                "offload": str(offload_path),
                "folder": str(out_dir),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
