#!/usr/bin/env python3
"""Upload the canvas PDF exports into the customer's Drive folder (Sheets venv).

Every PDF goes in the customer root: the account PDF, the appendix, and both consolidation
PDFs. Same-named files are updated in place. A consolidation PDF that an earlier run filed
under `full_retention` or `30d` in that root is moved up, and the folder is trashed once empty.
Writes `exports.<kind>.drive_url` back to account.json.

  ~/.config/serverless-analyzer/venv/bin/python upload_pdfs.py --account-json <key>_account.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from skill_paths import find_skill, scripts_dir  # noqa: E402


def _diag_scripts() -> Path:
    return scripts_dir(find_skill("diag-serverless-consolidation", HERE))


sys.path.insert(0, str(_diag_scripts()))
from render_pdf import CONSOLIDATION_PLAN_MARK, LEGACY_PDF_FOLDERS  # noqa: E402
from privacy import seal_text  # noqa: E402
from size_serverless import (  # noqa: E402
    FOLDER_MIME, _execute, assert_not_template, drive_children, find_in_folder, require_customer_parent, services, trash_file,
)

PDF_MIME = "application/pdf"


def retire_legacy_pdf_folders(drive, root_id: str) -> list[str]:
    """Move consolidation PDFs out of `full_retention` and `30d` in the customer root.

    Only folders directly under the customer root are touched. A PDF with no
    same-named file in the root is moved up, so its link keeps working and the
    upload updates it in place. One the root already holds is trashed. A folder is
    trashed once it is empty; one that still holds anything else is left alone.
    Returns the names moved.
    """
    root_id = require_customer_parent(root_id)
    moved: list[str] = []
    for name in LEGACY_PDF_FOLDERS:
        legacy = find_in_folder(drive, root_id, name)
        if not legacy or legacy.get("mimeType") != FOLDER_MIME:
            continue
        legacy_id = legacy["id"]
        for item in drive_children(drive, legacy_id):
            title = item.get("name") or ""
            if item.get("mimeType") == FOLDER_MIME or CONSOLIDATION_PLAN_MARK not in title:
                continue
            assert_not_template(item["id"])
            if find_in_folder(drive, root_id, title):
                trash_file(drive, item["id"])
                continue
            _execute(lambda fid=item["id"]: drive.files().update(
                fileId=fid, addParents=root_id, removeParents=legacy_id, fields="id,parents",
            ))
            moved.append(title)
        if not drive_children(drive, legacy_id):
            trash_file(drive, legacy_id)
    return moved


def upload_pdf(drive, local: Path, parent_id: str, title: str) -> dict:
    from googleapiclient.http import MediaFileUpload

    parent_id = require_customer_parent(parent_id)
    media = MediaFileUpload(str(local), mimetype=PDF_MIME, resumable=False)
    existing = find_in_folder(drive, parent_id, title)
    if existing:
        assert_not_template(existing["id"])
        meta = drive.files().update(fileId=existing["id"], media_body=media, fields="id,name,webViewLink").execute()
    else:
        meta = drive.files().create(body={"name": title, "parents": [parent_id], "mimeType": PDF_MIME}, media_body=media, fields="id,name,webViewLink").execute()
    return {"id": meta["id"], "url": meta.get("webViewLink") or f"https://drive.google.com/file/d/{meta['id']}/view"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-json", required=True)
    args = ap.parse_args()
    src = Path(args.account_json)
    acc = json.loads(src.read_text())
    sizing = acc.get("sizing") or {}
    rounded = sizing.get("drive_folder") or {}
    root = sizing.get("drive_root") or {}
    exports = acc.get("exports") or {}
    if not any(folder.get("id") for folder in (rounded, root)):
        print("no Drive folder (sizing not triggered) — PDFs kept local only")
        return
    _, drive = services()
    # Every PDF goes in the customer root. The estimate folders are the fallback when no root was recorded.
    in_root = bool(root.get("id"))
    folder = root if in_root else rounded
    if in_root:
        # Tidying the old folders must never stop the new PDFs from uploading.
        try:
            for name in retire_legacy_pdf_folders(drive, folder["id"]):
                print(f"moved {name} into the customer folder")
        except Exception as ex:  # noqa: BLE001
            print(f"WARN: could not tidy the old full_retention/30d folders ({ex!r}); uploading the PDFs anyway", file=sys.stderr)

    for kind, e in exports.items():
        pdf = Path(e.get("pdf") or "")
        if not pdf.is_file() or not folder.get("id"):
            continue
        up = upload_pdf(drive, pdf, folder["id"], pdf.name)
        e["drive_url"] = up["url"]
        print(f"{kind} PDF → {up['url']}")
    acc["exports"] = exports
    seal_text(src, json.dumps(acc, indent=2))


if __name__ == "__main__":
    main()
