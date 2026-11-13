#!/usr/bin/env python3
"""Diagnostic Serverless consolidation.

Reuses the account-summary fetch, then prices Observability and Security from
diagnostic bundles. Search disk is the local store. The K360 stored-GB backup
is a separate document.

  python run_consolidation.py --org "Acme Corp" --reuse serverless_analyzer/diag_artifacts/<key>/<key>_org_report.json
  python run_consolidation.py --org "Acme Corp" --diags-dir serverless_analyzer/diag_artifacts/<org_id>_diags_<date>

The quote window is the last 90 billed days (--lookback-days, maximum 365). The account fetch stays a year.
ECH dollars are the K360 billing cost field for that window, as a month. One consolidation PDF is
written, at the measured retention month.

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
    _run as _run_command,
    _sheets_py,
    agent_brief,
    finish_account,
    parse_args,
    prepare_account,
)


# The compression-applied set wrote bulk × 0.60 into the raw-data cells, where the
# sheets multiply by 1.66. Since 0.60 × 1.66 is 0.996, that set was the metered quote
# under another name. Bulk now goes into the metered cells directly, so the set
# is not produced. Its code stays until it is removed.
RUN_COMPRESSION_SET = False

# size_serverless.py exits with this status when an estimator copy does not have
# the layout its cell addresses were written for.
TEMPLATE_MISMATCH_EXIT = 3


def _run(cmd: list[str]) -> None:
    """Run one step. A template mismatch stops the whole run instead of quoting without it."""
    try:
        _run_command(cmd)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == TEMPLATE_MISMATCH_EXIT:
            raise SystemExit(
                "ERROR: an estimator copy does not match the layout this skill writes (see the message above). "
                "The run stopped before a quote was written."
            ) from exc
        raise


def method_stamp(lookback_days: int, *, diagnostic: bool = True) -> dict:
    """Which code and which rules produced this run.

    Two runs of one customer can only be compared when they carry the same stamp.
    `commit` is the checkout the scripts ran from. `uncommitted_changes` is true
    when that checkout had edits under serverless_analyzer that no commit records.
    Both are None when the scripts are not in a git checkout.
    """
    root = HERE.parent.parent

    def git(*argv: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    status = git("status", "--porcelain", "--", ".")
    return {
        "commit": git("rev-parse", "--short", "HEAD") or None,
        "uncommitted_changes": None if status is None else bool(status),
        "ingest_basis": "metered diagnostic bulk" if diagnostic else "K360 stored GB ÷ assumed retention",
        "ech_dollars": "k360_cost",
        "quote_window_days": int(lookback_days),
        "retention": "measured month, rounded up" if diagnostic else "assumed month",
        "integrity_pass": "every check",
    }


def quote_options_from(args) -> dict:
    """The comparison flags, stored on the account so the consolidation is a pure function of it."""
    return {
        "ech_scope": getattr(args, "ech_scope", "priced") or "priced",
        "unmeasured": getattr(args, "unmeasured", "exclude") or "exclude",
        "min_priced_ecu_pct": float(getattr(args, "min_priced_ecu_pct", 90.0)),
        "keep_serverless_spend": bool(getattr(args, "keep_serverless_spend", False)),
    }


def _estimator_flags(args) -> list[str]:
    """Flags for size_serverless.py. xlsx export is off unless --xlsx was passed."""
    flags: list[str] = []
    if getattr(args, "xlsx", False):
        flags.append("--xlsx")
    if getattr(args, "always_copy", False):
        flags.append("--always-copy")
    if getattr(args, "fresh", False):
        flags.append("--fresh")
    return flags


def _half_dollar(rate) -> bool:
    try:
        return rate is not None and abs(float(rate) - 0.50) < 0.0001
    except (TypeError, ValueError):
        return False


def _estimator_quote(entry: dict) -> dict:
    keep = ("title", "url", "status", "readback", "region_match", "echo_gate", "readback_rejected", "cells")
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
        "savings": {k: (cons.get("savings") or {}).get(k) for k in ("ech_basis", "ech_list_mo", "serverless_mo", "list_mo", "list_yr", "list_pct") if (cons.get("savings") or {}).get(k) is not None},
        "quotes": ((cons.get("reconciliation") or {}).get("quotes")) or [],
        "fleet": ((cons.get("sensitivity") or {}).get("fleet")) or [],
        "contract_rate": cons.get("contract_rate"),
        "ech_scope": cons.get("ech_scope"),
        "notes": list((cons.get("notes") or [])[:12]),
        "diagnostics_coverage": sz.get("diagnostics") if (cons.get("source") == "diagnostics" or sz.get("ingest_source") == "diagnostics") else None,
        "excluded_deployments": m.get("excluded_deployments"),
        "method": m.get("method"),
        "deployment_plan": _plan_brief(cons.get("deployment_plan")),
        "metered_rates": rates,
        "estimators": estimators,
        "links": {
            "folder": sz.get("folder"),
            "drive": (sz.get("drive_folder") or {}).get("url"),
            "pdfs": {
                k: {field: v.get(field) for field in ("pdf", "html", "drive_url") if v.get(field)}
                for k, v in exports.items() if isinstance(v, dict)
            },
        },
    }
    org_ids = [str(i) for i in ((m.get("org") or {}).get("ids") or []) if i]
    if org_ids:
        out["organization_id"] = (m.get("org") or {}).get("id")
        out["organization_ids"] = org_ids
    power = _search_power_brief(cons.get("search_power"))
    if power:
        out["search_power"] = power
    integrity = _integrity_brief(cons.get("integrity"))
    if integrity:
        out["integrity"] = integrity
    return out


def _with_appendix(docs: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """One appendix document, from the full-retention bulk quote, in the run folder."""
    source = next((doc for doc, kind in docs if kind == "consolidation"), None)
    if source is None:
        return docs
    return docs + [(source, "appendix")]


def quote_pdf_jobs(account: dict, diagnostic: dict | None, extras: list[tuple[dict | None, str]]) -> list[tuple[dict, str]]:
    """Account PDF plus each consolidation scenario that finished. One pool prints them."""
    diag_cons = (diagnostic or {}).get("consolidation") or {}
    if diagnostic is None or not diag_cons or diag_cons.get("error"):
        docs = [(account, "account")]
        cons = account.get("consolidation") or {}
        if cons and not cons.get("error"):
            docs.append((account, "consolidation"))
        return _with_appendix(docs)
    docs: list[tuple[dict, str]] = [(account, "account"), (diagnostic, "consolidation")]
    for doc, kind in extras:
        cons = (doc or {}).get("consolidation") or {}
        if doc is not None and cons and not cons.get("error"):
            docs.append((doc, kind))
    return _with_appendix(docs)


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
        "savings": {k: (cons.get("savings") or {}).get(k) for k in ("ech_basis", "ech_list_mo", "serverless_mo", "list_mo", "list_yr", "list_pct") if (cons.get("savings") or {}).get(k) is not None},
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
    """Observability, Security, and Search: raw bulk, and that bulk kept at 60%."""
    from admin_diags import COMPRESSION_RATIO

    con = (acc.get("sizing") or {}).get("consolidated") or {}
    bulk = 0.0
    has_workload = False
    for sol in ("observability", "security", "search"):
        blk = con.get(sol) or {}
        bulk += float(blk.get("bulk_gb_day") or 0)
        if (blk.get("stored_gb") or 0) or (blk.get("bulk_gb_day") or 0) or (blk.get("daily_raw_gb") or 0):
            has_workload = True
    if not has_workload:
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
    if ratio is None or not size_sheets or not py.is_file():
        if ratio is None:
            block["status"] = "not priced — no workload to compress"
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
    if args.fresh:
        flags.append("--fresh")
    try:
        _run([
            str(py), str(HERE / "size_serverless.py"),
            "--account-json", str(path),
            "--name-suffix", COMPRESSIONCALC,
            "--sizing-json", "sizing_compressioncalc_accounted_for.json",
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

    org_meta = (acc.get("meta") or {}).get("org") or {}
    org_id = org_meta.get("id")
    org_ids = [str(i) for i in (org_meta.get("ids") or []) if i]
    if org_id and str(org_id) not in org_ids:
        org_ids.insert(0, str(org_id))
    if not org_ids and org_id:
        org_ids = [str(org_id)]
    when = (acc.get("meta") or {}).get("date")
    download_failed: list[dict] = []
    if args.diags_dir:
        out_dir = Path(args.diags_dir)
        if not out_dir.is_dir():
            print(f"WARN: --diags-dir not found: {out_dir}", file=sys.stderr)
            sz["diagnostics_status"] = f"--diags-dir not found: {out_dir}"
            return None
    else:
        if not org_ids:
            print("WARN: no organization id — diagnostic consolidation unavailable", file=sys.stderr)
            sz["diagnostics_status"] = "no organization id"
            return None
        label = "+".join(org_ids) if len(org_ids) > 1 else org_ids[0]
        out_dir = folder.parent / f"{label}_diags_{when}"
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
            errors = []
            for oid in org_ids:
                try:
                    pulled = pull_diagnostics(env, oid, out_dir, exclude_ids=set(getattr(args, "exclude_ids", None) or []))
                    download_failed.extend(
                        {"name": r.get("name"), "id": r.get("id"), "error": r.get("error"), "attempts": r.get("attempts")}
                        for r in pulled if r.get("result") == "error"
                    )
                except Exception as ex:
                    errors.append(f"{oid}: {ex!r}")
                    print(f"WARN: diagnostic pull failed for {oid} ({ex!r})", file=sys.stderr)
            if download_failed and not getattr(args, "allow_missing_diags", False):
                names = ", ".join(str(r["name"]) for r in download_failed)
                raise SystemExit(
                    f"ERROR: {len(download_failed)} diagnostic download(s) failed after retries: {names}. "
                    "Rerun to collect them, or pass --allow-missing-diags to quote without them."
                )
            if errors and not any(out_dir.glob("*.zip")):
                sz["diagnostics_status"] = "; ".join(errors)
                return None
    zips = list(out_dir.glob("*.zip"))
    if not zips:
        print("WARN: no diagnostic bundles — the quote falls back to K360 stored GB", file=sys.stderr)
        sz["diagnostics_status"] = "no diagnostic bundles"
        return None
    captured = datetime.fromtimestamp(max(p.stat().st_mtime for p in zips), timezone.utc)
    try:
        rows = parse_diagnostics(out_dir, captured=captured, exclude_ids=set(getattr(args, "exclude_ids", None) or []))
    except Exception as ex:
        print(f"WARN: diagnostic parse failed ({ex!r})", file=sys.stderr)
        sz["diagnostics_status"] = f"diagnostic parse failed ({ex!r})"
        return None
    if not rows:
        sz["diagnostics_status"] = "no diagnostic rows"
        return None
    admin_names = None
    try:
        from admin_diags import admin_base, list_deployments
        base = admin_base(env)
        key = env.get("FOUND_ADMIN_API_KEY")
        names: set[str] = set()
        for oid in org_ids:
            names.update(str(x.get("name")) for x in list_deployments(base, key, oid) if x.get("name"))
        admin_names = names or None
    except Exception as ex:
        print(f"WARN: admin deployment list unavailable ({ex!r}); unmatched deployments stay 'missing diagnostic'", file=sys.stderr)
    options = quote_options_from(args)
    diag = apply_diagnostics(
        acc, rows, admin_names,
        unmeasured=options["unmeasured"],
        fallback_months=float(getattr(args, "retention_months", 3.0) or 3.0),
        security_fallback_months=getattr(args, "security_retention_months", None),
    )
    missing = (diag["sizing"].get("diagnostics") or {}).get("fallback_names") or []
    if options["unmeasured"] == "fail" and missing:
        raise SystemExit(
            f"ERROR: {len(missing)} active deployment(s) have no diagnostic bundle: {', '.join(str(n) for n in missing)}. "
            "--unmeasured fail stops the run. Collect the bundles, or pass --unmeasured exclude or --unmeasured k360."
        )
    diag["sizing"]["quote_options"] = options
    diag.setdefault("meta", {})["method"] = method_stamp(args.lookback_days)
    if download_failed:
        diag["sizing"].setdefault("diagnostics", {})["download_failed"] = download_failed
    apply_quote_lookback(diag, args.lookback_days)
    diag_path = folder / f"{acc['meta']['run_key']}_diags_account.json"
    seal_text(diag_path, json.dumps(diag, indent=2))
    py = _sheets_py()
    sizing_ran = False
    extra = ["--sizing-json", "sizing.json"]
    if args.fresh:
        extra.append("--fresh")
    if py.is_file():
        cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(diag_path), *extra]
        if args.xlsx:
            cmd.append("--xlsx")
        if args.always_copy:
            cmd.append("--always-copy")
        if args.fresh:
            cmd.append("--fresh")
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
        seal_text(diag_path, json.dumps(diag, indent=2))
    if RUN_COMPRESSION_SET:
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
    if not 0 <= float(args.min_priced_ecu_pct) <= 100:
        raise SystemExit("--min-priced-ecu-pct must be from 0 to 100")
    if args.keep_serverless_spend and args.ech_scope == "priced":
        print("NOTE: --keep-serverless-spend has no effect with --ech-scope priced; only priced deployments are compared.", file=sys.stderr)
    # A consolidation run leaves stack-monitoring clusters out. The account summary does not.
    args.auto_exclude_monitoring = not getattr(args, "keep_monitoring", False)
    acc, infra, folder, env, acc_path = prepare_account(args)
    # Until a diagnostic quote replaces it, this account is the K360 backup.
    acc.setdefault("meta", {})["method"] = method_stamp(args.lookback_days, diagnostic=False)
    acc.setdefault("sizing", {})["quote_options"] = quote_options_from(args)
    if args.skip_diags and args.skip_sizing:
        finish_account(acc, folder, acc_path, args)
        return
    if not (acc.get("ecu") or {}).get("daily_totals"):
        raise SystemExit(
            "ERROR: this org report has no daily billing series, so there is no quote window to price. "
            "It was fetched at week grain (run_org_report.py writes that). Run without --reuse, or reuse an "
            "org report written by run_account_analysis.py or run_consolidation.py."
        )
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
        # The backup is priced on the same billing window as a diagnostic quote, so --lookback-days
        # and --ech-scope mean the same thing on both. Its stored-GB ingest takes the window's pace.
        apply_quote_lookback(acc, args.lookback_days)
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
            if args.fresh:
                cmd.append("--fresh")
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
            "drive_folder_compressioncalc", "sized_at",
        ):
            if dsz.get(key) not in (None, {}, []):
                sz[key] = dsz[key]
        sz["ingest_source"] = "diagnostics"
        acc["consolidation"] = diag_acc.get("consolidation")
        acc.setdefault("meta", {})["method"] = (diag_acc.get("meta") or {}).get("method")
        # The account now carries the diagnostic quote, so it carries that quote's window figures too.
        for key in ("quote_lookback_requested", "quote_lookback_days", "quote_daily_avg", "quote_monthly_ecu", "quote_usage_factor", "quote_ingest_factor"):
            if key in (diag_acc.get("ecu") or {}):
                acc.setdefault("ecu", {})[key] = diag_acc["ecu"][key]
        if diag_acc.get("compressioncalc_accounted_for"):
            acc["compressioncalc_accounted_for"] = diag_acc["compressioncalc_accounted_for"]
        if diag_acc.get("compressioncalc_account"):
            acc["compressioncalc_account"] = diag_acc["compressioncalc_account"]
        cost = (diag_acc.get("analysis") or {}).get("cost")
        if cost:
            acc.setdefault("analysis", {})["cost"] = cost
        seal_text(acc_path, json.dumps(acc, indent=2))

    # 4b. One estimate per title: after every sizing pass, trash older same-titled sheets.
    dedupe_py = _sheets_py()
    sized_acc = diag_acc if diag_acc is not None else acc
    if dedupe_py.is_file() and (sized_acc.get("sizing") or {}).get("drive_folder"):
        dedupe_src = folder / f"{acc['meta']['run_key']}_diags_account.json"
        if not dedupe_src.is_file():
            dedupe_src = acc_path
        try:
            _run([str(dedupe_py), str(HERE / "size_serverless.py"), "--account-json", str(dedupe_src), "--dedupe"])
        except subprocess.CalledProcessError as e:
            print(f"WARN: estimate dedupe failed ({e}); check the Drive folder for repeated titles", file=sys.stderr)

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
                    (_load_quote(diag_acc.get("compressioncalc_account")), "consolidation_compressioncalc"),
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
