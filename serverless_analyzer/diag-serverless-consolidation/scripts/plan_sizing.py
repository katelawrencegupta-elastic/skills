"""Regional jobs and the deployment-count plan."""
from __future__ import annotations

import re
from pathlib import Path

from serverless_regions import deployment_providers, group_provider
from sizing_shared import (
    O11Y_READBACK, O11Y_TAB, SEC_READBACK, SEC_TAB, TemplateMismatch, e73_blocks_quote, echo_gate,
    observability_echo_gate, observability_readback, sheet_money,
)
from sheets_io import (
    copy_verified, export_xlsx, load_sheet_creds, map_in_order, push_cells, read_cells,
    services_from, trash_file, write_search_sheet,
)
from search_sheet import price_search
from estimate_paths import _months_for_sheet, _search_volume_scale, estimate_title
from sizing_fill import (
    _region_rejected, fill_security, ingest_basis, match_region, observability_fill,
    region_match_text, region_options, run_es3,
)

def _fill_regional_job(job: dict, creds) -> dict:
    """One regional project. The worker builds its own Sheets and Drive clients."""
    sheets, drive = services_from(creds)
    sol = job["solution"]
    if sol == "search":
        import sizing_shared
        result, published = price_search(
            drive, sheets, job["parent_id"], Path(job["local_dir"]), job["title"], {
                "stored_gb": job["stored_gb"],
                "org_stored_gb": job["org_gb"],
                "provider": job["provider"],
                "region": job["region"],
                "support_tier": job["support"],
                "workload": job["workload"],
            }, notebook=sizing_shared._SEARCH_NOTEBOOK, template_id=sizing_shared._SEARCH_TEMPLATE_ID,
            reuse=job["reuse"], skip_xlsx=job.get("skip_xlsx", True),
        )
        entry = {
            "solution": sol, "region": job["region"], "title": job["title"], "stored_gb": job["stored_gb"],
            "store_scale": job.get("store_scale"),
            "status": result.get("status") or "error",
            "region_match": result.get("region_match"),
            "readback": result.get("readback"),
            "profiles": result.get("profiles"),
            "local": published.get("local"),
            "url": published.get("url"),
            "spreadsheet_id": published.get("spreadsheet_id"),
        }
        if result.get("error"):
            entry["error"] = result["error"]
            if "not on the estimator list" in str(result["error"]):
                entry["region_match"] = "none"
                entry["readback_rejected"] = str(result["error"])
                entry["readback"] = None
        if published.get("error"):
            entry["error"] = published["error"]
        if job.get("role"):
            entry["role"] = job["role"]
            entry["retention_months"] = job.get("retention")
        return entry
    copied, reused, layout = copy_verified(
        drive, sheets, job["template_id"], job["title"], job["parent_id"], reuse=job["reuse"], kind=sol, tab=job["tab"],
    )
    opts = region_options(sheets, copied["id"])
    reg, how = match_region(job["provider"], job["region"], opts)
    if how == "none" or not reg:
        try:
            trash_file(drive, copied["id"])
        except Exception:  # noqa: BLE001
            pass
        return {
            "solution": sol, "region": job["region"], "title": job["title"],
            "region_match": "none", "readback_rejected": _region_rejected(job.get("region")),
            "readback": {}, "status": "unquoted", "error": _region_rejected(job.get("region")),
        }
    match_text = region_match_text(reg, how, job.get("region"))
    basis = job.get("basis") or "raw"
    account = {"sizing": {"feature_tier": job.get("feature_tier")}}
    gate = None
    if sol == "observability":
        cells = observability_fill(account, job["support"], reg, int(job["retention"]), job["daily"], job.get("mix"), bool(job.get("forced")), basis)
        n = push_cells(sheets, copied["id"], job["tab"], cells, allowed_text=set(opts))
        rb = observability_readback(read_cells(sheets, copied["id"], job["tab"], job["readback"]), basis)
        gate = observability_echo_gate(job.get("mix"), rb) if basis == "metered" else echo_gate(job["daily"], rb.get("echo_gb_day"))
    else:
        cells, n, rb, gate = fill_security(sheets, copied["id"], account, job["support"], reg, int(job["retention"]), job["daily"], opts, basis)
    entry = {
        "solution": sol, "region": job["region"], "title": copied["name"], "stored_gb": job["stored_gb"],
        "spreadsheet_id": copied["id"], "url": copied["url"], "reused_existing_copy": reused,
        "cells": cells, "cells_written": n, "region_match": match_text,
        "readback": rb, "status": "filled", "daily_raw_gb": round(job["daily"], 2),
        "template_version": layout["version"], "template_newer_than_validated": layout["newer_than_validated"],
    }
    entry["ingest_basis"] = basis
    if job.get("role"):
        entry["role"] = job["role"]
        entry["retention_months"] = job.get("retention")
    if gate is not None:
        entry["echo_gate"] = gate
        if not gate["accepted"]:
            entry["readback_rejected"] = "the sheet echo differs from the written bulk by more than the tolerance (0.1% of it, at least 0.02 GB/day)"
    if not job["skip_xlsx"]:
        try:
            entry["local_xlsx"] = str(export_xlsx(drive, copied["id"], Path(job["local_dir"]) / f"{job['title']}.xlsx"))
        except Exception as ex:  # noqa: BLE001
            entry["local_xlsx_error"] = str(ex)
    return entry


def size_regional_projects(acc: dict, sheets, drive, folder: dict, *, o11y_id: str, security_id: str, support: str, provider, skip_xlsx: bool, fresh: bool, local_dir: Path, customer: str, when: str, only: set[str] | None = None, name_suffix: str = "", force_months: int | None = None) -> list[dict]:
    """One estimator copy per serverless project when a solution spans regions.

    Sheet copies run on the shared worker pool. `sheets` and `drive` stay on the
    signature for callers that already opened a client; each copy builds its own.
    """
    del sheets, drive, customer, when
    groups = ((acc.get("consolidation") or {}).get("architecture") or {}).get("groups") or []
    by_sol: dict[str, list] = {}
    for g in groups:
        by_sol.setdefault(g["solution"], []).append(g)
    con = (acc.get("sizing") or {}).get("consolidated") or {}
    org_gb = org_store_gb(acc)
    search_slice = con.get("search") or {}
    workload = search_slice.get("workload") or acc.get("workload") or {}
    key = acc["meta"]["run_key"]
    feature_tier = (acc.get("sizing") or {}).get("feature_tier")
    jobs: list[dict] = []
    for sol, rows in by_sol.items():
        if only is not None and sol not in only:
            continue
        if len(rows) < 2:
            continue
        for g in rows:
            gb = float(g.get("stored_gb") or 0)
            if sol == "search" and gb <= 0:
                continue
            if gb <= 0 and not (g.get("daily_raw_gb") or 0):
                continue
            source_months = g.get("retention_months") if g.get("retention_months") is not None else (con.get(sol) or {}).get("retention_months")
            retention = _months_for_sheet({"retention_months": source_months}, force_months)
            try:
                region_daily = float(g.get("daily_raw_gb") or 0)
            except (TypeError, ValueError):
                region_daily = 0.0
            if sol != "search" and region_daily <= 0:
                continue
            if sol == "security":
                if retention is None:
                    continue
            elif sol != "search" and retention is None:
                continue
            daily = region_daily if sol != "search" else 0.0
            region = g["region"]
            title = estimate_title(key, sol, name_suffix, region)
            job = {
                "solution": sol,
                "region": region,
                "title": title,
                "stored_gb": gb,
                "parent_id": folder["id"],
                "local_dir": str(local_dir),
                "provider": group_provider(acc, g.get("deployments"), provider),
                "support": support,
                "feature_tier": feature_tier,
                "skip_xlsx": skip_xlsx,
                "reuse": not fresh,
                "basis": ingest_basis(acc),
            }
            if sol == "search":
                scale = _search_volume_scale(name_suffix)
                job["stored_gb"] = gb * scale
                job["org_gb"] = org_gb * scale
                job["store_scale"] = scale
                job["workload"] = workload
            elif sol == "observability":
                if g.get("mix_error"):
                    continue
                job.update({
                    "template_id": o11y_id, "tab": O11Y_TAB, "readback": O11Y_READBACK,
                    "retention": int(retention), "daily": daily,
                    "mix": g.get("mix"), "forced": force_months is not None,
                })
            elif sol == "security":
                job.update({
                    "template_id": security_id, "tab": SEC_TAB, "readback": SEC_READBACK,
                    "retention": int(retention), "daily": daily,
                })
            else:
                continue
            jobs.append(job)
    if not jobs:
        return []
    creds = load_sheet_creds()
    return map_in_order(lambda job: _fill_regional_job(job, creds), jobs)


def _observability_mix(rows: list[dict], bulk_key: str) -> dict:
    from diag_account import observability_mix
    return observability_mix(rows, bulk_key)


def _diag_inputs():
    import diag_inputs
    return diag_inputs


def assemble_plan_bars(specs: list[dict], fills: list[dict]) -> dict:
    """Join sheet read-backs onto plan bars. Input order is the cut order.

    A group that carries an error makes that bar unpriced. It is never added as $0.
    Later bars stay priced, and the bar records the gap. The 100% bar reuses the headline.
    """
    bars = []
    for spec in specs:
        if spec.get("reused_headline"):
            bars.append({
                "cut_pct": spec["cut_pct"],
                "label": spec["label"],
                "projects": spec.get("projects", 1),
                "from_projects": spec.get("from_projects"),
                "monthly": None,
                "reused_headline": True,
            })
            continue
        monthly = 0.0
        priced = []
        gap = False
        bar_error = None
        for group in spec.get("groups") or []:
            names = group.get("names") or []
            if group.get("error"):
                gap = True
                bar_error = str(group.get("error"))
                priced.append({
                    "names": names,
                    "monthly": None,
                    "unpriced_reason": bar_error,
                    "bulk_gb_day": group.get("bulk_gb_day") or 0,
                    "months_written": group.get("months_written"),
                })
                continue
            if group.get("sheet") is None:
                if group.get("monthly") is None:
                    gap = True
                    priced.append({
                        "names": names,
                        "monthly": None,
                        "unpriced_reason": group.get("unpriced_reason") or "unpriced",
                        "bulk_gb_day": group.get("bulk_gb_day") or 0,
                        "months_written": group.get("months_written"),
                    })
                    continue
                money = float(group.get("monthly") or 0)
                monthly += money
                priced.append({
                    "names": names,
                    "monthly": round(money, 2),
                    "bulk_gb_day": group.get("bulk_gb_day") or 0,
                    "months_written": group.get("months_written"),
                })
                continue
            fill = fills[group["sheet"]] if group["sheet"] < len(fills) else {}
            if fill.get("error") or fill.get("monthly") is None:
                gap = True
                reason = str(fill.get("error") or fill.get("unpriced_reason") or "unpriced")
                bar_error = bar_error or reason
                priced.append({
                    "names": names,
                    "monthly": None,
                    "unpriced_reason": reason,
                    "bulk_gb_day": group.get("bulk_gb_day"),
                    "months_written": group.get("months_written"),
                    "url": fill.get("url"),
                })
                continue
            monthly += float(fill["monthly"])
            priced.append({
                "names": names,
                "monthly": round(float(fill["monthly"]), 2),
                "bulk_gb_day": group.get("bulk_gb_day"),
                "months_written": group.get("months_written"),
                "url": fill.get("url"),
            })
        row = {
            "cut_pct": spec["cut_pct"],
            "label": spec["label"],
            "projects": spec["projects"],
            "from_projects": spec.get("from_projects"),
            "groups": priced,
        }
        if bar_error:
            row["monthly"] = None
            row["unpriced_reason"] = bar_error
            row["gap"] = True
        else:
            row["monthly"] = round(monthly, 2)
            if gap:
                row["gap"] = True
        bars.append(row)
    return {"bars": bars}


def _search_group_price(group: list[dict], *, workload: dict, org_gb: float, support: str, cache: dict) -> tuple[float | None, str | None]:
    """Performant Search price for one project, on its measured store. A missing notebook total is an error."""
    stored = sum(float(row.get("store_gb") or 0) for row in group)
    region = str(group[0].get("region") or "")
    provider = str(group[0].get("provider") or "")
    if stored <= 0:
        return 0.0, None
    org_store = float(org_gb or 0)
    key = (provider, region, round(stored, 3), round(org_store, 3), support)
    if key in cache:
        return cache[key], None
    result = run_es3({
        "stored_gb": stored,
        "org_stored_gb": org_store or stored,
        "provider": provider,
        "region": region,
        "support_tier": support,
        "workload": workload or {},
    })
    money = ((result.get("readback") or {}).get("monthly_steady_state"))
    if money is None:
        where = region or "the project"
        return None, f"Deployment-count plan was not priced: Search in {where} returned no performant total."
    cache[key] = round(float(money), 2)
    return cache[key], None


def plan_sheet_jobs(rows: list[dict], *, run_key: str, name_suffix: str, force_months: int | None, o11y_id: str, security_id: str, parent_id: str, reuse: bool, provider, region_name, support: str, feature_tier, workload: dict | None = None, org_gb: float = 0, basis: str = "raw") -> tuple[list[dict], list[dict], str | None, int]:
    """Bar specs plus the sheet jobs they point at.

    Observability projects read D38. Security projects read E74. Search projects
    are the performant notebook. A missing month or a missing Search total fails
    before the remaining copies are treated as a quote.
    """
    diag = _diag_inputs()
    specs: list[dict] = []
    jobs: list[dict] = []
    shared: dict[int, list] = {}
    search_cache: dict = {}
    floor = diag.plan_floor_for_rows(rows)
    for plan in diag.region_feature_plans(rows):
        pct = plan["cut_pct"]
        if pct <= 0 and plan["projects"] <= 1:
            specs.append({"cut_pct": 0, "label": "Ceiling", "projects": 1, "reused_headline": True, "groups": []})
            continue
        packed = shared.get(plan["projects"])
        if packed is not None:
            specs.append({
                "cut_pct": pct,
                "label": plan["label"],
                "projects": plan["projects"],
                "from_projects": plan.get("from_projects"),
                "groups": packed,
            })
            continue
        groups = []
        for index, group in enumerate(plan["groups"], start=1):
            try:
                summary = diag.summarize(group, force_months, basis=basis)
            except ValueError as exc:
                # A missing Observability mix leaves that project unquoted. The other projects still price.
                groups.append({
                    "sheet": None,
                    "names": [str(row.get("name")) for row in group if row.get("name")],
                    "bulk_gb_day": 0,
                    "error": str(exc),
                })
                continue
            solution = str((group[0] if group else {}).get("solution") or "observability")
            if solution == "search":
                money, err = _search_group_price(
                    group,
                    workload=workload or {},
                    org_gb=float(org_gb or 0),
                    support=support,
                    cache=search_cache,
                )
                if err:
                    return [], [], err, floor
                groups.append({
                    "sheet": None,
                    "names": summary.get("names") or [],
                    "bulk_gb_day": summary.get("bulk_gb_day") or 0,
                    "months_written": summary.get("months_written"),
                    "monthly": money,
                })
                continue
            if solution not in {"observability", "security"}:
                solution = "observability"
            bulk = float(summary.get("bulk_gb_day") or 0)
            if bulk <= 0:
                groups.append({"sheet": None, "names": summary.get("names") or [], "bulk_gb_day": 0})
                continue
            written = force_months or summary.get("months_written")
            if not written:
                label = "Security" if solution == "security" else "Observability"
                return [], [], f"{label} deployment-count plan was not priced: a project has bulk and no measured retention.", floor
            group_region = str((group[0] if group else {}).get("region") or region_name or "")
            group_provider = str((group[0] if group else {}).get("provider") or provider or "")
            slug = re.sub(r"[\\/:\s]+", "-", group_region.strip()).strip("-")
            kind = "Security" if solution == "security" else "Observability"
            title = f"{run_key}_{kind}_plan_{pct}pct_{plan['projects']}projects_{index}"
            if slug:
                title = f"{title}_{slug}"
            title = f"{title}{name_suffix}"
            groups.append({
                "sheet": len(jobs),
                "names": summary.get("names") or [],
                "bulk_gb_day": bulk,
                "months_written": int(written),
            })
            jobs.append({
                "solution": solution,
                "template_id": security_id if solution == "security" else o11y_id,
                "title": title,
                "parent_id": parent_id,
                "reuse": reuse,
                "provider": group_provider,
                "region_name": group_region,
                "support": support,
                "feature_tier": feature_tier,
                "months_written": int(written),
                "bulk": bulk,
                "mix": _observability_mix(group, "bulk_gb_day") if solution == "observability" else None,
                "forced": force_months is not None,
                "basis": basis,
            })
        shared[plan["projects"]] = groups
        specs.append({
            "cut_pct": pct,
            "label": plan["label"],
            "projects": plan["projects"],
            "from_projects": plan.get("from_projects"),
            "groups": groups,
        })
    return specs, jobs, None, floor


def fill_plan_sheet(job: dict, creds) -> dict:
    """Copy one plan workbook and read its quote cell. A 429 or 503 is retried inside the request.

    Observability reads D38 after the echo gate. Security reads E74 after E73 is 0.
    """
    sheets, drive = services_from(creds)
    solution = job.get("solution") or "observability"
    label = "Security" if solution == "security" else "Observability"
    try:
        copied, _reused, _layout = copy_verified(
            drive, sheets, job["template_id"], job["title"], job["parent_id"], reuse=job["reuse"],
            kind=solution, tab=SEC_TAB if solution == "security" else O11Y_TAB,
        )
        opts = region_options(sheets, copied["id"])
        reg, how = match_region(job["provider"], job["region_name"], opts)
        if how == "none" or not reg:
            return {"error": _region_rejected(job.get("region_name")), "unpriced_reason": _region_rejected(job.get("region_name"))}
        account = {"sizing": {"feature_tier": job.get("feature_tier")}}
        basis = job.get("basis") or "raw"
        if solution == "security":
            _cells, _n, rb, gate = fill_security(sheets, copied["id"], account, job["support"], reg, int(job["months_written"]), float(job["bulk"]), opts, basis)
            if gate is not None and not gate["accepted"]:
                return {"error": (
                    "Security deployment-count plan was not priced: "
                    f"{job['title']} echoes {gate['sheet_gb_day']} GB/day against {gate['written_bulk']} written."
                )}
            if e73_blocks_quote(rb.get("cps_addon_monthly")):
                addon = sheet_money(rb.get("cps_addon_monthly"))
                reason = "E73 was blank" if addon is None else (rb.get("addon_note") or f"E73 is {addon:g}")
                return {"error": f"Security deployment-count plan was not priced: {job['title']} {reason}."}
            price = sheet_money(rb.get("monthly_steady_state"))
            if price is None:
                return {"error": f"Security deployment-count plan was not priced: {job['title']} has no E74 read-back."}
            return {"monthly": round(price, 2), "url": copied.get("url")}
        cells = observability_fill(
            account, job["support"], reg, int(job["months_written"]), float(job["bulk"]),
            job.get("mix"), bool(job.get("forced")), basis,
        )
        push_cells(sheets, copied["id"], O11Y_TAB, cells, allowed_text=set(opts))
        rb = observability_readback(read_cells(sheets, copied["id"], O11Y_TAB, O11Y_READBACK), basis)
        gate = observability_echo_gate(job.get("mix"), rb) if basis == "metered" else echo_gate(float(job["bulk"]), rb.get("echo_gb_day"))
        if not gate["accepted"]:
            return {"error": (
                "Observability deployment-count plan was not priced: "
                f"{job['title']} echoes {gate['sheet_gb_day']} GB/day against {gate['written_bulk']} written."
            )}
        price = sheet_money(rb.get("monthly_steady_state"))
        if price is None:
            return {"error": f"Observability deployment-count plan was not priced: {job['title']} has no D38 read-back."}
        return {"monthly": round(price, 2), "url": copied.get("url")}
    except TemplateMismatch:
        raise
    except Exception as ex:  # noqa: BLE001
        return {"error": f"{label} deployment-count plan was not priced: {ex}"}


def _plan_bulk_mix(rec: dict) -> dict | None:
    """The four product streams `diag_inputs.summarize` reads, from the deployment's GB/day fields."""
    if isinstance(rec.get("bulk_mix"), dict):
        return rec.get("bulk_mix")
    fields = {
        "logs": rec.get("logs_gb_day"),
        "k8s_logs": rec.get("k8s_logs_gb_day"),
        "metrics": rec.get("metrics_gb_day"),
        "traces": rec.get("traces_gb_day"),
    }
    if not any(value is not None for value in fields.values()):
        return None
    return {key: float(value or 0) for key, value in fields.items()}


def _plan_solution(cloud_row: dict, measured: dict) -> str:
    known = {"observability", "security", "search"}
    sol = measured.get("solution")
    if sol in known:
        return sol
    use = str(cloud_row.get("use_case") or "").lower()
    if use in known:
        return use
    template = str(cloud_row.get("template") or "").lower()
    if any(token in template for token in ("vector", "storage", "search")):
        return "search"
    return "search"


def plan_roster(acc: dict) -> list[dict]:
    """Deployments on the cover. The ceiling is one project for each of them.

    That list is the quote window, the last 90 billed days unless --lookback-days
    says otherwise. A deployment with no bill in that window stays off the ceiling.
    """
    measured = [
        dep for dep in (acc.get("solutions") or {}).get("by_deployment") or []
        if dep.get("platform") != "serverless" and dep.get("ingest_source") not in ("inactive", "unsupported_region")
    ]
    cloud_rows = [
        dep for dep in ((acc.get("cloud") or {}).get("by_deployment") or [])
        if dep.get("platform") != "serverless"
    ]
    cloud_by_name = {dep.get("name"): dep for dep in cloud_rows}
    if measured:
        return [cloud_by_name.get(dep.get("name")) or dep for dep in measured]
    names = set(((acc.get("deployments") or {}).get("active") or {}).get("names") or [])
    if names:
        picked = [dep for dep in cloud_rows if dep.get("name") in names]
        if picked:
            return picked
    return cloud_rows


def size_deployment_plan(acc: dict, sheets, drive, folder: dict, *, o11y_id: str, security_id: str, support: str, provider, force_months: int | None, name_suffix: str, fresh: bool) -> dict:
    """Price the cuts from the cover count down to one project per region and feature.

    Observability projects are D38 read-backs. Security projects are E74 read-backs.
    Search projects are the performant estimator on their measured store.
    """
    del sheets, drive
    measured = {
        dep.get("name"): dep
        for dep in (acc.get("solutions") or {}).get("by_deployment") or []
        if dep.get("platform") != "serverless"
    }
    diagnostic_basis = ingest_basis(acc) == "metered"
    providers = deployment_providers(acc)
    rows = []
    for dep in plan_roster(acc):
        measured_rec = measured.get(dep.get("name"))
        rec = measured_rec or dep
        if diagnostic_basis and measured_rec is None:
            store_gb = 0
        else:
            store_gb = rec.get("gb")
            if store_gb is None:
                store_gb = 0 if diagnostic_basis else dep.get("gb") or 0
        rows.append({
            "name": dep.get("name") or "",
            "solution": _plan_solution(dep, rec),
            "region": dep.get("region") or rec.get("region") or "",
            "provider": providers.get(str(dep.get("name"))) or dep.get("provider") or provider,
            "bulk_gb_day": 0 if diagnostic_basis and measured_rec is None else rec.get("daily_raw_gb") or 0,
            "store_gb": store_gb,
            "retention_days": rec.get("retention_days"),
            "retention_days_metrics": rec.get("retention_days_metrics"),
            "bulk_mix": _plan_bulk_mix(rec),
            **{key: rec.get(key) for key in ("logs_gb_day", "k8s_logs_gb_day", "metrics_gb_day", "traces_gb_day") if rec.get(key) is not None},
        })
    if not rows:
        return {"bars": [], "error": "Deployment-count plan was not priced: no ECH deployments."}
    org_gb = org_store_gb(acc)
    specs, jobs, error, floor = plan_sheet_jobs(
        rows,
        run_key=acc["meta"]["run_key"],
        name_suffix=name_suffix,
        force_months=force_months,
        o11y_id=o11y_id,
        security_id=security_id,
        parent_id=folder["id"],
        reuse=not fresh,
        provider=provider,
        region_name=(acc.get("sizing") or {}).get("region"),
        support=support,
        feature_tier=(acc.get("sizing") or {}).get("feature_tier"),
        basis=ingest_basis(acc),
        workload=acc.get("workload") or {},
        org_gb=org_gb,
    )
    if error:
        return {"bars": [], "error": error}
    if not jobs:
        built = assemble_plan_bars(specs, [])
    else:
        try:
            creds = load_sheet_creds()
            fills = map_in_order(lambda job: fill_plan_sheet(job, creds), jobs)
        except TemplateMismatch:
            raise
        except Exception as ex:  # noqa: BLE001
            return {"bars": [], "error": f"Deployment-count plan was not priced: {ex}"}
        built = assemble_plan_bars(specs, fills)
    if not built.get("error"):
        built["floor"] = floor
    return built


def org_store_gb(acc: dict) -> float:
    """Org store behind the Search telemetry share.

    A diagnostic quote's ingest_share_store_gb is the sum of matched diagnostic
    local stores. A K360 backup with no diagnostic share uses the account total.
    """
    st = acc.get("storage") or {}
    return float(st.get("ingest_share_store_gb") or st.get("data_stored_gb") or 0)


