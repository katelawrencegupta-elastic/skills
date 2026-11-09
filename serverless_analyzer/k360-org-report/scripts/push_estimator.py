#!/usr/bin/env python3
"""Create <customer>_<date> artifacts folder, copy golden estimators into it, fill cells.

Layout (local + matching Drive folder — PDF must live here too):

  artifacts/<CustomerSlug>_<YYYY-MM-DD>/
    <CustomerSlug>_<YYYY-MM-DD>_Search.xlsx          # exported copy of ES3 golden
    <CustomerSlug>_<YYYY-MM-DD>_Observability.xlsx   # exported copy of O11y golden
    <CustomerSlug>_<YYYY-MM-DD>_{Exec|Full}_report.pdf  # --report exec|full
    <CustomerSlug>_<YYYY-MM-DD>_org_report.json
    artifacts.json

Never mutate golden templates.

  python scripts/push_estimator.py --org-json /tmp/org_report.json --kind both --read-back --report exec
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

from report_mode import add_report_arg, html_name, normalize_report, pdf_name
from sheets_auth import get_creds

DEFAULT_ES3 = "1Jsi5cMIDGfaP-S1_xqDcGeMYJSgklcBbu4dSJAEjlA0"
DEFAULT_O11Y = "1bu-qjVIab-rbWb7zvCEq6QEoLdKpi0vfw3yDj9_VJuE"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME = "application/pdf"
FOLDER_MIME = "application/vnd.google-apps.folder"


def sheets_service():
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=get_creds(interactive=False), cache_discovery=False)


def drive_service():
    from googleapiclient.discovery import build

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
    env = os.environ.get("CLOUD_TRACKER_ARTIFACTS_ROOT")
    if env:
        return Path(env)
    return Path.cwd() / "artifacts"


def ensure_local_folder(root: Path, key: str) -> Path:
    path = root / key
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    dest.write_bytes(buf.getvalue())
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


def push_cells(service, spreadsheet_id: str, sheet_tab: str, cells: dict) -> int:
    data = []
    for a1, val in (cells or {}).items():
        if val is None or val == "":
            continue
        data.append({"range": f"'{sheet_tab}'!{a1}", "values": [[val]]})
    if not data:
        return 0
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    return len(data)


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
        help="Parent of <customer>_<date>/ (default: ./artifacts)",
    )
    ap.add_argument(
        "--no-copy",
        action="store_true",
        help="Write golden ids directly (debug only)",
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
        local_dir.mkdir(parents=True, exist_ok=True)
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
                pdf_local.write_bytes(source.read_bytes())
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
                    pdf_local.write_bytes(alt.read_bytes())
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
        meta_path.write_text(json.dumps(meta, indent=2))
        off = data.setdefault("offload", {})
        off["estimator_artifacts"] = meta
        text = json.dumps(data, indent=2)
        org_path.write_text(text)
        dest_org = local_dir / f"{key}_org_report.json"
        dest_org.write_text(text)
        lines.append(f"Metadata → {meta_path}")
        print("\n".join(lines))
        return

    # Seed org JSON into the folder
    dest_org = local_dir / f"{key}_org_report.json"
    if org_path.resolve() != dest_org.resolve():
        dest_org.write_text(json.dumps(data, indent=2))
        lines.append(f"Copied org JSON → {dest_org.name}")

    artifacts: list[dict] = []

    def target_id(kind: str, template_id: str, product: str) -> str:
        if args.no_copy:
            return template_id
        assert drive is not None and drive_folder is not None
        title = copy_name(customer, when, product)
        copied = copy_spreadsheet_into_folder(drive, template_id, title, drive_folder["id"])
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

    if args.kind in ("es3", "both"):
        block = payload["es3"]
        sid = target_id("es3", args.es3_id, "Search")
        n = push_cells(sheets, sid, block["sheet_tab"], block["cells"])
        lines.append(f"ES3 {block['sheet_tab']}: {n} cells → {sid}")
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
        if drive and not args.no_copy and not args.skip_xlsx_export and artifacts:
            last = next(a for a in artifacts if a["kind"] == "es3")
            try:
                export_xlsx(drive, sid, local_dir / f"{last['title']}.xlsx")
            except Exception:
                pass

    if args.kind in ("o11y", "both"):
        block = payload["o11y"]
        sid = target_id("o11y", args.o11y_id, "Observability")
        n = push_cells(sheets, sid, block["sheet_tab"], block["cells"])
        lines.append(f"O11y {block['sheet_tab']}: {n} cells → {sid}")
        if args.read_back:
            rb = read_ranges(
                sheets,
                sid,
                [
                    f"'{block['sheet_tab']}'!G21",
                    f"'{block['sheet_tab']}'!G23",
                    f"'{block['sheet_tab']}'!G26",
                    f"'{block['sheet_tab']}'!D32",
                ],
            )
            lines.append(f"  read-back: {rb}")
        if drive and not args.no_copy and not args.skip_xlsx_export:
            last = next(a for a in artifacts if a["kind"] == "o11y")
            try:
                export_xlsx(drive, sid, local_dir / f"{last['title']}.xlsx")
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
    }
    meta_path = local_dir / "artifacts.json"
    meta_path.write_text(json.dumps(meta, indent=2))
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
    readme.write_text(
        f"""# {customer} — artifacts ({when.isoformat()})

Folder: `{key}` (= `<customer_name>_<date>`) · report=`{report}`

| File | Role |
|------|------|
| `{expected_pdf}` | {label} report PDF (**same folder** as estimators; local + Drive) |
| `{html_name(key, report)}` | Print HTML |
| `{key}_org_report.json` | fetch_org payload |
| `{key}_Search.xlsx` | Copied + filled ES3 estimator |
| `{key}_Observability.xlsx` | Copied + filled Observability estimator |
| `artifacts.json` | Drive ids / URLs |

Drive folder: {drive_folder['url'] if drive_folder else 'n/a'}
PDF on Drive: {drive_pdf}
"""
    )

    off = data.setdefault("offload", {})
    off["estimator_artifacts"] = meta
    # Keep source org-json and folder copy in sync
    text = json.dumps(data, indent=2)
    org_path.write_text(text)
    dest_org.write_text(text)
    lines.append(f"Updated offload.estimator_artifacts")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
