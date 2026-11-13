#!/usr/bin/env python3
"""Create <customer>_<date> artifacts folder, copy golden estimators into it, fill cells.

Layout (local + matching Drive folder — PDF must live here too):

  serverless_analyzer/diag_artifacts/<CustomerSlug>_<YYYY-MM-DD>/
    <CustomerSlug>_<YYYY-MM-DD>_Search.xlsx          # exported copy of ES3 golden
    <CustomerSlug>_<YYYY-MM-DD>_Observability.xlsx   # exported copy of O11y golden
    <CustomerSlug>_<YYYY-MM-DD>_{Exec|Full}_report.pdf  # --report exec|full
    <CustomerSlug>_<YYYY-MM-DD>_org_report.json
    artifacts.json

Never mutate golden templates. `--no-copy` writes to a sheet you name with
`--o11y-id` / `--es3-id` and refuses a golden template id.

The Search (ES3) estimator is filled only when estimator_layout.py records its
layout. Until then `--kind both` fills Observability and says Search was not
filled, and `--kind es3` stops.

  python scripts/push_estimator.py --org-json <folder>/<key>_org_report.json --kind both --read-back --report exec
  python scripts/push_estimator.py --org-json … --upload-pdf-only --report full
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from report_mode import add_report_arg, html_name, normalize_report, pdf_name, seal_dir, write_private
from skill_paths import artifacts_root
from estimator_layout import (
    ES3_LAYOUT_MISSING,
    GOLDEN_ES3,
    GOLDEN_OBSERVABILITY,
    REFERENCE_TAB,
    check_value_ranges,
    is_golden_template,
    layout_ranges,
    layout_recorded,
    match_region,
    region_options,
    sheet_id,
    upgrade_observability_cells,
)

DEFAULT_ES3 = GOLDEN_ES3
DEFAULT_O11Y = GOLDEN_OBSERVABILITY  # Observability estimator v1.22
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME = "application/pdf"
FOLDER_MIME = "application/vnd.google-apps.folder"


def sheets_service():
    from googleapiclient.discovery import build
    from sheets_auth import get_creds

    return build("sheets", "v4", credentials=get_creds(interactive=False), cache_discovery=False)


def drive_service():
    from googleapiclient.discovery import build
    from sheets_auth import get_creds

    return build("drive", "v3", credentials=get_creds(interactive=False), cache_discovery=False)


def customer_slug(name: str) -> str:
    s = (name or "Customer").strip()
    s = re.sub(r"[^\w\s\-]+", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s\-]+", "_", s).strip("_")
    return s or "Customer"


def run_key(customer: str, when: date) -> str:
    """Folder / base name: <customer_name>_<date>."""
    return f"{customer_slug(customer)}_{when.isoformat()}"


def copy_name(customer: str, when: date, product: str) -> str:
    """Each estimator copy: <customer_name>_<date>_<Search|Observability>."""
    return f"{run_key(customer, when)}_{product}"


def sheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def resolve_customer(data: dict, payload: dict) -> str:
    org = data.get("organization") or {}
    if isinstance(org, dict) and org.get("name"):
        return str(org["name"])
    if payload.get("org"):
        return str(payload["org"])
    if data.get("org"):
        return str(data["org"])
    return "Customer"


def default_artifacts_root() -> Path:
    return artifacts_root()


def ensure_local_folder(root: Path, key: str) -> Path:
    """The run's artifacts folder, private to this user (0700)."""
    return seal_dir(root / key)


def ensure_drive_folder(drive, name: str) -> dict:
    """Find or create a Drive folder named exactly `name` in My Drive."""
    q = (
        f"name = '{name.replace(chr(39), chr(92) + chr(39))}' "
        f"and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    found = (
        drive.files()
        .list(q=q, spaces="drive", fields="files(id,name,webViewLink)", pageSize=5)
        .execute()
        .get("files")
        or []
    )
    if found:
        f = found[0]
        return {
            "id": f["id"],
            "name": f.get("name") or name,
            "url": f.get("webViewLink") or folder_url(f["id"]),
        }
    meta = (
        drive.files()
        .create(
            body={"name": name, "mimeType": FOLDER_MIME},
            fields="id,name,webViewLink",
        )
        .execute()
    )
    return {
        "id": meta["id"],
        "name": meta.get("name") or name,
        "url": meta.get("webViewLink") or folder_url(meta["id"]),
    }


def copy_spreadsheet_into_folder(drive, source_id: str, title: str, parent_id: str) -> dict:
    meta = (
        drive.files()
        .copy(
            fileId=source_id,
            body={"name": title, "parents": [parent_id]},
            supportsAllDrives=True,
            fields="id,name,webViewLink",
        )
        .execute()
    )
    sid = meta["id"]
    return {
        "id": sid,
        "name": meta.get("name") or title,
        "url": meta.get("webViewLink") or sheet_url(sid),
    }


def export_xlsx(drive, spreadsheet_id: str, dest: Path) -> Path:
    from googleapiclient.http import MediaIoBaseDownload

    request = drive.files().export_media(fileId=spreadsheet_id, mimeType=XLSX_MIME)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    write_private(dest, buf.getvalue())
    return dest


def find_drive_file(drive, parent_id: str, name: str) -> dict | None:
    q = (
        f"'{parent_id}' in parents and name = '{name.replace(chr(39), chr(92) + chr(39))}' "
        f"and trashed = false"
    )
    res = (
        drive.files()
        .list(q=q, spaces="drive", fields="files(id,name,webViewLink)", pageSize=5)
        .execute()
    )
    files = res.get("files") or []
    return files[0] if files else None


def upload_pdf_to_folder(drive, local_pdf: Path, parent_id: str, name: str | None = None) -> dict:
    """Create or replace PDF in the Drive artifacts folder (same folder as estimators)."""
    from googleapiclient.http import MediaFileUpload

    title = name or local_pdf.name
    media = MediaFileUpload(str(local_pdf), mimetype=PDF_MIME, resumable=True)
    existing = find_drive_file(drive, parent_id, title)
    if existing:
        meta = (
            drive.files()
            .update(
                fileId=existing["id"],
                media_body=media,
                supportsAllDrives=True,
                fields="id,name,webViewLink",
            )
            .execute()
        )
    else:
        meta = (
            drive.files()
            .create(
                body={"name": title, "parents": [parent_id], "mimeType": PDF_MIME},
                media_body=media,
                supportsAllDrives=True,
                fields="id,name,webViewLink",
            )
            .execute()
        )
    return {
        "id": meta["id"],
        "name": meta.get("name") or title,
        "url": meta.get("webViewLink")
        or f"https://drive.google.com/file/d/{meta['id']}/view",
    }


def assert_writable(spreadsheet_id: str) -> None:
    """Stop before any write to a golden template."""
    if is_golden_template(spreadsheet_id):
        raise SystemExit(
            f"ERROR: {sheet_id(spreadsheet_id)} is a golden estimator template. Nothing was written. "
            "Templates are copied, never filled."
        )


def cell_updates(sheet_tab: str, cells: dict) -> list[dict]:
    """One update per cell. A cell with no value is written blank, so the template's sample does not stay."""
    data = []
    for a1, val in (cells or {}).items():
        data.append({"range": f"'{sheet_tab}'!{a1}", "values": [["" if val is None else val]]})
    return data


def push_cells(service, spreadsheet_id: str, sheet_tab: str, cells: dict) -> int:
    assert_writable(spreadsheet_id)
    data = cell_updates(sheet_tab, cells)
    if not data:
        return 0
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    return len(data)


def layout_problems(service, spreadsheet_id: str, kind: str, sheet_tab: str) -> list[str]:
    """Why this copy is not the layout the cells were built for. Empty means it matches.

    Reads the label beside every cell the fill uses and the sheet's version.
    """
    cells, ranges = layout_ranges(kind, sheet_tab)
    res = (
        service.spreadsheets()
        .values()
        .batchGet(spreadsheetId=spreadsheet_id, ranges=ranges, valueRenderOption="FORMATTED_VALUE")
        .execute()
    )
    return check_value_ranges(kind, cells, res.get("valueRanges") or [])["problems"]


def org_cloud(data: dict, payload: dict, block: dict) -> dict:
    """The org's provider and region: the block's own, then the payload's, then the infrastructure summary."""
    for src in ((block or {}).get("cloud"), (payload or {}).get("cloud"), (data.get("infrastructure") or {}).get("summary")):
        if src and (src.get("provider") or src.get("region")):
            return {"provider": src.get("provider"), "region": src.get("region")}
    return {"provider": None, "region": None}


def resolve_region(options: list[str], cloud: dict) -> dict:
    """Which sheet option is the org's region. No network.

    `region_match` is exact, alias, or none. With none, `option` is None and the
    region cell is not written: the copy keeps the template's own value.
    """
    option, how = match_region(cloud.get("provider"), cloud.get("region"), options)
    return {
        "region_match": how,
        "region_option": option,
        "region_wanted": {"provider": cloud.get("provider"), "region": cloud.get("region")},
    }


def read_region_options(service, spreadsheet_id: str) -> list[str]:
    """The region dropdown strings on this copy's reference tab."""
    res = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=f"'{REFERENCE_TAB}'!A1:M450")
        .execute()
    )
    return region_options(res.get("values") or [])


def read_ranges(service, spreadsheet_id: str, ranges: list[str]) -> dict:
    if not ranges:
        return {}
    res = (
        service.spreadsheets()
        .values()
        .batchGet(spreadsheetId=spreadsheet_id, ranges=ranges)
        .execute()
    )
    out = {}
    for vr in res.get("valueRanges", []):
        rng = vr.get("range", "")
        vals = vr.get("values") or []
        out[rng] = vals[0][0] if vals and vals[0] else None
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org-json", required=True)
    ap.add_argument("--kind", choices=("o11y", "es3", "both"), default="both")
    ap.add_argument("--es3-id", default=os.environ.get("ES3_SHEET_ID", DEFAULT_ES3))
    ap.add_argument("--o11y-id", default=os.environ.get("O11Y_SHEET_ID", DEFAULT_O11Y))
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today UTC)")
    ap.add_argument(
        "--artifacts-root",
        default=None,
        help="Parent of <customer>_<date>/ (default: serverless_analyzer/diag_artifacts)",
    )
    ap.add_argument(
        "--no-copy",
        action="store_true",
        help="Fill the sheet named by --o11y-id / --es3-id instead of a new copy. A golden template id is refused",
    )
    ap.add_argument("--skip-xlsx-export", action="store_true")
    ap.add_argument("--read-back", action="store_true")
    ap.add_argument(
        "--pdf",
        default=None,
        help="Path to report PDF to place in the artifacts folder (local copy + Drive upload)",
    )
    ap.add_argument(
        "--upload-pdf-only",
        action="store_true",
        help="Skip sheet copy/fill; only ensure PDF is in local + Drive artifacts folder",
    )
    add_report_arg(ap)
    args = ap.parse_args()
    report = normalize_report(args.report)
    fill_es3 = args.kind in ("es3", "both") and not args.upload_pdf_only
    fill_o11y = args.kind in ("o11y", "both") and not args.upload_pdf_only
    es3_skipped = None
    if fill_es3 and not layout_recorded("es3"):
        if args.kind == "es3":
            raise SystemExit(f"ERROR: {ES3_LAYOUT_MISSING}")
        fill_es3, es3_skipped = False, ES3_LAYOUT_MISSING
    if args.no_copy:
        # Checked before any Google call: --no-copy must never reach a template.
        for flag, wanted, sid in (("--es3-id", fill_es3, args.es3_id), ("--o11y-id", fill_o11y, args.o11y_id)):
            if wanted and is_golden_template(sid):
                raise SystemExit(
                    f"ERROR: --no-copy would write into the golden template {sheet_id(sid)} ({flag}). "
                    f"Nothing was written. Pass {flag} with the id of your own copy, or drop --no-copy."
                )

    org_path = Path(args.org_json)
    data = json.loads(org_path.read_text())
    payload = (data.get("offload") or {}).get("apps_script_payload")
    if not payload and not args.upload_pdf_only:
        raise SystemExit("No offload.apps_script_payload — re-run fetch_org.py")

    customer = resolve_customer(data, payload or {})
    when = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()
    # Prefer run key from a prior push when uploading PDF only
    prior = ((data.get("offload") or {}).get("estimator_artifacts") or {})
    key = prior.get("run_key") or run_key(customer, when)
    if args.date is None and prior.get("date"):
        when = date.fromisoformat(prior["date"])
        key = prior.get("run_key") or run_key(customer, when)
    root = Path(args.artifacts_root) if args.artifacts_root else default_artifacts_root()
    if prior.get("local_folder") and args.artifacts_root is None and args.upload_pdf_only:
        local_dir = Path(prior["local_folder"])
        seal_dir(local_dir)
    else:
        local_dir = ensure_local_folder(root, key)

    sheets = None if args.upload_pdf_only else sheets_service()
    drive = None if args.no_copy else drive_service()
    drive_folder = None
    if drive is not None:
        if prior.get("drive_folder") and prior["drive_folder"].get("id") and args.upload_pdf_only:
            drive_folder = prior["drive_folder"]
        else:
            drive_folder = ensure_drive_folder(drive, key)

    lines: list[str] = [f"Artifacts folder → {local_dir}"]
    if drive_folder:
        lines.append(f"Drive folder → {drive_folder['url']}")

    expected_pdf = pdf_name(key, report)
    pdf_local = local_dir / expected_pdf
    pdf_drive_meta = None

    def place_pdf(src: Path | None = None) -> None:
        nonlocal pdf_drive_meta
        source = Path(src) if src else (Path(args.pdf) if args.pdf else None)
        if source is not None:
            source = source.expanduser().resolve()
            if not source.is_file():
                raise SystemExit(f"PDF not found: {source}")
            if source.resolve() != pdf_local.resolve():
                write_private(pdf_local, source.read_bytes())
                lines.append(f"PDF → {pdf_local.name} (copied into artifacts folder)")
            else:
                lines.append(f"PDF → {pdf_local.name} (already in folder)")
        elif not pdf_local.is_file():
            # tolerate legacy Org_report or the other mode sitting in the folder
            for alt in (
                local_dir / pdf_name(key, "exec"),
                local_dir / pdf_name(key, "full"),
                local_dir / f"{key}_Org_report.pdf",
            ):
                if alt.is_file() and alt.resolve() != pdf_local.resolve():
                    write_private(pdf_local, alt.read_bytes())
                    lines.append(f"PDF → {pdf_local.name} (from {alt.name})")
                    break
            else:
                return
        else:
            lines.append(f"PDF → {pdf_local.name} (found in folder)")

        if drive is not None and drive_folder is not None:
            pdf_drive_meta = upload_pdf_to_folder(
                drive, pdf_local, drive_folder["id"], expected_pdf
            )
            lines.append(f"PDF uploaded to Drive folder → {pdf_drive_meta['url']}")

    if args.upload_pdf_only:
        if not args.pdf and not pdf_local.is_file() and not any(
            (local_dir / n).is_file()
            for n in (
                pdf_name(key, "exec"),
                pdf_name(key, "full"),
                f"{key}_Org_report.pdf",
            )
        ):
            raise SystemExit(
                f"--upload-pdf-only needs --pdf or an existing {expected_pdf} in {local_dir}"
            )
        place_pdf()
        # Merge into artifacts.json / estimator_artifacts
        meta = dict(prior) if prior else {}
        meta.update(
            {
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                "customer": customer,
                "customer_slug": customer_slug(customer),
                "date": when.isoformat(),
                "run_key": key,
                "report": report,
                "local_folder": str(local_dir),
                "drive_folder": drive_folder or meta.get("drive_folder"),
                "pdf": {
                    "local": str(pdf_local) if pdf_local.is_file() else None,
                    "drive": pdf_drive_meta,
                },
                "pdf_expected": expected_pdf,
            }
        )
        meta_path = local_dir / "artifacts.json"
        write_private(meta_path, json.dumps(meta, indent=2))
        off = data.setdefault("offload", {})
        off["estimator_artifacts"] = meta
        text = json.dumps(data, indent=2)
        write_private(org_path, text)
        dest_org = local_dir / f"{key}_org_report.json"
        write_private(dest_org, text)
        lines.append(f"Metadata → {meta_path}")
        print("\n".join(lines))
        return

    # Seed org JSON into the folder
    dest_org = local_dir / f"{key}_org_report.json"
    if org_path.resolve() != dest_org.resolve():
        write_private(dest_org, json.dumps(data, indent=2))
        lines.append(f"Copied org JSON → {dest_org.name}")

    artifacts: list[dict] = []

    def target_id(kind: str, template_id: str, product: str) -> str:
        if args.no_copy:
            # The caller's own sheet. A golden template id was refused before any Google call.
            assert_writable(template_id)
            artifacts.append({"kind": kind, "product": product, "spreadsheet_id": sheet_id(template_id), "copied": False})
            return sheet_id(template_id)
        assert drive is not None and drive_folder is not None
        title = copy_name(customer, when, product)
        copied = copy_spreadsheet_into_folder(drive, sheet_id(template_id), title, drive_folder["id"])
        entry = {
            "kind": kind,
            "product": product,
            "title": copied["name"],
            "template_id": template_id,
            "spreadsheet_id": copied["id"],
            "url": copied["url"],
            "local_xlsx": None,
            "naming": "<customer_name>_<date>_<Search|Observability>",
        }
        lines.append(f"Copied golden {kind} → Drive '{copied['name']}'")
        lines.append(f"  {copied['url']}")

        if not args.skip_xlsx_export:
            xlsx = local_dir / f"{title}.xlsx"
            try:
                export_xlsx(drive, copied["id"], xlsx)
                entry["local_xlsx"] = str(xlsx)
                lines.append(f"  Exported → {xlsx.name}")
            except Exception as e:
                lines.append(f"  XLSX export failed ({e}); Drive copy still usable")

        artifacts.append(entry)
        return copied["id"]

    def stop_on_layout(kind: str, label: str, sid: str, problems: list[str], id_flag: str) -> None:
        """Fail closed: nothing is written to a sheet whose rows are not where the cells expect."""
        if not problems:
            return
        if drive and not args.no_copy:
            try:
                drive.files().update(fileId=sid, body={"trashed": True}, supportsAllDrives=True).execute()
            except Exception:
                pass
        raise SystemExit(
            f"ERROR: {label} estimator copy {sid} does not match the layout these cells were built for. "
            + "; ".join(problems[:6])
            + f". Nothing was written. Point {id_flag} at the current estimator, or update estimator_layout.py and the cell map together."
        )

    if es3_skipped:
        lines.append(f"ES3 NOT FILLED: {es3_skipped}")
    if fill_es3:
        block = payload["es3"]
        sid = target_id("es3", args.es3_id, "Search")
        stop_on_layout("es3", "Search (ES3)", sid, layout_problems(sheets, sid, "es3", block["sheet_tab"]), "--es3-id")
        n = push_cells(sheets, sid, block["sheet_tab"], block["cells"])
        lines.append(f"ES3 {block['sheet_tab']}: {n} cells → {sid}")
        entry = next((a for a in artifacts if a["kind"] == "es3"), None)
        if entry is not None:
            # The provider and region cells are not written: the sheet's option strings are not recorded.
            entry.update({"region_match": "none", "region_option": None, "region_wanted": org_cloud(data, payload, block)})
            for k in ("assumed_cells", "unmeasured_cells", "hour_mix"):
                if block.get(k):
                    entry[k] = block[k]
        if args.read_back:
            rb = read_ranges(
                sheets,
                sid,
                [
                    f"'{block['sheet_tab']}'!C19",
                    f"'{block['sheet_tab']}'!C30",
                    f"'{block['sheet_tab']}'!H8",
                ],
            )
            lines.append(f"  read-back: {rb}")
        # Re-export after fill so local xlsx has values
        if drive and not args.no_copy and not args.skip_xlsx_export and entry is not None:
            try:
                export_xlsx(drive, sid, local_dir / f"{entry['title']}.xlsx")
            except Exception:
                pass

    if fill_o11y:
        try:
            block = upgrade_observability_cells(payload["o11y"])
        except ValueError as ex:
            raise SystemExit(f"ERROR: {ex}") from ex
        if block.get("upgraded_from"):
            lines.append(f"O11y cells were built for estimator v{block['upgraded_from']} and were moved to the v{block['layout']} addresses")
        sid = target_id("o11y", args.o11y_id, "Observability")
        stop_on_layout("observability", "Observability", sid, layout_problems(sheets, sid, "observability", block["sheet_tab"]), "--o11y-id")
        # The region cell follows the org's own provider and region. It is written only when the
        # copy has that option; an org JSON fetched before this check may carry another cloud's string.
        cells = dict(block["cells"])
        region_cell = (block.get("cloud") or {}).get("cell") or "G8"
        cells.pop(region_cell, None)
        region = resolve_region(read_region_options(sheets, sid), org_cloud(data, payload, block))
        if region["region_option"]:
            cells[region_cell] = region["region_option"]
            lines.append(f"O11y region {region_cell}: {region['region_option']} ({region['region_match']})")
        else:
            wanted = region["region_wanted"]
            lines.append(
                f"O11y region {region_cell} NOT WRITTEN: no estimator option for {wanted.get('provider')} {wanted.get('region')}. "
                "The copy keeps the template's region (region_match: none)."
            )
        n = push_cells(sheets, sid, block["sheet_tab"], cells)
        lines.append(f"O11y {block['sheet_tab']}: {n} cells → {sid}")
        entry = next((a for a in artifacts if a["kind"] == "o11y"), None)
        if entry is not None:
            entry.update(region)
            if block.get("unmeasured_cells"):
                entry["unmeasured_cells"] = block["unmeasured_cells"]
        if args.read_back:
            rb = read_ranges(
                sheets,
                sid,
                [
                    f"'{block['sheet_tab']}'!G8",    # region on the copy
                    f"'{block['sheet_tab']}'!G23",   # logs GB/day
                    f"'{block['sheet_tab']}'!G25",   # metrics GB/day
                    f"'{block['sheet_tab']}'!G28",   # total raw GB/day
                    f"'{block['sheet_tab']}'!D38",   # monthly, steady state
                ],
            )
            lines.append(f"  read-back: {rb}")
        if drive and not args.no_copy and not args.skip_xlsx_export and entry is not None:
            try:
                export_xlsx(drive, sid, local_dir / f"{entry['title']}.xlsx")
            except Exception:
                pass

    # PDF into the same local + Drive folder (required for a complete run)
    place_pdf()

    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "customer": customer,
        "customer_slug": customer_slug(customer),
        "date": when.isoformat(),
        "run_key": key,
        "folder_pattern": "<customer_name>_<date>",
        "copy_pattern": "<customer_name>_<date>_<Search|Observability>",
        "local_folder": str(local_dir),
        "drive_folder": drive_folder,
        "copied": not args.no_copy,
        "report": report,
        "pdf_expected": expected_pdf,
        "pdf": {
            "local": str(pdf_local) if pdf_local.is_file() else None,
            "drive": pdf_drive_meta,
        },
        "artifacts": artifacts,
        # Why a requested estimator was not filled. None when every requested one was.
        "es3_not_filled": es3_skipped,
    }
    meta_path = local_dir / "artifacts.json"
    write_private(meta_path, json.dumps(meta, indent=2))
    lines.append(f"Metadata → {meta_path}")
    if not pdf_local.is_file():
        lines.append(
            f"NOTE: write {expected_pdf} into this folder, then "
            f"re-run with --upload-pdf-only --report {report} (or --pdf PATH)"
        )

    # README index
    readme = local_dir / "README.md"
    drive_pdf = (pdf_drive_meta or {}).get("url") or "pending"
    label = "Full" if report == "full" else "Exec"
    search_row = (
        f"| `{key}_Search.xlsx` | Copied + filled ES3 estimator |"
        if any(a["kind"] == "es3" for a in artifacts)
        else "| (no Search estimator) | Not filled: no ES3 layout is recorded in estimator_layout.py |"
    )
    write_private(
        readme,
        f"""# {customer} — artifacts ({when.isoformat()})

Folder: `{key}` (= `<customer_name>_<date>`) · report=`{report}`

| File | Role |
|------|------|
| `{expected_pdf}` | {label} report PDF (**same folder** as estimators; local + Drive) |
| `{html_name(key, report)}` | Print HTML |
| `{key}_org_report.json` | fetch_org payload |
{search_row}
| `{key}_Observability.xlsx` | Copied + filled Observability estimator |
| `artifacts.json` | Drive ids / URLs |

Drive folder: {drive_folder['url'] if drive_folder else 'n/a'}
PDF on Drive: {drive_pdf}
""",
    )

    off = data.setdefault("offload", {})
    off["estimator_artifacts"] = meta
    # Keep source org-json and folder copy in sync
    text = json.dumps(data, indent=2)
    write_private(org_path, text)
    write_private(dest_org, text)
    lines.append("Updated offload.estimator_artifacts")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
