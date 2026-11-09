#!/usr/bin/env python3
"""Diagnostic Serverless consolidation.

Reuses the account-summary fetch, then prices Observability and Security from
diagnostic bundles. Search disk is the local store. The K360 stored-GB backup
is a separate document.

  python run_consolidation.py --org "Acme Corp" --reuse serverless_analyzer/diag_artifacts/<key>/<key>_org_report.json
  python run_consolidation.py --org "Acme Corp" --diags-dir serverless_analyzer/diag_artifacts/<org_id>_diags_<date>

The quote window is the last 30 days (--lookback-days, maximum 365). The account fetch stays a year.

Run from the project root that holds `.env` (KIBANA_URL, ELASTIC_API_KEY, FOUND_ADMIN_API_KEY).
The account summary stays on run_account_analysis.py --skip-diags --skip-sizing.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from skill_paths import find_skill, scripts_dir  # noqa: E402

_account_root = find_skill("k360-account-analysis", HERE)
K360_SCRIPTS = scripts_dir(_account_root)
sys.path.insert(0, str(K360_SCRIPTS))

from consolidation import apply_quote_lookback, build_consolidation  # noqa: E402
from privacy import seal_text  # noqa: E402
from render_pdf import export_documents  # noqa: E402
from report_md import render  # noqa: E402
from run_account_analysis import (  # noqa: E402
    _est,
    _run,
    _sheets_py,
    agent_brief,
    finish_account,
    parse_args,
    prepare_account,
)


def _estimator_flags(args) -> list[str]:
    """Flags for size_serverless.py. xlsx export is off unless --xlsx was passed."""
    flags: list[str] = []
    if getattr(args, "xlsx", False):
        flags.append("--xlsx")
    if getattr(args, "always_copy", False):
        flags.append("--always-copy")
    return flags


def _half_dollar(rate) -> bool:
    try:
        return rate is not None and abs(float(rate) - 0.50) < 0.0001
    except (TypeError, ValueError):
        return False


def _estimator_quote(entry: dict) -> dict:
    keep = ("title", "url", "status", "readback", "region_match", "g26_gate", "readback_rejected", "cells")
    return {k: entry[k] for k in keep if k in entry}


def _search_power_brief(block: dict | None) -> dict | None:
    if not isinstance(block, dict):
        return None
    profiles = []
    for row in block.get("profiles") or []:
        if not isinstance(row, dict):
            continue
        monthly = row.get("Total $/month")
        if monthly is None:
            monthly = row.get("monthly") if row.get("monthly") is not None else row.get("monthly_steady_state")
        profiles.append({
            "profile": row.get("Search Power Setting") or row.get("profile") or row.get("name"),
            "monthly": monthly,
        })
    if not profiles:
        return None
    out = {"profiles": profiles}
    if block.get("region_match"):
        out["region_match"] = block["region_match"]
    return out


def _integrity_brief(block: dict | None) -> dict | None:
    if not isinstance(block, dict) or not (block.get("checks") or "score" in block):
        return None
    failed = [
        {"check": row.get("check"), "detail": row.get("detail"), "score": row.get("score")}
        for row in (block.get("checks") or [])
        if row.get("passed") is False
    ]
    return {"score": block.get("score"), "passed": block.get("passed"), "failed": failed}


def _plan_brief(plan) -> list[dict]:
    rows = plan.get("bars") if isinstance(plan, dict) else plan
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        out.append({"label": row.get("label") or "", "monthly": row.get("monthly"), "projects": row.get("projects")})
    return out


def consolidation_brief(acc: dict) -> dict:
    """Quote slice for the diagnostic skill. The account-summary brief stays separate."""
    m = acc.get("meta") or {}
    sz = acc.get("sizing") or {}
    cons = acc.get("consolidation") or {}
    est_in = sz.get("estimators") if isinstance(sz.get("estimators"), dict) else {}
    estimators = {}
    for key, entry in est_in.items():
        if key == "projects" or not isinstance(entry, dict):
            continue
        estimators[key] = _estimator_quote(entry)
    projects = []
    for entry in est_in.get("projects") or []:
        if isinstance(entry, dict):
            projects.append(_estimator_quote(entry) | {
                "solution": entry.get("solution"),
                "region": entry.get("region"),
            })
    if projects:
        estimators["projects"] = projects
    exports = acc.get("exports") or {}
    rates = [
        {
            "name": row.get("name") or "",
            "raw_gb_day": row.get("raw_gb_day"),
            "metered_gb_day": row.get("metered_gb_day"),
            "usd_per_gb": row.get("usd_per_gb"),
            "at_half_dollar": _half_dollar(row.get("usd_per_gb")),
        }
        for row in (cons.get("metered_rates") or [])
        if isinstance(row, dict)
    ]
    out = {
        "customer": m.get("customer"),
        "run_key": m.get("run_key"),
        "date": m.get("date"),
        "ingest_source": cons.get("source") or sz.get("ingest_source") or "k360",
        "savings": {k: (cons.get("savings") or {}).get(k) for k in ("ech_contract_mo", "serverless_mo", "cost_mo", "cost_yr", "cost_pct") if (cons.get("savings") or {}).get(k) is not None},
        "quotes": ((cons.get("reconciliation") or {}).get("quotes")) or [],
        "fleet": ((cons.get("sensitivity") or {}).get("fleet")) or [],
        "contract_rate": cons.get("contract_rate"),
        "notes": list((cons.get("notes") or [])[:12]),
        "diagnostics_coverage": sz.get("diagnostics") if (cons.get("source") == "diagnostics" or sz.get("ingest_source") == "diagnostics") else None,
        "compressioncalc_accounted_for": acc.get("compressioncalc_accounted_for"),
        "thirty_day": cons.get("thirty_day"),
        "deployment_plan": _plan_brief(cons.get("deployment_plan")),
        "metered_rates": rates,
        "estimators": estimators,
        "links": {
            "folder": sz.get("folder"),
            "drive": (sz.get("drive_folder") or {}).get("url"),
            "drive_30d": (sz.get("drive_folder_30d") or {}).get("url"),
            "drive_compressioncalc": (sz.get("drive_folder_compressioncalc") or {}).get("url"),
            "drive_compressioncalc_30d": (sz.get("drive_folder_compressioncalc_30d") or {}).get("url"),
            "pdfs": {
                k: {field: v.get(field) for field in ("pdf", "html", "drive_url") if v.get(field)}
                for k, v in exports.items() if isinstance(v, dict)
            },
        },
    }
    power = _search_power_brief(cons.get("search_power"))
    if power:
        out["search_power"] = power
    integrity = _integrity_brief(cons.get("integrity"))
    if integrity:
        out["integrity"] = integrity
    return out


def quote_pdf_jobs(account: dict, diagnostic: dict | None, extras: list[tuple[dict | None, str]]) -> list[tuple[dict, str]]:
    """Account PDF plus each consolidation scenario that finished. One pool prints them."""
    diag_cons = (diagnostic or {}).get("consolidation") or {}
    if diagnostic is None or not diag_cons or diag_cons.get("error"):
        docs = [(account, "account")]
        cons = account.get("consolidation") or {}
        if cons and not cons.get("error"):
            docs.append((account, "consolidation"))
        return docs
    docs: list[tuple[dict, str]] = [(account, "account"), (diagnostic, "consolidation")]
    for doc, kind in extras:
        cons = (doc or {}).get("consolidation") or {}
        if doc is not None and cons and not cons.get("error"):
            docs.append((doc, kind))
    return docs


def _load_quote(path: str | None) -> dict | None:
    if not path or not Path(path).is_file():
        return None
    doc = json.loads(Path(path).read_text())
    if (doc.get("consolidation") or {}).get("error"):
        return None
    return doc


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


def _money_note(diag_mo, k360_mo) -> str:
    return (
        f"Discrepancy: diagnostic bulk prices serverless at ${float(diag_mo):,.0f}/mo. "
        f"The K360 backup, stored GB ÷ assumed retention, prices it at ${float(k360_mo):,.0f}/mo. "
        "K360 does not provide a raw ingest total, so those two figures are not interchangeable."
    )


def _compression_totals(acc: dict) -> tuple[float, float | None, float]:
    """Observability plus Security: raw bulk, and that bulk kept at 60%."""
    from admin_diags import COMPRESSION_RATIO

    con = (acc.get("sizing") or {}).get("consolidated") or {}
    bulk = 0.0
    for sol in ("observability", "security"):
        bulk += float((con.get(sol) or {}).get("bulk_gb_day") or 0)
    if bulk <= 0:
        return 0.0, None, 0.0
    return round(bulk, 3), COMPRESSION_RATIO, round(bulk * COMPRESSION_RATIO, 3)


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
    seal_text(path, json.dumps(comp, indent=2))
    block = {
        "bulk_gb_day": bulk,
        "compression_ratio": ratio,
        "gb_day": accounted,
        "account": str(path),
        "formula": (
            "compressioncalc_accounted_for = bulk GB/day × 0.60. "
            "On-disk bytes are not used."
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
    if args.xlsx:
        flags.append("--xlsx")
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
        seal_text(path, json.dumps(comp, indent=2))
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
                seal_text(path, json.dumps(comp, indent=2))
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
        seal_text(path_30, json.dumps(comp_30, indent=2))
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
        seal_text(path_30, json.dumps(comp_30, indent=2))
        monthly_30 = ((comp_30.get("consolidation") or {}).get("savings") or {}).get("serverless_mo")
        comp["consolidation"]["thirty_day"] = {
            "monthly": monthly_30,
            "annual_year1": None if monthly_30 is None else round(float(monthly_30) * 12, 2),
        }
        seal_text(path, json.dumps(comp, indent=2))
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
            from urllib.parse import urlsplit
            from admin_diags import admin_base
            from vpn_check import aviatrix_status
            try:
                host = urlsplit(admin_base(env)).hostname or "admin.found.no"
                vpn_up, vpn_detail = aviatrix_status(host)
            except (RuntimeError, ValueError) as ex:
                vpn_up, vpn_detail = False, str(ex)
            if not vpn_up:
                print(f"WARN: {vpn_detail}", file=sys.stderr)
                if not any(out_dir.glob("*.zip")):
                    sz["diagnostics_status"] = vpn_detail
                    return None
                print("WARN: parsing diagnostic zips already on disk", file=sys.stderr)
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
    apply_quote_lookback(diag, args.lookback_days)
    diag_path = folder / f"{acc['meta']['run_key']}_diags_account.json"
    seal_text(diag_path, json.dumps(diag, indent=2))
    py = _sheets_py()
    sizing_ran = False
    extra = ["--sizing-json", "sizing.json"]
    if py.is_file():
        cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(diag_path), *extra]
        if args.xlsx:
            cmd.append("--xlsx")
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
    seal_text(diag_path, json.dumps(diag, indent=2))
    if sizing_ran and (diag.get("consolidation") or {}).get("architecture", {}).get("per_region") and not (diag.get("consolidation") or {}).get("error"):
        try:
            cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(diag_path), "--projects", *extra]
            if args.xlsx:
                cmd.append("--xlsx")
            _run(cmd)
            diag = json.loads(diag_path.read_text())
            diag["consolidation"] = build_consolidation(diag, infra)
            seal_text(diag_path, json.dumps(diag, indent=2))
        except subprocess.CalledProcessError as e:
            print(f"WARN: diagnostics regional sizing failed ({e}); document uses the consolidated read-backs", file=sys.stderr)
    if sizing_ran and py.is_file() and not (diag.get("consolidation") or {}).get("error"):
        if not args.skip_deployment_plan:
            plan_cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(diag_path), "--deployment-plan", *extra]
            if args.xlsx:
                plan_cmd.append("--xlsx")
            try:
                _run(plan_cmd)
            except subprocess.CalledProcessError as e:
                diag = json.loads(diag_path.read_text())
                diag.setdefault("sizing", {})["deployment_plan"] = {
                    "bars": [],
                    "error": f"Observability deployment-count plan was not priced ({e}).",
                }
                seal_text(diag_path, json.dumps(diag, indent=2))
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
                seal_text(path_30, json.dumps(diag_30, indent=2))
                month_cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(path_30), "--name-suffix", "_30d", "--force-months", "1", "--skip-search", *extra]
                if args.xlsx:
                    month_cmd.append("--xlsx")
                _run(month_cmd)
                if not args.skip_deployment_plan:
                    plan_30 = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(path_30), "--deployment-plan", "--name-suffix", "_30d", "--force-months", "1", *extra]
                    if args.xlsx:
                        plan_30.append("--xlsx")
                    try:
                        _run(plan_30)
                    except subprocess.CalledProcessError as e:
                        diag_30 = json.loads(path_30.read_text())
                        diag_30.setdefault("sizing", {})["deployment_plan"] = {
                            "bars": [],
                            "error": f"Observability deployment-count plan was not priced ({e}).",
                        }
                        seal_text(path_30, json.dumps(diag_30, indent=2))
                diag_30 = json.loads(path_30.read_text())
                diag_30["consolidation"] = build_consolidation(diag_30, infra)
                diag_30["consolidation"]["retention_scenario"] = "30d"
                diag_30["consolidation"]["retention_set_days"] = 30
                seal_text(path_30, json.dumps(diag_30, indent=2))
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
        seal_text(diag_path, json.dumps(diag, indent=2))
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
    seal_text(diag_path, json.dumps(diag, indent=2))
    sz["diagnostics_status"] = "ok"
    sz["diagnostics_dir"] = str(out_dir)
    return diag



def main() -> None:
    args = parse_args(description=__doc__)
    if not 1 <= int(args.lookback_days) <= 365:
        raise SystemExit("--lookback-days must be from 1 to 365")
    acc, infra, folder, env, acc_path = prepare_account(args)
    if args.skip_diags and args.skip_sizing:
        finish_account(acc, folder, acc_path, args)
        return
    # 4. Diagnostics are the default ingest. K360 stored-GB ÷ retention is the backup.
    diag_acc = None
    if acc["sizing"]["triggered"] and not args.skip_diags:
        try:
            diag_acc = _diagnostics_document(acc, infra, folder, env, args)
        except Exception as ex:
            print(f"WARN: diagnostics consolidation failed ({ex!r})", file=sys.stderr)
            acc["sizing"]["diagnostics_status"] = f"diagnostics consolidation failed ({ex!r})"
            diag_acc = None
    seal_text(acc_path, json.dumps(acc, indent=2))

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
            seal_text(acc_path, json.dumps(acc, indent=2))
        else:
            cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(acc_path), *extra]
            if args.xlsx:
                cmd.append("--xlsx")
            if args.always_copy:
                cmd.append("--always-copy")
            try:
                _run(cmd)
                sizing_ran = True
                acc = json.loads(acc_path.read_text())
            except subprocess.CalledProcessError as e:
                print(f"WARN: K360 sizing step failed ({e}); report still generated", file=sys.stderr)
                acc["sizing"]["estimators"] = {"error": f"size_serverless.py exit {e.returncode}"}
                seal_text(acc_path, json.dumps(acc, indent=2))
        try:
            acc["consolidation"] = build_consolidation(acc, infra)
        except Exception as ex:
            print(f"WARN: K360 consolidation failed ({ex!r})", file=sys.stderr)
            acc["consolidation"] = {"triggered": True, "source": "k360", "error": repr(ex)}
        if diag_acc is None:
            why = (acc.get("sizing") or {}).get("diagnostics_status") or "diagnostic bundles were not available"
            notes = (acc.get("consolidation") or {}).setdefault("notes", [])
            notes.insert(0, f"Diagnostic bundles were not used ({why}). This quote is the K360 backup.")
        seal_text(acc_path, json.dumps(acc, indent=2))
        if sizing_ran and (acc.get("consolidation") or {}).get("architecture", {}).get("per_region") and not (acc.get("consolidation") or {}).get("error"):
            try:
                cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(acc_path), "--projects", *extra]
                if args.xlsx:
                    cmd.append("--xlsx")
                _run(cmd)
                acc = json.loads(acc_path.read_text())
                acc["consolidation"] = build_consolidation(acc, infra)
                if diag_acc is None:
                    why = (acc.get("sizing") or {}).get("diagnostics_status") or "diagnostic bundles were not available"
                    acc["consolidation"].setdefault("notes", []).insert(0, f"Diagnostic bundles were not used ({why}). This quote is the K360 backup.")
                seal_text(acc_path, json.dumps(acc, indent=2))
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
        seal_text(acc_path, json.dumps(acc, indent=2))

    # 5. report
    run_key = acc["meta"]["run_key"]
    report_path = folder / f"{run_key}_account_report.md"
    seal_text(report_path, render(acc))

    # 6. PDF export. Every finished scenario is printed on one Chrome pool.
    if not args.skip_pdf:
        try:
            k360_doc = k360_for_pdf if k360_for_pdf and (k360_for_pdf.get("consolidation") or {}).get("source") == "k360" and not (k360_for_pdf.get("consolidation") or {}).get("error") else None
            if diag_acc is not None and not (diag_acc.get("consolidation") or {}).get("error"):
                jobs = quote_pdf_jobs(acc, diag_acc, [
                    (_load_quote(diag_acc.get("thirty_day_account")), "consolidation_30d"),
                    (_load_quote(diag_acc.get("compressioncalc_account")), "consolidation_compressioncalc"),
                    (_load_quote(diag_acc.get("compressioncalc_thirty_day_account")), "consolidation_compressioncalc_30d"),
                    (k360_doc, "consolidation_k360"),
                ])
            else:
                jobs = quote_pdf_jobs(acc, None, [])
            acc["exports"] = export_documents(jobs, folder)
            for kind, e in acc["exports"].items():
                if isinstance(e, dict) and e.get("pdf"):
                    print(f"{kind} PDF → {e['pdf']}", flush=True)
                elif isinstance(e, dict) and e.get("html"):
                    print(f"{kind} HTML → {e['html']}", flush=True)
                else:
                    print(f"{kind} → {e.get('error') if isinstance(e, dict) else e}", flush=True)
        except Exception as ex:
            print(f"WARN: PDF export failed ({ex!r})", file=sys.stderr)
            acc["exports"] = {"error": repr(ex)}
        seal_text(acc_path, json.dumps(acc, indent=2))
        py = _sheets_py()
        if not args.skip_pdf_upload and (acc["sizing"].get("drive_folder") or {}).get("id") and py.is_file() and any((e or {}).get("pdf") for e in acc["exports"].values() if isinstance(e, dict)):
            try:
                _run([str(py), str(K360_SCRIPTS / "upload_pdfs.py"), "--account-json", str(acc_path)])
                acc = json.loads(acc_path.read_text())
            except subprocess.CalledProcessError as e:
                print(f"WARN: PDF upload failed ({e}); PDFs kept local", file=sys.stderr)

    brief = agent_brief(acc)
    brief_path = folder / f"{run_key}_brief.json"
    seal_text(brief_path, json.dumps(brief, separators=(",", ":")))
    printed = brief_path
    if brief.get("ingest_source") == "diagnostics" or (acc.get("sizing") or {}).get("ingest_source") == "diagnostics":
        quote = consolidation_brief(acc)
        printed = folder / f"{run_key}_consolidation_brief.json"
        seal_text(printed, json.dumps(quote, separators=(",", ":")))
    est_in = acc["sizing"].get("estimators") if isinstance(acc["sizing"].get("estimators"), dict) else {}
    seal_text(folder / f"{run_key}_summary.json", json.dumps({
        "run_key": run_key,
        "chat_bullets": brief["chat_bullets"],
        "brief": str(printed),
        "links": brief["links"],
        "estimators": {k: view for k, v in est_in.items() if (view := _est(v))},
        "savings": brief["savings"],
        "ingest_source": brief.get("ingest_source"),
        "k360_ingest": brief.get("k360_ingest"),
    }, separators=(",", ":")))
    print(f"brief {printed}", flush=True)


if __name__ == "__main__":
    main()
