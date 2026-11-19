"""Estimate folder names and where a sheet is filed."""
from __future__ import annotations

import re
from pathlib import Path

from sizing_shared import (
    COMPRESSION_FOLDER_NAME, COMPRESSION_NAME_MARK, ESTIMATE_FOLDER_NAME, ESTIMATE_NAME_MARK,
    FOLDER_MIME, LEGACY_ESTIMATE_DIRS, PLAN_FOLDER_NAME, seal_dir, seal_file,
)
from sheets_io import (
    _execute, assert_not_template, ensure_drive_folder, find_in_folder, require_customer_parent, trash_file,
)

def _suffix(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    return text if text.startswith("_") else f"_{text}"


def _estimate_parts(name_suffix: str | None) -> tuple[str, str]:
    """Retention label and basis for one sizing. Basis is bulk, compression-applied, or k360."""
    suffix = _suffix(name_suffix).lower()
    if suffix.endswith("_30d"):
        retention = "30d"
        basis_src = suffix[: -len("_30d")]
    else:
        retention = "full retention"
        basis_src = suffix
    if "compression" in basis_src:
        basis = "compression-applied"
    elif "k360" in basis_src:
        basis = "k360"
    else:
        basis = "bulk"
    return retention, basis


def estimate_folder_name(name_suffix: str | None) -> str:
    """Directory that holds the estimate sheets. Plan sheets are in a folder under it."""
    _estimate_parts(name_suffix)
    return ESTIMATE_FOLDER_NAME


def estimate_dir_parts(name_suffix: str | None) -> tuple[str, ...]:
    """Path under generated_estimates for this sizing.

    Bulk and k360 sheets sit directly in generated_estimates. Sheets from the
    retired compression-applied set stay in compression_applied.
    """
    _retention, basis = _estimate_parts(name_suffix)
    if basis == "compression-applied":
        return (COMPRESSION_FOLDER_NAME,)
    return ()


def estimate_subfolder(name_suffix: str | None) -> str:
    """Relative directory under generated_estimates for this sizing."""
    return "/".join(estimate_dir_parts(name_suffix))


def is_compression_estimate_name(name: str) -> bool:
    return COMPRESSION_NAME_MARK in (name or "")


def is_estimate_artifact_name(name: str) -> bool:
    return ESTIMATE_NAME_MARK in (name or "")


def estimate_dest_parts_for_filename(name: str) -> tuple[str, ...] | None:
    """Where an existing estimate file belongs. Plan sheets and other files stay put."""
    if not is_estimate_artifact_name(name):
        return None
    if is_compression_estimate_name(name):
        return (COMPRESSION_FOLDER_NAME,)
    return ()


def relocate_local_estimates(parent: Path) -> list[str]:
    """Move estimate files out of the old retention folders, up into generated_estimates.

    When the same file name is already there, the newer copy is kept. A retention
    folder is removed once it is empty; one that still holds anything else stays.
    """
    moved: list[str] = []
    files = [
        item for item in parent.rglob("*")
        if item.is_file() and PLAN_FOLDER_NAME not in item.relative_to(parent).parts
    ]
    for item in files:
        parts = estimate_dest_parts_for_filename(item.name)
        if parts is None:
            continue
        dest_dir = parent.joinpath(*parts)
        if item.parent == dest_dir:
            continue
        seal_dir(dest_dir)
        target = dest_dir / item.name
        if target.is_file() and target.stat().st_mtime > item.stat().st_mtime:
            item.unlink()
            continue
        item.replace(target)
        seal_file(target)
        moved.append(item.name)
    for parts in LEGACY_ESTIMATE_DIRS:
        legacy = parent.joinpath(*parts)
        if not legacy.is_dir() or legacy.is_symlink():
            continue
        if any(child.name != ".DS_Store" for child in legacy.iterdir()):
            continue
        (legacy / ".DS_Store").unlink(missing_ok=True)
        try:
            legacy.rmdir()
        except OSError:
            pass
    return moved


def drive_children(drive, parent_id: str) -> list[dict]:
    """Every live child of a Drive folder, files and folders."""
    parent_id = require_customer_parent(parent_id)
    found: list[dict] = []
    token = None
    while True:
        def _list(page=token):
            kwargs = {
                "q": f"'{parent_id}' in parents and trashed = false",
                "spaces": "drive",
                "fields": "nextPageToken, files(id,name,mimeType)",
                "pageSize": 100,
            }
            if page:
                kwargs["pageToken"] = page
            return drive.files().list(**kwargs)
        page = _execute(_list)
        found.extend(page.get("files") or [])
        token = page.get("nextPageToken")
        if not token:
            return found


def _drive_child_files(drive, parent_id: str) -> list[dict]:
    return [item for item in drive_children(drive, parent_id) if item.get("mimeType") != FOLDER_MIME]


def _drive_folder_id(drive, estimates_id: str, parts: tuple[str, ...]) -> str | None:
    current = estimates_id
    for name in parts:
        found = find_in_folder(drive, current, name)
        if not found or found.get("mimeType") != FOLDER_MIME:
            return None
        current = found["id"]
    return current


def relocate_drive_estimates(drive, estimates_id: str) -> list[str]:
    """Move estimate files out of the old retention folders, up into generated_estimates.

    A retention folder is trashed once it is empty; one that still holds anything
    else stays. Two sheets that end up with one title are settled by the
    one-estimate-per-title rule when that title is next written, and by the dedupe pass.
    """
    estimates_id = require_customer_parent(estimates_id)
    candidates = [(), (COMPRESSION_FOLDER_NAME,), *LEGACY_ESTIMATE_DIRS]
    planned: list[tuple[dict, str, tuple[str, ...]]] = []
    for parts in candidates:
        source_id = estimates_id if not parts else _drive_folder_id(drive, estimates_id, parts)
        if not source_id:
            continue
        for item in _drive_child_files(drive, source_id):
            dest = estimate_dest_parts_for_filename(item.get("name") or "")
            if dest is None or dest == parts:
                continue
            planned.append((item, source_id, dest))
    dest_ids: dict[tuple[str, ...], str] = {}
    moved: list[str] = []
    for item, source_id, dest in planned:
        if dest not in dest_ids:
            current = estimates_id
            for name in dest:
                current = ensure_drive_folder(drive, name, current)["id"]
            dest_ids[dest] = current
        assert_not_template(item["id"])
        file_id = item["id"]
        dest_id = dest_ids[dest]
        _execute(lambda fid=file_id, src=source_id, dst=dest_id: drive.files().update(
            fileId=fid,
            addParents=dst,
            removeParents=src,
            fields="id,parents",
        ))
        moved.append(item.get("name") or file_id)
    # Deepest first, so compression_applied/30d goes before a check of its parent would matter.
    for parts in sorted(LEGACY_ESTIMATE_DIRS, key=len, reverse=True):
        legacy_id = _drive_folder_id(drive, estimates_id, parts)
        if legacy_id and not drive_children(drive, legacy_id):
            trash_file(drive, legacy_id)
    return moved


def open_estimate_place(drive, root_id: str, local_dir: Path, name_suffix: str | None) -> tuple[dict, Path, dict, list[str]]:
    """Drive folder and local directory for this sizing, plus the generated_estimates Drive folder.

    Sheets land directly in generated_estimates. Files an earlier run left in
    full_retention or 30d are moved up to match, and those folders are removed.
    """
    parent = ensure_drive_folder(drive, ESTIMATE_FOLDER_NAME, root_id)
    local_parent = local_dir / ESTIMATE_FOLDER_NAME
    seal_dir(local_parent)
    local_moved = relocate_local_estimates(local_parent)
    drive_moved = relocate_drive_estimates(drive, parent["id"])
    folder = parent
    for name in estimate_dir_parts(name_suffix):
        folder = ensure_drive_folder(drive, name, folder["id"])
    working_local = local_parent.joinpath(*estimate_dir_parts(name_suffix))
    seal_dir(working_local)
    return folder, working_local, parent, local_moved + drive_moved


def _deployment_region_rows(acc: dict) -> list[dict]:
    """Solution and region pairs from the account when architecture groups are not written yet."""
    region_by_name = {
        dep.get("name"): dep.get("region")
        for dep in ((acc.get("cloud") or {}).get("by_deployment") or [])
        if dep.get("name") and dep.get("region")
    }
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for dep in (acc.get("solutions") or {}).get("by_deployment") or []:
        if dep.get("platform") == "serverless" or dep.get("ingest_source") in ("inactive", "unsupported_region"):
            continue
        solution = dep.get("solution") or "search"
        region = str(region_by_name.get(dep.get("name")) or "").strip()
        key = (solution, region)
        if not region or key in seen:
            continue
        seen.add(key)
        rows.append({"solution": solution, "region": region})
    return rows


def architecture_regions(acc: dict, solution: str) -> list[str]:
    """Deployment regions for one solution, in first-seen order.

    Architecture groups are the quote's grouping. Before that consolidation
    exists, the cloud and solution rows carry the same regions. A fleet-wide
    sheet must not be written in that window and then reused as one region's file.
    """
    groups = ((acc.get("consolidation") or {}).get("architecture") or {}).get("groups") or []
    if not groups:
        groups = _deployment_region_rows(acc)
    out: list[str] = []
    for group in groups:
        if group.get("solution") != solution:
            continue
        region = str(group.get("region") or "").strip()
        if region and region not in out:
            out.append(region)
    return out


def prefer_regional_estimators(estimators: dict) -> None:
    """Drop a fleet-wide read-back once that solution has a sheet per region.

    The fleet sheet is priced as the account region. Keeping it beside the
    regional sheet for that same region records the solution twice.
    """
    projects = estimators.get("projects") or []
    for solution in ("observability", "security", "search"):
        regions: list[str] = []
        for project in projects:
            if project.get("solution") != solution:
                continue
            region = str(project.get("region") or "").strip()
            if region and region not in regions:
                regions.append(region)
        if len(regions) >= 2:
            estimators[solution] = {"kind": solution, "status": "sized per region", "regions": regions}


def fleet_sheet_region(acc: dict, solution: str, fallback: str | None) -> str | None:
    """Region for the one fleet-wide sheet.

    None when that solution already has one sheet per region. A second fleet-wide
    sheet would price the whole solution again, under the account region.
    """
    regions = architecture_regions(acc, solution)
    if len(regions) >= 2:
        return None
    if len(regions) == 1:
        return regions[0]
    text = str(fallback or "").strip()
    return text or None


def estimate_title(run_key: str, solution: str, name_suffix: str | None = "", region: str | None = None) -> str:
    """Google Sheet title for one serverless estimate.

    <customer>_<date>_<solution>_full retention_bulk_<region>_serverless_estimate
    The region is the cloud region the sheet was scoped to.
    A K360 backup uses k360 in the basis slot so it does not replace the bulk sheet.
    """
    retention, basis = _estimate_parts(name_suffix)
    name = f"{run_key}_{(solution or '').strip().lower()}_{retention}_{basis}"
    if region:
        safe = re.sub(r"[\\/:\s]+", "-", str(region).strip()).strip("-")
        if safe:
            name = f"{name}_{safe}"
    return f"{name}_serverless_estimate"


def _sheet_retention(months) -> int | None:
    """Estimator retention rows are the integers 1–36. A missing month is not invented.

    Rounding is `diag_inputs.whole_months`, the one shared rule.
    """
    if months is None or months == "":
        return None
    try:
        value = float(months)
    except (TypeError, ValueError):
        return None
    from diag_inputs import whole_months
    return whole_months(value)


def _months_for_sheet(block: dict, force: int | None) -> int | None:
    measured = _sheet_retention((block or {}).get("retention_months"))
    if measured is None:
        return None
    if force is not None:
        return int(min(36, max(1, force)))
    return measured


def _search_volume_scale(name_suffix: str | None) -> float:
    """Scale on Search disk. Search is priced on the measured local store.

    Search disk is never scaled by a retention month. An index's age is not a
    retention window for search content.
    """
    return 1.0


def estimator_skip(block: dict, force: int | None, *, always_copy: bool = False) -> str | None:
    """Why an Observability or Security sheet is not filled. None means fill it.

    No workload is the reason when the solution has no stored GB and no bulk.
    A missing retention month is the reason only when that solution has a workload.
    """
    if not always_copy and not (block or {}).get("has_workload"):
        return "no_workload"
    if _months_for_sheet(block, force) is None:
        return "retention"
    return None


def _fill_missing_doc_bytes(acc_path: Path, acc: dict) -> None:
    """Use index telemetry document size when the ingest workload has none."""
    ingest = ((acc.get("workload") or {}).get("ingest") or {})
    if ingest.get("avg_doc_bytes"):
        return
    env_path = next((p / ".env" for p in [acc_path.parent, *acc_path.parents] if (p / ".env").is_file()), None)
    if env_path is None:
        return
    try:
        from k360_infra import es_base_and_key, fill_ingest_doc_bytes, load_env
        base, key = es_base_and_key(load_env(env_path))
        fill_ingest_doc_bytes(base, key, acc)
    except SystemExit:
        return

