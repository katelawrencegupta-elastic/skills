#!/usr/bin/env python3
"""One-shot K360 account analysis: fetch → infra → analyze → report.md → (ECH>50%) sizing.

  python run_account_analysis.py --org "Acme Corp"
  python run_account_analysis.py --org "Acme Corp" --reuse /tmp/org_report.json --skip-sizing

Run from the project root that holds `.env` (KIBANA_URL, ELASTIC_API_KEY).
Depends on the k360-org-report skill for fetch_org.py + sheets_auth.py
(override location with K360_ORG_SKILL_DIR).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from analyze import build_account, chat_bullets, component_summary  # noqa: E402
from consolidation import build_consolidation  # noqa: E402
from quote_checks import e73_blocks_quote, money as _sheet_money  # noqa: E402
from render_pdf import K360_BACKUP_SUFFIX, export as export_pdfs  # noqa: E402
from k360_infra import es_base_and_key, fetch_infra, fill_ingest_doc_bytes, fill_storage_from_data_pivot, fill_storage_monthly, load_env, resolve_org_name, size_gaps  # noqa: E402
from report_md import render  # noqa: E402

ORG_SKILL = Path(os.environ.get("K360_ORG_SKILL_DIR") or Path.home() / ".cursor/skills/k360-org-report")


def _run(cmd: list[str]) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.check_call(cmd)


def _sheets_py() -> Path:
    return Path(os.environ.get("CLOUD_TRACKER_SHEETS_PY") or Path.home() / ".config/cloud-tracker-sheets/venv/bin/python")


def _rnd(v, nd: int = 0):
    try:
        if v is None or isinstance(v, bool):
            return v
        return round(float(v), nd)
    except (TypeError, ValueError):
        return v


def _est(v):
    if not isinstance(v, dict):
        return None
    keep = ("title", "url", "status", "readback", "region_match")
    return {k: v[k] for k in keep if k in v}


def _token_meter_brief(meter: dict | None) -> dict | None:
    """Compact token meter for the brief. Full rows stay in account.json."""
    if not isinstance(meter, dict) or not (meter.get("rows") or meter.get("million_tokens")):
        return None
    rows = []
    for r in (meter.get("rows") or [])[:6]:
        rows.append({
            "model": r.get("model_label"),
            "kind": r.get("kind_label"),
            "million_tokens": r.get("million_tokens"),
            "ecu": r.get("ecu"),
            "ecu_per_million": r.get("ecu_per_million"),
        })
    out = {
        "unit": "million tokens",
        "million_tokens": meter.get("million_tokens"),
        "ecu": meter.get("ecu"),
        "rows": rows,
    }
    extra = len(meter.get("rows") or []) - len(rows)
    if extra > 0:
        out["rows_omitted"] = extra
    split = {k: v for k, v in (meter.get("by_solution") or {}).items() if k != "unattributed" or (v or {}).get("ecu")}
    if len(split) > 1:
        out["by_solution"] = split
    return out


def agent_brief(acc: dict) -> dict:
    """One small JSON the agent reads. account.json stays on disk."""
    m = acc.get("meta") or {}
    org = m.get("org") or {}
    p = acc.get("platform") or {}
    cloud = acc.get("cloud") or {}
    ecu = acc.get("ecu") or {}
    monthly = ecu.get("monthly") or {}
    deps = acc.get("deployments") or {}
    st = acc.get("storage") or {}
    sol = (acc.get("solutions") or {}).get("totals") or {}
    sol_by = {d.get("name"): d.get("solution") for d in ((acc.get("solutions") or {}).get("by_deployment") or [])}
    an = acc.get("analysis") or {}
    burn = an.get("burndown") or {}
    series = burn.get("series") or {}
    months = series.get("months") or []
    start = months.index(series["forecast_from"]) if series.get("forecast_from") in months else 0
    window = slice(start, start + 18)
    sz = acc.get("sizing") or {}
    cons = acc.get("consolidation") or {}
    est_in = sz.get("estimators") if isinstance(sz.get("estimators"), dict) else {}
    estimators = {k: view for k, v in est_in.items() if (view := _est(v))}
    sec_rb = (estimators.get("security") or {}).get("readback") or {}
    cps_val = _sheet_money(sec_rb.get("cps_addon_monthly"))
    notes = list((cons.get("notes") or [])[:3])
    if e73_blocks_quote(cps_val):
        notes.append(f"Security was left out of the quote. Sheet E73, the template cross-project search add-on, is ${cps_val:,.0f}.")
    exports = acc.get("exports") or {}
    slug = (m.get("customer_slug") or "account").replace("_", "-").lower()
    return {
        "customer": m.get("customer"),
        "run_key": m.get("run_key"),
        "date": m.get("date"),
        "lookback": m.get("lookback"),
        "org": {
            "id": org.get("id"), "ae": org.get("AE"), "arr": _rnd(org.get("ARR"), 2),
            "segment": org.get("segment"), "geo": org.get("geo"), "customer_type": org.get("customer_type"),
            "contract": org.get("contract"),
        },
        "platform": {
            "label": p.get("label"), "ech_count": p.get("ech_count"), "serverless_count": p.get("serverless_count"),
            "ech_ecu": _rnd(p.get("ech_ecu")), "serverless_ecu": _rnd(p.get("serverless_ecu"), 2),
            "ech_share_pct": p.get("ech_share_pct"), "summary": cloud.get("summary"),
            "zones": cloud.get("zones"), "regions": cloud.get("regions"), "tiers": (cloud.get("tiers") or [])[:6],
            "one_az": [d.get("name") for d in (cloud.get("by_deployment") or []) if d.get("zones") == 1][:8],
            "top_deployments": [
                {
                    "name": d.get("name"), "version": d.get("version"), "region": d.get("region"),
                    "provider": d.get("provider"), "zones": d.get("zones"),
                    "ecu": _rnd(d.get("ecu")),
                    "solution": sol_by.get(d.get("name")) or d.get("use_case"),
                    "nodes": component_summary(d) or None,
                }
                for d in (cloud.get("by_deployment") or [])[:8]
            ],
        },
        "ecu": {
            "total": _rnd(ecu.get("total_lookback")), "monthly_avg": _rnd(ecu.get("monthly_avg_complete_months")),
            "peak": ecu.get("monthly_peak"), "low": ecu.get("monthly_low"),
            "daily_avg": _rnd(ecu.get("daily_avg")), "last_30d_daily_avg": _rnd(ecu.get("last_30d_daily_avg")),
            "trend": ecu.get("trend"), "months": monthly.get("months"),
            "totals": [_rnd(x) for x in (monthly.get("totals") or [])],
            "partial_months": monthly.get("partial_months"), "contract": ecu.get("contract"),
        },
        "versions": (deps.get("by_version") or [])[:6],
        "version_summary": deps.get("version_summary"),
        "deployment_trend": deps.get("trend"),
        "deployment_counts": ((deps.get("over_time") or {}).get("month")),
        "storage": {
            "as_of": st.get("as_of"), "telemetry_age_days": st.get("telemetry_age_days"),
            "tb": st.get("data_stored_tb"), "gb": _rnd(st.get("data_stored_gb"), 1),
            "docs_count": st.get("docs_count"), "avg_doc_kb": st.get("avg_doc_kb"),
            "notes": (st.get("notes") or [])[:2],
            "top": [{"name": r.get("name"), "gb": _rnd(r.get("data_stored_gb"), 1), "as_of": r.get("as_of")} for r in (st.get("by_deployment") or [])[:5]],
            "monthly": {
                "months": (st.get("monthly") or {}).get("months"),
                "gb": (st.get("monthly") or {}).get("gb"),
                "source": (st.get("monthly") or {}).get("source"),
            } if (st.get("monthly") or {}).get("months") else None,
        },
        "solutions": {k: {"ecu_pct": v.get("ecu_pct"), "deployments": v.get("deployments"), "gb": _rnd(v.get("gb"), 1)} for k, v in sol.items() if isinstance(v, dict)},
        "ai_ml": {
            "ml_node_ecu": _rnd((acc.get("ai_ml") or {}).get("ml_node_ecu")),
            "eis_inference_ecu": _rnd((acc.get("ai_ml") or {}).get("eis_inference_ecu"), 2),
            "feature_counts": (acc.get("ai_ml") or {}).get("feature_counts"),
            "token_meter": _token_meter_brief((acc.get("ai_ml") or {}).get("token_meter")),
        },
        "burndown": {
            "level": burn.get("level"), "flags": burn.get("flags"), "runway_days": burn.get("runway_days"),
            "runout_date": burn.get("runout_date"), "contract_end": burn.get("nearest_contract_end"),
            "balance": burn.get("ecu_balance"), "daily_30d": burn.get("daily_30d"),
            "commit_pace": burn.get("commit_pace"), "required_pace": burn.get("required_pace"),
            "consumption_rate": burn.get("consumption_rate"),
            "months": months[window],
            "balance_at_rate": (series.get("implied_balance") or [])[window],
            "balance_at_commit": (series.get("commit_pace_balance") or [])[window],
        },
        "projections": an.get("projections") or {},
        "recommendations": [
            {"kind": kind, "severity": r.get("severity"), "title": r.get("title"), "evidence": r.get("evidence")}
            for kind in ("performance", "cost") for r in (an.get(kind) or [])[:5]
        ],
        "contract_rate": cons.get("contract_rate"),
        "savings": {k: (cons.get("savings") or {}).get(k) for k in ("ech_contract_mo", "serverless_mo", "cost_mo", "cost_yr", "cost_pct") if (cons.get("savings") or {}).get(k) is not None},
        "quotes": ((cons.get("reconciliation") or {}).get("quotes")) or [],
        "fleet": ((cons.get("sensitivity") or {}).get("fleet")) or [],
        "notes": notes,
        "estimators": estimators,
        "links": {
            "folder": sz.get("folder"),
            "drive": (sz.get("drive_folder") or {}).get("url"),
            "drive_30d": (sz.get("drive_folder_30d") or {}).get("url"),
            "drive_compressioncalc": (sz.get("drive_folder_compressioncalc") or {}).get("url"),
            "drive_compressioncalc_30d": (sz.get("drive_folder_compressioncalc_30d") or {}).get("url"),
            "estimators": {k: v.get("url") for k, v in estimators.items() if v.get("url")},
            "pdfs": {k: {"pdf": v.get("pdf"), "drive_url": v.get("drive_url")} for k, v in exports.items() if isinstance(v, dict)},
        },
        "chat_bullets": chat_bullets(acc),
        "canvas_file": f"{slug}-account-analysis.canvas.tsx",
        "ingest_source": cons.get("source") or sz.get("ingest_source") or "k360",
        "consolidation_canvas_file": f"{slug}-serverless-consolidation-savings.canvas.tsx" if cons else None,
        "k360_ingest": _k360_brief(acc),
        "k360_canvas_file": f"{slug}-serverless-consolidation-from-k360.canvas.tsx" if acc.get("k360_ingest") else None,
        "diagnostics_coverage": sz.get("diagnostics") if (cons.get("source") == "diagnostics" or sz.get("ingest_source") == "diagnostics") else None,
        "compressioncalc_accounted_for": acc.get("compressioncalc_accounted_for"),
    }


def _quote_block(acc: dict) -> dict:
    cons = acc.get("consolidation") or {}
    return {
        "source": cons.get("source") or "k360",
        "savings": {k: (cons.get("savings") or {}).get(k) for k in ("ech_contract_mo", "ech_list_mo", "serverless_mo", "cost_mo", "cost_yr", "cost_pct", "list_mo", "list_pct") if (cons.get("savings") or {}).get(k) is not None},
        "quotes": ((cons.get("reconciliation") or {}).get("quotes")) or [],
        "notes": (cons.get("notes") or [])[:4],
        "contract_rate": cons.get("contract_rate"),
        "coverage": (acc.get("sizing") or {}).get("diagnostics") or None,
    }


def _k360_brief(acc: dict) -> dict | None:
    block = acc.get("k360_ingest") or {}
    if not block:
        return None
    keep = ("source", "savings", "quotes", "notes", "contract_rate")
    return {k: block[k] for k in keep if k in block}


def _money_note(diag_mo, k360_mo) -> str:
    return (
        f"Discrepancy: diagnostic bulk prices serverless at ${float(diag_mo):,.0f}/mo. "
        f"The K360 backup, stored GB ÷ assumed retention, prices it at ${float(k360_mo):,.0f}/mo. "
        "K360 does not provide a raw ingest total, so those two figures are not interchangeable."
    )


def _compression_totals(acc: dict) -> tuple[float, float | None, float]:
    """Observability plus Security: raw bulk, on-disk ÷ bulk ratio, accounted GB/day.

    The ratio stays at or below 1. Combining two solutions weights each solution's
    ratio by its bulk.
    """
    from admin_diags import compression_share_from_ratios

    con = (acc.get("sizing") or {}).get("consolidated") or {}
    bulk = 0.0
    accounted = 0.0
    bulks: list[float] = []
    ratios: list[float | None] = []
    for sol in ("observability", "security"):
        blk = con.get(sol) or {}
        solution_bulk = float(blk.get("bulk_gb_day") or 0)
        bulk += solution_bulk
        accounted += float(blk.get("compressioncalc_accounted_for") or 0)
        bulks.append(solution_bulk)
        ratios.append(blk.get("compression_ratio"))
    share = compression_share_from_ratios(bulks, ratios)
    return round(bulk, 3), None if share is None else round(share, 3), round(accounted, 3)


def _stamp_compression(acc: dict, *, scenario: str) -> tuple[float, float | None, float]:
    from diag_account import COMPRESSIONCALC

    bulk, ratio, accounted = _compression_totals(acc)
    cons = acc.setdefault("consolidation", {})
    cons["ingest_basis"] = COMPRESSIONCALC
    cons["bulk_gb_day"] = bulk
    cons["compression_ratio"] = ratio
    cons[COMPRESSIONCALC] = accounted
    cons["retention_scenario"] = scenario
    return bulk, ratio, accounted


def _run_compressioncalc(diag: dict, diag_path: Path, infra: dict, py: Path, args, *, size_sheets: bool) -> None:
    """Price Observability and Security on compressioncalc_accounted_for. The bulk quote stays put."""
    from diag_account import COMPRESSIONCALC, compressioncalc_account

    comp = compressioncalc_account(diag)
    bulk, ratio, accounted = _compression_totals(comp)
    path = diag_path.with_name(diag_path.stem + "_compressioncalc_accounted_for.json")
    path.write_text(json.dumps(comp, indent=2))
    block = {
        "bulk_gb_day": bulk,
        "compression_ratio": ratio,
        "gb_day": accounted,
        "account": str(path),
        "formula": (
            "compressioncalc_accounted_for = bulk GB/day × compression ratio. "
            "The compression ratio is on-disk ÷ bulk and is never above 1. "
            "When the ratio of sums is above 1, the median of the per-index ratios is used."
        ),
    }
    diag[COMPRESSIONCALC] = block
    if accounted == 0 or not size_sheets or not py.is_file():
        if accounted == 0:
            block["status"] = "not priced — compressioncalc_accounted_for is 0"
        elif not py.is_file():
            block["status"] = "measured only — Sheets venv missing"
        else:
            block["status"] = "measured only — the bulk quote did not finish"
        return
    flags: list[str] = []
    if args.skip_xlsx:
        flags.append("--skip-xlsx")
    if args.always_copy:
        flags.append("--always-copy")
    try:
        _run([
            str(py), str(HERE / "size_serverless.py"),
            "--account-json", str(path),
            "--name-suffix", COMPRESSIONCALC,
            "--sizing-json", "sizing_compressioncalc_accounted_for.json",
            "--skip-search",
            *flags,
        ])
        comp = json.loads(path.read_text())
        comp["consolidation"] = build_consolidation(comp, infra)
        _stamp_compression(comp, scenario="rounded")
        path.write_text(json.dumps(comp, indent=2))
        if (comp.get("consolidation") or {}).get("architecture", {}).get("per_region") and not (comp.get("consolidation") or {}).get("error"):
            try:
                _run([
                    str(py), str(HERE / "size_serverless.py"),
                    "--account-json", str(path),
                    "--projects",
                    "--name-suffix", COMPRESSIONCALC,
                    "--sizing-json", "sizing_compressioncalc_accounted_for.json",
                    "--skip-search",
                    *flags,
                ])
                comp = json.loads(path.read_text())
                comp["consolidation"] = build_consolidation(comp, infra)
                _stamp_compression(comp, scenario="rounded")
                path.write_text(json.dumps(comp, indent=2))
            except subprocess.CalledProcessError as e:
                print(f"WARN: compressioncalc_accounted_for regional sizing failed ({e})", file=sys.stderr)
                block["regional_status"] = f"consolidated read-back kept ({e})"
    except subprocess.CalledProcessError as e:
        print(f"WARN: compressioncalc_accounted_for sizing failed ({e})", file=sys.stderr)
        block["status"] = f"not priced ({e})"
        diag.setdefault("consolidation", {}).setdefault("notes", []).append(
            f"compressioncalc_accounted_for was not priced ({e})."
        )
        return
    except Exception as ex:
        print(f"WARN: compressioncalc_accounted_for quote failed ({ex!r})", file=sys.stderr)
        block["status"] = f"not priced ({ex!r})"
        diag.setdefault("consolidation", {}).setdefault("notes", []).append(
            f"compressioncalc_accounted_for was not priced ({ex!r})."
        )
        return
    if (comp.get("consolidation") or {}).get("error"):
        block["status"] = "not priced — consolidation failed"
        return
    path_30 = diag_path.with_name(diag_path.stem + "_compressioncalc_accounted_for_30d.json")
    try:
        comp_30 = copy.deepcopy(comp)
        for sol in ("observability", "security"):
            blk = ((comp_30.get("sizing") or {}).get("consolidated") or {}).get(sol) or {}
            if blk.get("retention_months") is not None:
                blk["retention_months"] = 1
        (comp_30.get("sizing") or {}).pop("deployment_plan", None)
        path_30.write_text(json.dumps(comp_30, indent=2))
        _run([
            str(py), str(HERE / "size_serverless.py"),
            "--account-json", str(path_30),
            "--name-suffix", f"{COMPRESSIONCALC}_30d",
            "--sizing-json", "sizing_compressioncalc_accounted_for_30d.json",
            "--force-months", "1",
            "--skip-search",
            *flags,
        ])
        comp_30 = json.loads(path_30.read_text())
        comp_30["consolidation"] = build_consolidation(comp_30, infra)
        _stamp_compression(comp_30, scenario="30d")
        comp_30["consolidation"]["retention_set_days"] = 30
        path_30.write_text(json.dumps(comp_30, indent=2))
        monthly_30 = ((comp_30.get("consolidation") or {}).get("savings") or {}).get("serverless_mo")
        comp["consolidation"]["thirty_day"] = {
            "monthly": monthly_30,
            "annual_year1": None if monthly_30 is None else round(float(monthly_30) * 12, 2),
        }
        path.write_text(json.dumps(comp, indent=2))
        block["thirty_day_monthly"] = monthly_30
        block["thirty_day_account"] = str(path_30)
        diag["compressioncalc_thirty_day_account"] = str(path_30)
        folder_30 = (comp_30.get("sizing") or {}).get("drive_folder") or {}
        if folder_30.get("id"):
            diag.setdefault("sizing", {})["drive_folder_compressioncalc_30d"] = folder_30
    except subprocess.CalledProcessError as e:
        print(f"WARN: compressioncalc_accounted_for 30-day sizing failed ({e})", file=sys.stderr)
        block["thirty_day_status"] = f"not priced ({e})"
    except Exception as ex:
        print(f"WARN: compressioncalc_accounted_for 30-day quote failed ({ex!r})", file=sys.stderr)
        block["thirty_day_status"] = f"not priced ({ex!r})"
    monthly = ((comp.get("consolidation") or {}).get("savings") or {}).get("serverless_mo")
    block["monthly"] = monthly
    block["quotes"] = ((comp.get("consolidation") or {}).get("reconciliation") or {}).get("quotes") or []
    block["account"] = str(path)
    diag["compressioncalc_account"] = str(path)
    folder = (comp.get("sizing") or {}).get("drive_folder") or {}
    if folder.get("id"):
        diag.setdefault("sizing", {})["drive_folder_compressioncalc"] = folder


def _diagnostics_document(acc: dict, infra: dict, folder: Path, env: dict, args) -> dict | None:
    """Default consolidation: measured ingest and retention from admin-console diagnostics."""
    sz = acc.setdefault("sizing", {})
    if not sz.get("triggered") or args.skip_diags:
        if args.skip_diags:
            sz["diagnostics_status"] = "skipped via --skip-diags"
        return None
    from admin_diags import parse_diagnostics, pull_diagnostics
    from diag_account import apply_diagnostics

    org_id = ((acc.get("meta") or {}).get("org") or {}).get("id")
    when = (acc.get("meta") or {}).get("date")
    if args.diags_dir:
        out_dir = Path(args.diags_dir)
        if not out_dir.is_dir():
            print(f"WARN: --diags-dir not found: {out_dir}", file=sys.stderr)
            sz["diagnostics_status"] = f"--diags-dir not found: {out_dir}"
            return None
    else:
        if not org_id:
            print("WARN: no organization id — diagnostic consolidation unavailable", file=sys.stderr)
            sz["diagnostics_status"] = "no organization id"
            return None
        out_dir = folder.parent / f"{org_id}_diags_{when}"
        pull = True
        if not args.skip_vpn_check:
            from vpn_check import aviatrix_status
            try:
                vpn_up, vpn_detail = aviatrix_status()
            except RuntimeError as ex:
                vpn_up, vpn_detail = False, str(ex)
            if not vpn_up:
                print(f"WARN: {vpn_detail}", file=sys.stderr)
                if not any(out_dir.glob("*.zip")):
                    sz["diagnostics_status"] = vpn_detail
                    return None
                print("WARN: tunnel is down; parsing diagnostic zips already on disk", file=sys.stderr)
                pull = False
        if pull:
            try:
                pull_diagnostics(env, str(org_id), out_dir)
            except Exception as ex:
                print(f"WARN: diagnostic pull failed ({ex!r})", file=sys.stderr)
                sz["diagnostics_status"] = f"diagnostic pull failed ({ex!r})"
                if not any(out_dir.glob("*.zip")):
                    return None
    zips = list(out_dir.glob("*.zip"))
    if not zips:
        print("WARN: no diagnostic bundles — the quote falls back to K360 stored GB", file=sys.stderr)
        sz["diagnostics_status"] = "no diagnostic bundles"
        return None
    captured = datetime.fromtimestamp(max(p.stat().st_mtime for p in zips), timezone.utc)
    try:
        rows = parse_diagnostics(out_dir, captured=captured)
    except Exception as ex:
        print(f"WARN: diagnostic parse failed ({ex!r})", file=sys.stderr)
        sz["diagnostics_status"] = f"diagnostic parse failed ({ex!r})"
        return None
    if not rows:
        sz["diagnostics_status"] = "no diagnostic rows"
        return None
    diag = apply_diagnostics(acc, rows)
    diag_path = folder / f"{acc['meta']['run_key']}_diags_account.json"
    diag_path.write_text(json.dumps(diag, indent=2))
    py = _sheets_py()
    sizing_ran = False
    extra = ["--sizing-json", "sizing.json"]
    if py.is_file():
        cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(diag_path), *extra]
        if args.skip_xlsx:
            cmd.append("--skip-xlsx")
        if args.always_copy:
            cmd.append("--always-copy")
        try:
            _run(cmd)
            sizing_ran = True
            diag = json.loads(diag_path.read_text())
        except subprocess.CalledProcessError as e:
            print(f"WARN: diagnostics sizing failed ({e}); the diagnostics document keeps the measured inputs", file=sys.stderr)
    else:
        print(f"WARN: Sheets venv missing at {py} — diagnostics document has no estimator read-backs", file=sys.stderr)
    try:
        diag["consolidation"] = build_consolidation(diag, infra)
    except Exception as ex:
        print(f"WARN: diagnostics consolidation failed ({ex!r})", file=sys.stderr)
        diag["consolidation"] = {"triggered": True, "source": "diagnostics", "error": repr(ex)}
    diag_path.write_text(json.dumps(diag, indent=2))
    if sizing_ran and (diag.get("consolidation") or {}).get("architecture", {}).get("per_region") and not (diag.get("consolidation") or {}).get("error"):
        try:
            cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(diag_path), "--projects", *extra]
            if args.skip_xlsx:
                cmd.append("--skip-xlsx")
            _run(cmd)
            diag = json.loads(diag_path.read_text())
            diag["consolidation"] = build_consolidation(diag, infra)
            diag_path.write_text(json.dumps(diag, indent=2))
        except subprocess.CalledProcessError as e:
            print(f"WARN: diagnostics regional sizing failed ({e}); document uses the consolidated read-backs", file=sys.stderr)
    if sizing_ran and py.is_file() and not (diag.get("consolidation") or {}).get("error"):
        if not args.skip_deployment_plan:
            plan_cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(diag_path), "--deployment-plan", *extra]
            if args.skip_xlsx:
                plan_cmd.append("--skip-xlsx")
            try:
                _run(plan_cmd)
            except subprocess.CalledProcessError as e:
                diag = json.loads(diag_path.read_text())
                diag.setdefault("sizing", {})["deployment_plan"] = {
                    "bars": [],
                    "error": f"Observability deployment-count plan was not priced ({e}).",
                }
                diag_path.write_text(json.dumps(diag, indent=2))
            diag = json.loads(diag_path.read_text())
            try:
                diag["consolidation"] = build_consolidation(diag, infra)
            except Exception as ex:
                print(f"WARN: deployment-plan consolidation failed ({ex!r})", file=sys.stderr)
                diag["consolidation"] = {"triggered": True, "source": "diagnostics", "error": repr(ex)}
        if not (diag.get("consolidation") or {}).get("error"):
            diag.setdefault("consolidation", {})["retention_scenario"] = "rounded"
            try:
                diag_30 = copy.deepcopy(diag)
                for sol in ("observability", "security"):
                    blk = ((diag_30.get("sizing") or {}).get("consolidated") or {}).get(sol) or {}
                    if blk.get("retention_months") is not None:
                        blk["retention_months"] = 1
                if args.skip_deployment_plan:
                    (diag_30.get("sizing") or {}).pop("deployment_plan", None)
                path_30 = diag_path.with_name(diag_path.stem + "_30d.json")
                path_30.write_text(json.dumps(diag_30, indent=2))
                month_cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(path_30), "--name-suffix", "_30d", "--force-months", "1", "--skip-search", *extra]
                if args.skip_xlsx:
                    month_cmd.append("--skip-xlsx")
                _run(month_cmd)
                if not args.skip_deployment_plan:
                    plan_30 = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(path_30), "--deployment-plan", "--name-suffix", "_30d", "--force-months", "1", *extra]
                    if args.skip_xlsx:
                        plan_30.append("--skip-xlsx")
                    try:
                        _run(plan_30)
                    except subprocess.CalledProcessError as e:
                        diag_30 = json.loads(path_30.read_text())
                        diag_30.setdefault("sizing", {})["deployment_plan"] = {
                            "bars": [],
                            "error": f"Observability deployment-count plan was not priced ({e}).",
                        }
                        path_30.write_text(json.dumps(diag_30, indent=2))
                diag_30 = json.loads(path_30.read_text())
                diag_30["consolidation"] = build_consolidation(diag_30, infra)
                diag_30["consolidation"]["retention_scenario"] = "30d"
                diag_30["consolidation"]["retention_set_days"] = 30
                path_30.write_text(json.dumps(diag_30, indent=2))
                monthly = ((diag_30.get("consolidation") or {}).get("savings") or {}).get("serverless_mo")
                diag["consolidation"]["thirty_day"] = {
                    "monthly": monthly,
                    "annual_year1": None if monthly is None else round(float(monthly) * 12, 2),
                }
                diag["thirty_day_account"] = str(path_30)
                folder_30 = ((diag_30.get("sizing") or {}).get("drive_folder") or {})
                if folder_30.get("id"):
                    diag.setdefault("sizing", {})["drive_folder_30d"] = folder_30
            except subprocess.CalledProcessError as e:
                print(f"WARN: 30-day sizing failed ({e})", file=sys.stderr)
                diag.setdefault("consolidation", {}).setdefault("notes", []).append(f"The 30-day PDF was not priced ({e}).")
            except Exception as ex:
                print(f"WARN: 30-day quote failed ({ex!r})", file=sys.stderr)
                diag.setdefault("consolidation", {}).setdefault("notes", []).append(f"The 30-day PDF was not priced ({ex!r}).")
        diag_path.write_text(json.dumps(diag, indent=2))
    try:
        _run_compressioncalc(
            diag, diag_path, infra, py, args,
            size_sheets=sizing_ran and not (diag.get("consolidation") or {}).get("error"),
        )
    except Exception as ex:
        print(f"WARN: compressioncalc_accounted_for failed ({ex!r})", file=sys.stderr)
        diag.setdefault("consolidation", {}).setdefault("notes", []).append(
            f"compressioncalc_accounted_for was not priced ({ex!r})."
        )
    diag_path.write_text(json.dumps(diag, indent=2))
    sz["diagnostics_status"] = "ok"
    sz["diagnostics_dir"] = str(out_dir)
    return diag


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", required=True, help="Organization name as in K360 (match_phrase)")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--days", type=int, default=365, help="Trailing lookback in days. The skill runs the past year (365)")
    ap.add_argument("--reuse", default=None, help="Existing fetch_org.py JSON (skips the K360 fetch)")
    ap.add_argument("--artifacts-root", default=None, help="Parent of <customer>_<date>/ (default ./artifacts)")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD for the run key (default today UTC)")
    ap.add_argument("--retention-months", type=float, default=3.0, help="Assumed retention for the K360 backup (stored GB ÷ retention). K360 has no raw ingest total")
    ap.add_argument("--security-retention-months", type=float, default=None, help="Override for the Security estimator (default = --retention-months)")
    ap.add_argument("--skip-sizing", action="store_true", help="Never run the ECH>50%% estimator step")
    ap.add_argument("--force-sizing", action="store_true", help="Run sizing even if ECH <= 50%%")
    ap.add_argument("--skip-workload", action="store_true", help="Pass through to fetch_org.py (saves 2 ES calls)")
    ap.add_argument("--skip-xlsx", action="store_true")
    ap.add_argument("--always-copy", action="store_true", help="Copy estimators even for empty slices")
    ap.add_argument("--skip-pdf", action="store_true", help="No PDF export of the canvases")
    ap.add_argument("--skip-pdf-upload", action="store_true", help="Keep PDFs local (no Drive upload)")
    ap.add_argument("--skip-diags", action="store_true", help="Do not pull diagnostics. The quote uses the K360 backup (stored GB ÷ assumed retention)")
    ap.add_argument("--diags-dir", default=None, help="Existing directory of admin-console diagnostic zips. Skips the admin API and parses this folder")
    ap.add_argument("--k360-ingest", action="store_true", help="Also price serverless from K360 stored GB ÷ assumed retention. K360 has no raw ingest total, so this backup can sit far below diagnostic bulk. Used automatically when bundles are unavailable")
    ap.add_argument("--skip-vpn-check", action="store_true", help="Pull diagnostics without checking that the Aviatrix VPN tunnel is up")
    ap.add_argument("--skip-deployment-plan", action="store_true", help="Skip the Observability 0/25/50/75/100%% deployment-count sheets. The headline quote still runs")
    args = ap.parse_args()

    fetch_py = ORG_SKILL / "scripts" / "fetch_org.py"
    if not fetch_py.is_file():
        raise SystemExit(f"fetch_org.py not found at {fetch_py} — install k360-org-report or set K360_ORG_SKILL_DIR")

    env_path = Path(args.env)
    env = load_env(env_path)

    # 1. fetch (daily chart grain so daily avg/peak are exact)
    if args.reuse:
        org_json = Path(args.reuse)
        if not org_json.is_file():
            raise SystemExit(f"--reuse not found: {org_json}")
    else:
        base, key = es_base_and_key(env)
        canonical = resolve_org_name(base, key, args.org)
        if canonical != args.org:
            print(f"org → {canonical}", flush=True)
        args.org = canonical
        org_json = Path("/tmp/account_org_report.json")
        cmd = [sys.executable, str(fetch_py), "--org", args.org, "--env", str(env_path), "--days", str(args.days), "--chart", "day", "--out", str(org_json)]
        if args.skip_workload:
            cmd.append("--skip-workload")
        _run(cmd)
    org = json.loads(org_json.read_text())

    # 2. infra (1 ES call)
    base, key = es_base_and_key(env)
    fill_storage_from_data_pivot(base, key, org)
    fill_storage_monthly(base, key, org)
    fill_ingest_doc_bytes(base, key, org)
    infra = fetch_infra(base, key, org)

    # 3. analyze
    when = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()
    acc = build_account(org, infra, when=when, retention_months=args.retention_months, security_retention_months=args.security_retention_months)
    acc["storage"]["size_gaps"] = size_gaps(base, key, acc)
    run_key = acc["meta"]["run_key"]
    root = Path(args.artifacts_root) if args.artifacts_root else Path.cwd() / "artifacts"
    folder = root / run_key
    folder.mkdir(parents=True, exist_ok=True)
    acc["sizing"]["folder"] = str(folder)
    if args.force_sizing:
        acc["sizing"]["triggered"] = True
        acc["platform"]["sizing_triggered"] = True
        acc["sizing"]["forced"] = True
        acc["sizing"]["notes"].append("sizing forced via --force-sizing")
    if args.skip_sizing:
        acc["sizing"]["skipped_reason"] = "--skip-sizing" + (" (would have triggered: ECH > 50%)" if acc["sizing"]["triggered"] else "")
        acc["sizing"]["triggered"] = False
        acc["sizing"]["notes"].append("sizing skipped via --skip-sizing")

    acc_path = folder / f"{run_key}_account.json"
    acc_path.write_text(json.dumps(acc, indent=2))
    (folder / f"{run_key}_org_report.json").write_text(json.dumps(org, separators=(",", ":")))
    (folder / f"{run_key}_infra.json").write_text(json.dumps(infra, separators=(",", ":")))

    # 4. Diagnostics are the default ingest. K360 stored-GB ÷ retention is the backup.
    diag_acc = None
    if acc["sizing"]["triggered"] and not args.skip_diags:
        try:
            diag_acc = _diagnostics_document(acc, infra, folder, env, args)
        except Exception as ex:
            print(f"WARN: diagnostics consolidation failed ({ex!r})", file=sys.stderr)
            acc["sizing"]["diagnostics_status"] = f"diagnostics consolidation failed ({ex!r})"
            diag_acc = None
    acc_path.write_text(json.dumps(acc, indent=2))

    k360_for_pdf = None
    quote_k360 = acc["sizing"]["triggered"] and (diag_acc is None or args.k360_ingest)
    if quote_k360:
        backup = diag_acc is not None
        extra = ["--name-suffix", "from_k360", "--sizing-json", "sizing_from_k360.json"] if backup else []
        sizing_ran = False
        py = _sheets_py()
        if not py.is_file():
            print(f"WARN: Sheets venv missing at {py} — skipping estimator copies (see references/setup.md)", file=sys.stderr)
            acc["sizing"]["estimators"] = {"error": f"sheets venv missing: {py}"}
            acc_path.write_text(json.dumps(acc, indent=2))
        else:
            cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(acc_path), *extra]
            if args.skip_xlsx:
                cmd.append("--skip-xlsx")
            if args.always_copy:
                cmd.append("--always-copy")
            try:
                _run(cmd)
                sizing_ran = True
                acc = json.loads(acc_path.read_text())
            except subprocess.CalledProcessError as e:
                print(f"WARN: K360 sizing step failed ({e}); report still generated", file=sys.stderr)
                acc["sizing"]["estimators"] = {"error": f"size_serverless.py exit {e.returncode}"}
                acc_path.write_text(json.dumps(acc, indent=2))
        try:
            acc["consolidation"] = build_consolidation(acc, infra)
        except Exception as ex:
            print(f"WARN: K360 consolidation failed ({ex!r})", file=sys.stderr)
            acc["consolidation"] = {"triggered": True, "source": "k360", "error": repr(ex)}
        if diag_acc is None:
            why = (acc.get("sizing") or {}).get("diagnostics_status") or "diagnostic bundles were not available"
            notes = (acc.get("consolidation") or {}).setdefault("notes", [])
            notes.insert(0, f"Diagnostic bundles were not used ({why}). This quote is the K360 backup.")
        acc_path.write_text(json.dumps(acc, indent=2))
        if sizing_ran and (acc.get("consolidation") or {}).get("architecture", {}).get("per_region") and not (acc.get("consolidation") or {}).get("error"):
            try:
                cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(acc_path), "--projects", *extra]
                if args.skip_xlsx:
                    cmd.append("--skip-xlsx")
                _run(cmd)
                acc = json.loads(acc_path.read_text())
                acc["consolidation"] = build_consolidation(acc, infra)
                if diag_acc is None:
                    why = (acc.get("sizing") or {}).get("diagnostics_status") or "diagnostic bundles were not available"
                    acc["consolidation"].setdefault("notes", []).insert(0, f"Diagnostic bundles were not used ({why}). This quote is the K360 backup.")
                acc_path.write_text(json.dumps(acc, indent=2))
            except subprocess.CalledProcessError as e:
                print(f"WARN: regional project sizing failed ({e}); report uses the consolidated read-backs", file=sys.stderr)
        if backup:
            acc["k360_ingest"] = _quote_block(acc)

    if diag_acc is not None and acc.get("k360_ingest"):
        diag_mo = ((diag_acc.get("consolidation") or {}).get("savings") or {}).get("serverless_mo")
        k360_mo = (acc["k360_ingest"].get("savings") or {}).get("serverless_mo")
        if diag_mo is not None and k360_mo is not None:
            note = _money_note(diag_mo, k360_mo)
            acc["k360_ingest"].setdefault("notes", []).insert(0, note)
            (diag_acc.get("consolidation") or {}).setdefault("notes", []).insert(0, note)
            (acc.get("consolidation") or {}).setdefault("notes", []).insert(0, note)
        k360_for_pdf = copy.deepcopy(acc)

    if diag_acc is not None and not (diag_acc.get("consolidation") or {}).get("error"):
        dsz = diag_acc.get("sizing") or {}
        sz = acc.setdefault("sizing", {})
        for key in (
            "estimators", "consolidated", "notes", "diagnostics", "drive_folder", "drive_root",
            "drive_folder_30d", "drive_folder_compressioncalc", "drive_folder_compressioncalc_30d", "sized_at",
        ):
            if dsz.get(key) not in (None, {}, []):
                sz[key] = dsz[key]
        sz["ingest_source"] = "diagnostics"
        acc["consolidation"] = diag_acc.get("consolidation")
        if diag_acc.get("compressioncalc_accounted_for"):
            acc["compressioncalc_accounted_for"] = diag_acc["compressioncalc_accounted_for"]
        if diag_acc.get("compressioncalc_account"):
            acc["compressioncalc_account"] = diag_acc["compressioncalc_account"]
        if diag_acc.get("compressioncalc_thirty_day_account"):
            acc["compressioncalc_thirty_day_account"] = diag_acc["compressioncalc_thirty_day_account"]
        cost = (diag_acc.get("analysis") or {}).get("cost")
        if cost:
            acc.setdefault("analysis", {})["cost"] = cost
        acc_path.write_text(json.dumps(acc, indent=2))

    # 5. report
    report_path = folder / f"{run_key}_account_report.md"
    report_path.write_text(render(acc))

    # 6. PDF export. The consolidation PDF is whichever ingest is the quote.
    if not args.skip_pdf:
        try:
            if diag_acc is not None and not (diag_acc.get("consolidation") or {}).get("error"):
                acc["exports"] = export_pdfs(acc, folder, only="account")
                acc["exports"].update(export_pdfs(diag_acc, folder, only="consolidation"))
                path_30 = diag_acc.get("thirty_day_account")
                if path_30 and Path(path_30).is_file():
                    diag_30 = json.loads(Path(path_30).read_text())
                    if not (diag_30.get("consolidation") or {}).get("error"):
                        acc["exports"].update(export_pdfs(
                            diag_30, folder, only="consolidation",
                            consolidation_suffix="Serverless_consolidation_30d_savings",
                            consolidation_kind="consolidation_30d",
                        ))
                comp_path = diag_acc.get("compressioncalc_account")
                if comp_path and Path(comp_path).is_file():
                    comp = json.loads(Path(comp_path).read_text())
                    if not (comp.get("consolidation") or {}).get("error"):
                        acc["exports"].update(export_pdfs(
                            comp, folder, only="consolidation",
                            consolidation_suffix="Serverless_consolidate_meta_overhead_removed",
                            consolidation_kind="consolidation_compressioncalc",
                        ))
                comp_30_path = diag_acc.get("compressioncalc_thirty_day_account")
                if comp_30_path and Path(comp_30_path).is_file():
                    comp_30 = json.loads(Path(comp_30_path).read_text())
                    if not (comp_30.get("consolidation") or {}).get("error"):
                        acc["exports"].update(export_pdfs(
                            comp_30, folder, only="consolidation",
                            consolidation_suffix="Serverless_consolidate_meta_overhead_removed_30d",
                            consolidation_kind="consolidation_compressioncalc_30d",
                        ))
            else:
                acc["exports"] = export_pdfs(acc, folder)
            for kind, e in acc["exports"].items():
                print(f"{kind} PDF → {e.get('pdf') or e.get('error')}", flush=True)
        except Exception as ex:
            print(f"WARN: PDF export failed ({ex!r})", file=sys.stderr)
            acc["exports"] = {"error": repr(ex)}
        if k360_for_pdf and (k360_for_pdf.get("consolidation") or {}).get("source") == "k360" and not (k360_for_pdf.get("consolidation") or {}).get("error"):
            try:
                extra = export_pdfs(k360_for_pdf, folder, only="consolidation", consolidation_suffix=K360_BACKUP_SUFFIX, consolidation_kind="consolidation_k360")
                if not isinstance(acc.get("exports"), dict) or ("error" in acc["exports"] and len(acc["exports"]) == 1):
                    acc["exports"] = {}
                acc["exports"].update(extra)
                for kind, e in extra.items():
                    print(f"{kind} PDF → {e.get('pdf') or e.get('error')}", flush=True)
            except Exception as ex:
                print(f"WARN: K360 backup PDF failed ({ex!r})", file=sys.stderr)
        acc_path.write_text(json.dumps(acc, indent=2))
        py = _sheets_py()
        if not args.skip_pdf_upload and (acc["sizing"].get("drive_folder") or {}).get("id") and py.is_file() and any((e or {}).get("pdf") for e in acc["exports"].values() if isinstance(e, dict)):
            try:
                _run([str(py), str(HERE / "upload_pdfs.py"), "--account-json", str(acc_path)])
                acc = json.loads(acc_path.read_text())
            except subprocess.CalledProcessError as e:
                print(f"WARN: PDF upload failed ({e}); PDFs kept local", file=sys.stderr)

    brief = agent_brief(acc)
    brief_path = folder / f"{run_key}_brief.json"
    brief_path.write_text(json.dumps(brief, separators=(",", ":")))
    est_in = acc["sizing"].get("estimators") if isinstance(acc["sizing"].get("estimators"), dict) else {}
    (folder / f"{run_key}_summary.json").write_text(json.dumps({
        "run_key": run_key,
        "chat_bullets": brief["chat_bullets"],
        "brief": str(brief_path),
        "links": brief["links"],
        "estimators": {k: view for k, v in est_in.items() if (view := _est(v))},
        "savings": brief["savings"],
        "ingest_source": brief.get("ingest_source"),
        "k360_ingest": brief.get("k360_ingest"),
    }, separators=(",", ":")))
    print(f"brief {brief_path}", flush=True)


if __name__ == "__main__":
    main()
