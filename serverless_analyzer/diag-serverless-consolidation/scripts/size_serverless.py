#!/usr/bin/env python3
"""ECH > 50% rule: copy golden estimators into Drive folder <customer>_<date>, fill, read back.

Run with the Sheets venv python (google-api-python-client installed):

  ~/.config/serverless-analyzer/venv/bin/python size_serverless.py \
      --account-json serverless_analyzer/diag_artifacts/Acme_2026-11-02/Acme_2026-11-02_account.json

Produces a Drive folder <customer>_<date>. Every PDF stays in that folder: the account
PDF, the appendix, and the consolidation PDF.
Estimate sheets go directly in generated_estimates. There is no full_retention or 30d folder.
Deployment-plan sheets stay in generated_estimates/plan_sheets.
Each solution with a workload gets one sheet per region. A solution with no workload is skipped.
  <solution>_full retention_bulk
Observability and Security write GB/day at the average index retention, rounded up to a whole month. The size-weighted age is `retention_months_measured`.
Search disk is the measured local store. It is not scaled by a retention month.
Sheet titles are
  <customer>_<date>_<search|observability|security>_full retention_<bulk|k360>_<region>_serverless_estimate
Local .xlsx export is off unless `--xlsx` is passed. Search CSV reports still use that title. An estimate that an earlier run filed under generated_estimates/full_retention or generated_estimates/30d is moved up into generated_estimates, on disk and on Drive, and the empty folder is removed.
  sizing.json ; account JSON `sizing.estimators` updated

Never mutates the golden templates.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import sizing_shared
from search_inputs import exact_inputs
from constants import TEMPLATE_MISMATCH_EXIT
from sizing_shared import (
    DAYS_PER_MONTH, DEFAULT_O11Y, DEFAULT_SECURITY, O11Y_READBACK, O11Y_TAB, PLAN_FOLDER_NAME,
    SEC_TAB, TemplateMismatch, echo_gate, observability_echo_gate,
    observability_readback, seal_dir, seal_text,
)
from sheets_io import (
    copy_verified, dedupe_estimates, ensure_drive_folder, export_xlsx, push_cells, read_cells,
    services, sheet_id, trash_file, write_search_sheet,
)
from serverless_regions import solution_provider  # noqa: E402
from estimate_paths import (
    _fill_missing_doc_bytes, _months_for_sheet, _search_volume_scale, _suffix, architecture_regions,
    estimate_title, estimator_skip, fleet_sheet_region, open_estimate_place, prefer_regional_estimators,
)
from sizing_fill import (
    _region_rejected, fill_security, ingest_basis, match_region, observability_fill,
    observability_input_cells, region_match_text, region_options, run_es3, security_expansion,
    security_input_cells,
)
from search_sheet import price_search
from sizing_shared import DEFAULT_SEARCH
from plan_sizing import (
    assemble_plan_bars, org_store_gb, plan_roster, plan_sheet_jobs, size_deployment_plan,
    size_regional_projects,
)

# size_serverless.py is the CLI the sizing family is driven through, and the tests reach the
# steps by importing them from here. These re-exports are deliberate.
from sizing_shared import COMPRESSION_FOLDER_NAME, FOLDER_MIME, GSHEET_MIME
from sheets_io import (
    _execute, assert_not_template, cell_input_mode, copy_template, find_in_folder,
    map_in_order, partition_cells, require_customer_parent,
)
from estimate_paths import (
    _sheet_retention, drive_children, estimate_folder_name, estimate_subfolder,
    relocate_drive_estimates, relocate_local_estimates,
)

def _checkpoint_sizing(local_dir: Path, sizing_name: str, sz: dict, estimators: dict) -> None:
    """Write sizing.json after a solution so a later timeout keeps the read-backs already priced."""
    sz["estimators"] = estimators
    seal_text(Path(local_dir) / sizing_name, json.dumps(sz, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-json", required=True)
    ap.add_argument("--o11y-id", default=os.environ.get("O11Y_SHEET_ID", DEFAULT_O11Y), type=sheet_id, help="Observability golden template (id or URL)")
    ap.add_argument("--security-id", default=os.environ.get("SECURITY_SHEET_ID", DEFAULT_SECURITY), type=sheet_id, help="Security golden template (id or URL)")
    ap.add_argument("--support", default=None, help="Support tier for both sheets (default from JSON: Premium)")
    ap.add_argument("--xlsx", action="store_true", help="Export filled estimators as local xlsx workbooks")
    ap.add_argument("--skip-xlsx", action="store_true", help="Do not export xlsx (this is the default)")
    ap.add_argument("--always-copy", action="store_true", help="Copy an estimator even when its slice has no workload")
    ap.add_argument("--fresh", action="store_true", help="Always create new copies (default reuses same-named copies in the folder)")
    ap.add_argument("--projects", action="store_true", help="Only write one sizing per regional project; leave the consolidated copies in place")
    ap.add_argument("--search-sheets", action="store_true", help="Redo only the Search estimator copy (the sheet, or with --search-notebook the library report as a Google Sheet) and leave the other copies in place")
    ap.add_argument("--search-notebook", action="store_true", help="Price Search with the es3-estimator notebook library instead of the Search estimator sheet (the default)")
    ap.add_argument("--search-id", default=os.environ.get("SEARCH_SHEET_ID", DEFAULT_SEARCH), type=sheet_id, help="Search golden template (id or URL)")
    ap.add_argument("--name-suffix", default="", help="Appended to estimator titles so a second sizing does not overwrite the first")
    ap.add_argument("--sizing-json", default=None, help="Filename for the sizing sidecar (default sizing.json)")
    ap.add_argument("--force-months", type=int, default=None, help="Write this whole month on the Observability and Security sheets when a measured month already exists. Search disk is never scaled by a month")
    ap.add_argument("--skip-search", action="store_true", help="Keep the Search estimator already on the account JSON")
    ap.add_argument("--dedupe", action="store_true", help="Trash all but the newest sheet for each estimate title under this run's Drive folder, then exit")
    ap.add_argument("--dedupe-dry-run", action="store_true", help="With --dedupe: list the duplicates and trash nothing")
    ap.add_argument("--deployment-plan", action="store_true", help="Price the deployment-count bars from one project per ECH deployment down to one project per feature per region")
    ap.add_argument("--allow-nearest-region", action="store_true", help="When the provider default is also absent, write the nearest same-provider region. A region that is not listed is otherwise priced as AWS us-east-1, Azure eastus, or GCP us-east1. The quote stays unverified.")
    args = ap.parse_args()
    import sizing_shared
    sizing_shared._ALLOW_NEAREST = bool(args.allow_nearest_region)
    sizing_shared._SEARCH_NOTEBOOK = bool(args.search_notebook)
    sizing_shared._SEARCH_TEMPLATE_ID = args.search_id
    args.skip_xlsx = not args.xlsx

    acc_path = Path(args.account_json)
    acc = json.loads(acc_path.read_text())
    _fill_missing_doc_bytes(acc_path, acc)
    sz = acc.get("sizing") or {}
    if not sz:
        raise SystemExit("account JSON has no `sizing` block — run analyze.py first")
    meta = acc["meta"]
    key = meta["run_key"]
    local_dir = acc_path.parent
    seal_dir(local_dir)
    con = sz["consolidated"]
    suffix = _suffix(args.name_suffix)
    sizing_name = args.sizing_json or "sizing.json"
    support = args.support or sz.get("support_tier") or "Premium"
    provider, region = sz.get("provider"), sz.get("region")
    basis = ingest_basis(acc)

    sheets, drive = services()
    stored_root = (sz.get("drive_root") or {}).get("id")
    if stored_root:
        root = {
            "id": stored_root,
            "name": (sz.get("drive_root") or {}).get("name") or key,
            "url": (sz.get("drive_root") or {}).get("url") or f"https://drive.google.com/drive/folders/{stored_root}",
        }
    else:
        root = ensure_drive_folder(drive, key)
    if args.dedupe:
        removed = dedupe_estimates(drive, root["id"], dry_run=args.dedupe_dry_run)
        verb = "would trash" if args.dedupe_dry_run else "trashed"
        for r in removed:
            print(f"{verb} duplicate {r['name']} ({r['id']}) in {r['path'] or '/'}; kept {r['kept']}")
        print(f"dedupe: {len(removed)} duplicate estimate(s) {verb.replace('would trash', 'found')}")
        return
    folder, estimate_dir, estimates_root, moved = open_estimate_place(drive, root["id"], local_dir, suffix)
    lines = [f"Local folder → {estimate_dir}", f"Drive folder → {folder['url']}"]
    if moved:
        lines.append(f"Moved {len(moved)} estimate(s) into retention folders")
    if args.projects:
        # --skip-search keeps Search projects already on the account JSON.
        only = {"observability", "security"} if args.skip_search else None
        projects = size_regional_projects(
            acc, sheets, drive, folder, o11y_id=args.o11y_id, security_id=args.security_id,
            support=support, provider=provider, skip_xlsx=args.skip_xlsx, fresh=args.fresh,
            local_dir=estimate_dir, customer=meta["customer"], when=meta["date"], name_suffix=suffix,
            only=only, force_months=args.force_months,
        )
        if args.skip_search:
            kept = [p for p in ((sz.get("estimators") or {}).get("projects") or []) if p.get("solution") == "search"]
            projects = projects + kept
        sz.setdefault("estimators", {})["projects"] = projects
        prefer_regional_estimators(sz["estimators"])
        sz["folder"] = str(local_dir)
        sz["estimate_folder"] = str(estimate_dir)
        sz["drive_root"] = root
        sz["drive_folder"] = folder
        sz["sized_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        acc["sizing"] = sz
        seal_text(acc_path, json.dumps(acc, indent=2))
        seal_text(local_dir / sizing_name, json.dumps(sz, indent=2))
        for p in projects:
            rb = p.get("readback") or {}
            lines.append(f"{p['solution']} {p['region']} → {p.get('url') or p.get('error')}  (monthly {rb.get('monthly_steady_state')})")
        print("\n".join(lines))
        return
    if args.search_sheets:
        estimators = sz.setdefault("estimators", {})
        se = con["search"]
        search_regions = architecture_regions(acc, "search")
        search_scope = fleet_sheet_region(acc, "search", region)
        if search_scope is None:
            estimators["search"] = {"kind": "search", "status": "sized per region", "regions": search_regions}
            lines.append(f"Search → one sheet per region ({', '.join(search_regions)})")
        else:
            title_se = estimate_title(key, "search", suffix, search_scope)
            store_scale = _search_volume_scale(suffix)
            if (se.get("stored_gb") or 0) > 0:
                result, published = price_search(
                    drive, sheets, folder["id"], estimate_dir, title_se, {
                        "stored_gb": float(se.get("stored_gb") or 0) * store_scale,
                        "org_stored_gb": exact_inputs(se.get("workload") or acc.get("workload") or {}, search_scope or region, float(se.get("stored_gb") or 0) * store_scale, org_store_gb(acc) * store_scale)[1],
                        "provider": solution_provider(acc, "search", provider),
                        "region": search_scope or region,
                        "support_tier": support,
                        "workload": exact_inputs(se.get("workload") or acc.get("workload") or {}, search_scope or region, float(se.get("stored_gb") or 0) * store_scale, org_store_gb(acc) * store_scale)[0],
                    }, notebook=args.search_notebook, template_id=args.search_id, reuse=not args.fresh, skip_xlsx=args.skip_xlsx,
                )
                if not published.get("url") and not published.get("error"):
                    published = {"error": result.get("error") or "no report"}
            else:
                result, published = {}, {"error": "no report"}
            entry = dict(estimators.get("search") or {"kind": "search", "title": title_se})
            entry["region"] = search_scope or region
            entry.pop("doc_id", None)
            entry.update({
                "status": result.get("status") or "error",
                "local": published.get("local"),
                "url": published.get("url"),
                "spreadsheet_id": published.get("spreadsheet_id"),
                "readback": result.get("readback") or entry.get("readback"),
                "profiles": result.get("profiles") or entry.get("profiles"),
                "region_match": result.get("region_match") or entry.get("region_match"),
            })
            if published.get("error"):
                entry["error"] = published["error"]
            estimators["search"] = entry
            rb = result.get("readback") or {}
            lines.append(f"Search → {entry.get('url') or entry.get('error')}  (performant {rb.get('monthly_steady_state')})")
        fresh = size_regional_projects(
            acc, sheets, drive, folder, o11y_id=args.o11y_id, security_id=args.security_id,
            support=support, provider=provider, skip_xlsx=args.skip_xlsx, fresh=args.fresh,
            local_dir=estimate_dir, customer=meta["customer"], when=meta["date"], only={"search"},
            name_suffix=suffix,
        )
        kept = [p for p in (estimators.get("projects") or []) if p.get("solution") != "search"]
        estimators["projects"] = fresh + kept
        prefer_regional_estimators(estimators)
        for p in fresh:
            prb = p.get("readback") or {}
            lines.append(f"{p['solution']} {p['region']} → {p.get('url') or p.get('error')}  (monthly {prb.get('monthly_steady_state')})")
        sz["folder"] = str(local_dir)
        sz["estimate_folder"] = str(estimate_dir)
        sz["drive_root"] = root
        sz["drive_folder"] = folder
        sz["sized_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        acc["sizing"] = sz
        seal_text(acc_path, json.dumps(acc, indent=2))
        seal_text(local_dir / sizing_name, json.dumps(sz, indent=2))
        print("\n".join(lines))
        return
    if args.deployment_plan:
        plan_folder = ensure_drive_folder(drive, PLAN_FOLDER_NAME, estimates_root["id"])
        lines.append(f"Plan folder → {plan_folder['url']}")
        plan = size_deployment_plan(
            acc, sheets, drive, plan_folder, o11y_id=args.o11y_id, security_id=args.security_id,
            support=support, provider=provider,
            force_months=args.force_months, name_suffix=suffix, fresh=args.fresh,
        )
        sz["deployment_plan"] = plan
        sz["drive_folder_deployment_plan"] = plan_folder
        sz["folder"] = str(local_dir)
        sz["estimate_folder"] = str(estimate_dir)
        sz["drive_root"] = root
        sz["drive_folder"] = folder
        sz["sized_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        acc["sizing"] = sz
        seal_text(acc_path, json.dumps(acc, indent=2))
        seal_text(local_dir / sizing_name, json.dumps(sz, indent=2))
        lines.append(plan.get("error") or f"Deployment plan → {len(plan.get('bars') or [])} bars")
        print("\n".join(lines))
        return
    estimators: dict = dict(sz.get("estimators") or {})

    # ---------------- Observability
    o = con["observability"]
    o_regions = architecture_regions(acc, "observability")
    o_scope = fleet_sheet_region(acc, "observability", region)
    o_skip = estimator_skip(o, args.force_months, always_copy=args.always_copy)
    if o_scope is None:
        estimators["observability"] = {"kind": "observability", "status": "sized per region", "regions": o_regions}
        lines.append(f"Observability → one sheet per region ({', '.join(o_regions)})")
    elif o_skip == "no_workload":
        estimators["observability"] = {"kind": "observability", "status": "skipped — no observability workload detected (use --always-copy to force)"}
        lines.append("Observability → skipped (no workload)")
    elif o_skip == "retention":
        estimators["observability"] = {"kind": "observability", "status": "skipped — no diagnostic bulk to write a one-year estimate from"}
        lines.append("Observability → skipped (no diagnostic bulk to write a one-year estimate from)")
    else:
        title_o = estimate_title(key, "observability", suffix, o_scope)
        o_months = _months_for_sheet(o, args.force_months)
        copied, reused, layout = copy_verified(drive, sheets, args.o11y_id, title_o, folder["id"], reuse=not args.fresh, kind="observability", tab=O11Y_TAB)
        opts = region_options(sheets, copied["id"])
        reg, how = match_region(solution_provider(acc, "observability", provider), o_scope or region, opts)
        if how == "none" or not reg:
            reason = _region_rejected(o_scope or region)
            try:
                trash_file(drive, copied["id"])
            except Exception:  # noqa: BLE001
                pass
            estimators["observability"] = {
                "kind": "observability", "title": title_o, "region": o_scope or region, "region_match": "none",
                "readback_rejected": reason, "readback": {}, "status": "unquoted",
            }
            lines.append(f"Observability → {reason}")
        else:
            daily = float(o.get("daily_raw_gb") or 0)
            match_text = region_match_text(reg, how, o_scope or region)
            o_mix = None
            if o.get("mix_basis"):
                o_mix = {
                    "basis": o.get("mix_basis"),
                    "logs_gb_day": o.get("logs_gb_day"), "metrics_gb_day": o.get("metrics_gb_day"),
                    "traces_gb_day": o.get("traces_gb_day"), "k8s_logs_share": o.get("k8s_logs_share"),
                    "metrics_retention_days": float(o["retention_months_metrics"]) * DAYS_PER_MONTH if o.get("retention_months_metrics") else None,
                }
                cells = observability_fill(acc, support, reg, o_months, daily, o_mix, args.force_months is not None, basis)
            else:
                cells = observability_input_cells(
                    acc, support, reg, o_months, daily, logs=o.get("logs_gb_day"), metrics=o.get("metrics_gb_day"), basis=basis,
                )
            n = push_cells(sheets, copied["id"], O11Y_TAB, cells, allowed_text=set(opts))
            rb = observability_readback(read_cells(sheets, copied["id"], O11Y_TAB, O11Y_READBACK), basis)
            entry = {"kind": "observability", "region": o_scope or region, "title": copied["name"], "spreadsheet_id": copied["id"], "url": copied["url"], "template_id": args.o11y_id, "reused_existing_copy": reused, "tab": O11Y_TAB, "cells": cells, "cells_written": n, "region_match": match_text, "readback": rb, "status": "filled", "template_version": layout["version"], "template_newer_than_validated": layout["newer_than_validated"]}
            entry["ingest_basis"] = basis
            entry["echo_gate"] = observability_echo_gate(o_mix or o, rb) if basis == "metered" else echo_gate(daily, rb.get("echo_gb_day"))
            if not entry["echo_gate"]["accepted"]:
                entry["readback_rejected"] = "the sheet echo differs from the written bulk by more than the tolerance (0.1% of it, at least 0.02 GB/day)"
            if not args.skip_xlsx:
                try:
                    x = export_xlsx(drive, copied["id"], estimate_dir / f"{title_o}.xlsx")
                    entry["local_xlsx"] = str(x)
                except Exception as ex:  # noqa: BLE001
                    entry["local_xlsx_error"] = str(ex)
            estimators["observability"] = entry
            lines.append(f"Observability → {copied['url']}  ({n} cells; Y1 {rb.get('annual_year1')}, monthly {rb.get('monthly_steady_state')}; region {reg} [{how}])")

    _checkpoint_sizing(local_dir, sizing_name, sz, estimators)

    # ---------------- Security
    s = con["security"]
    s_regions = architecture_regions(acc, "security")
    s_scope = fleet_sheet_region(acc, "security", region)
    s_skip = estimator_skip(s, args.force_months, always_copy=args.always_copy)
    if s_scope is None:
        estimators["security"] = {"kind": "security", "status": "sized per region", "regions": s_regions}
        lines.append(f"Security → one sheet per region ({', '.join(s_regions)})")
    elif s_skip == "no_workload":
        estimators["security"] = {"kind": "security", "status": "skipped — no security workload detected (use --always-copy to force)"}
        lines.append("Security → skipped (no workload)")
    elif s_skip == "retention":
        estimators["security"] = {"kind": "security", "status": "skipped — no diagnostic bulk to write a one-year estimate from"}
        lines.append("Security → skipped (no diagnostic bulk to write a one-year estimate from)")
    else:
        title_s = estimate_title(key, "security", suffix, s_scope)
        s_months = _months_for_sheet(s, args.force_months)
        copied, reused, layout = copy_verified(drive, sheets, args.security_id, title_s, folder["id"], reuse=not args.fresh, kind="security", tab=SEC_TAB)
        opts = region_options(sheets, copied["id"])
        reg, how = match_region(solution_provider(acc, "security", provider), s_scope or region, opts)
        if how == "none" or not reg:
            reason = _region_rejected(s_scope or region)
            try:
                trash_file(drive, copied["id"])
            except Exception:  # noqa: BLE001
                pass
            estimators["security"] = {
                "kind": "security", "title": title_s, "region": s_scope or region, "region_match": "none",
                "readback_rejected": reason, "readback": {}, "status": "unquoted",
            }
            lines.append(f"Security → {reason}")
        else:
            s_daily = float(s.get("siem_gb_day") if s.get("siem_gb_day") is not None else s.get("daily_raw_gb") or 0)
            cells, n, rb, s_gate = fill_security(sheets, copied["id"], acc, support, reg, s_months, s_daily, opts, basis)
            match_text = region_match_text(reg, how, s_scope or region)
            entry = {"kind": "security", "region": s_scope or region, "title": copied["name"], "spreadsheet_id": copied["id"], "url": copied["url"], "template_id": args.security_id, "reused_existing_copy": reused, "tab": SEC_TAB, "cells": cells, "cells_written": n, "region_match": match_text, "readback": rb, "status": "filled", "template_version": layout["version"], "template_newer_than_validated": layout["newer_than_validated"]}
            entry["ingest_basis"] = basis
            if s_gate is not None:
                entry["echo_gate"] = s_gate
                if not s_gate["accepted"]:
                    entry["readback_rejected"] = "the sheet echo differs from the written bulk by more than the tolerance (0.1% of it, at least 0.02 GB/day)"
            if not args.skip_xlsx:
                try:
                    x = export_xlsx(drive, copied["id"], estimate_dir / f"{title_s}.xlsx")
                    entry["local_xlsx"] = str(x)
                except Exception as ex:  # noqa: BLE001
                    entry["local_xlsx_error"] = str(ex)
            estimators["security"] = entry
            lines.append(f"Security → {copied['url']}  ({n} cells; Y1 {rb.get('annual_year1')}; region {reg} [{how}])")

    _checkpoint_sizing(local_dir, sizing_name, sz, estimators)

    # ---------------- Search (es3-estimator notebook library)
    se = con["search"]
    search_regions = architecture_regions(acc, "search")
    search_scope = fleet_sheet_region(acc, "search", region)
    if args.skip_search:
        estimators["search"] = (sz.get("estimators") or {}).get("search") or {"kind": "search", "status": "kept from the previous sizing"}
        lines.append("Search → kept from the previous sizing")
    elif search_scope is None:
        estimators["search"] = {"kind": "search", "status": "sized per region", "regions": search_regions}
        lines.append(f"Search → one sheet per region ({', '.join(search_regions)})")
    else:
        title_se = estimate_title(key, "search", suffix, search_scope)
        has_search = bool(se.get("has_workload") or (se.get("stored_gb") or 0) > 0 or args.always_copy)
        result: dict = {}
        store_scale = _search_volume_scale(suffix)
        if has_search and (se.get("stored_gb") or 0) > 0:
            result, published = price_search(
                drive, sheets, folder["id"], estimate_dir, title_se, {
                    "stored_gb": float(se.get("stored_gb") or 0) * store_scale,
                    "org_stored_gb": exact_inputs(se.get("workload") or acc.get("workload") or {}, search_scope or region, float(se.get("stored_gb") or 0) * store_scale, org_store_gb(acc) * store_scale)[1],
                    "provider": solution_provider(acc, "search", provider),
                    "region": search_scope or region,
                    "support_tier": support,
                    "workload": exact_inputs(se.get("workload") or acc.get("workload") or {}, search_scope or region, float(se.get("stored_gb") or 0) * store_scale, org_store_gb(acc) * store_scale)[0],
                }, notebook=args.search_notebook, template_id=args.search_id, reuse=not args.fresh, skip_xlsx=args.skip_xlsx,
            )
        elif has_search:
            result = {"status": "skipped — search slice has no stored GB", "assumptions": ["Nothing to price."]}
            published = {}
        else:
            result = {"status": "skipped — no search workload detected (use --always-copy to force)"}
            published = {}
        entry = {
            "kind": "search",
            "region": search_scope or region,
            "status": result.get("status") or "error",
            "title": title_se,
            "local": published.get("local"),
            "region_match": result.get("region_match"),
            "support_level": result.get("support_level"),
            "readback": result.get("readback"),
            "profiles": result.get("profiles"),
            "inputs": result.get("inputs"),
            "store_scale": store_scale,
            "assumptions": result.get("assumptions"),
            "source": result.get("source"),
            "url": published.get("url"),
            "spreadsheet_id": published.get("spreadsheet_id"),
            "search_path": "notebook" if args.search_notebook else "sheet",
            "template_id": None if args.search_notebook else args.search_id,
            "cells": result.get("cells"),
            "note": "performant is the quotable profile" if result.get("readback") else (result.get("error") or result.get("status")),
        }
        if result.get("error"):
            entry["error"] = result["error"]
            if "not on the estimator list" in str(result["error"]):
                entry["region_match"] = "none"
                entry["readback_rejected"] = str(result["error"])
                entry["readback"] = None
        if published.get("error"):
            entry["error"] = published["error"]
            entry["note"] = f"{entry.get('note')}; Search estimate was not published: {published['error']}"
        rb = result.get("readback") or {}
        lines.append(f"Search → {entry.get('url') or entry.get('local')}  (performant {rb.get('monthly_steady_state') or entry['status']}; region {result.get('region_match')})")
        estimators["search"] = entry
    _checkpoint_sizing(local_dir, sizing_name, sz, estimators)
    project_only = {"observability", "security"} if args.skip_search else None
    fresh_projects = size_regional_projects(
        acc, sheets, drive, folder, o11y_id=args.o11y_id, security_id=args.security_id,
        support=support, provider=provider, skip_xlsx=args.skip_xlsx, fresh=args.fresh,
        local_dir=estimate_dir, customer=meta["customer"], when=meta["date"], name_suffix=suffix,
        only=project_only, force_months=args.force_months,
    )
    if args.skip_search:
        kept = [p for p in ((sz.get("estimators") or {}).get("projects") or []) if p.get("solution") == "search" and p.get("role") != "measured_retention"]
        estimators["projects"] = fresh_projects + kept
    else:
        estimators["projects"] = fresh_projects
    prefer_regional_estimators(estimators)

    sz["folder"] = str(local_dir)
    sz["estimate_folder"] = str(estimate_dir)
    sz["drive_root"] = root
    sz["drive_folder"] = folder
    sz["estimators"] = estimators
    sz["sized_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    sz["support_tier"] = support
    acc["sizing"] = sz
    seal_text(acc_path, json.dumps(acc, indent=2))
    seal_text(local_dir / sizing_name, json.dumps(sz, indent=2))
    lines.append(f"Updated {acc_path.name} + {sizing_name}")
    print("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except TemplateMismatch as mismatch:
        print(f"ERROR: {mismatch}", file=sys.stderr)
        sys.exit(TEMPLATE_MISMATCH_EXIT)
