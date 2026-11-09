#!/usr/bin/env python3
"""Upload the canvas PDF exports into the customer's Drive folder (Sheets venv).

Consolidation PDFs at measured retention go in `full_retention`. 30-day PDFs go in `30d`.
The account PDF stays in the customer root. Same-named files are updated in place.
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
from render_pdf import pdf_folder_name  # noqa: E402
from privacy import seal_text  # noqa: E402
from size_serverless import assert_not_template, ensure_drive_folder, find_in_folder, require_customer_parent, services  # noqa: E402

PDF_MIME = "application/pdf"


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
    day30 = sizing.get("drive_folder_30d") or {}
    compressed = sizing.get("drive_folder_compressioncalc") or {}
    compressed_30 = sizing.get("drive_folder_compressioncalc_30d") or {}
    root = sizing.get("drive_root") or rounded
    exports = acc.get("exports") or {}
    if not any(folder.get("id") for folder in (rounded, day30, root, compressed, compressed_30)):
        print("no Drive folder (sizing not triggered) — PDFs kept local only")
        return
    _, drive = services()

    def parent_for(kind: str) -> dict:
        base = root if root.get("id") else (rounded or day30 or compressed or compressed_30)
        name = pdf_folder_name(kind)
        if not name or not base.get("id"):
            return base
        return ensure_drive_folder(drive, name, base["id"])

    for kind, e in exports.items():
        pdf = Path(e.get("pdf") or "")
        folder = parent_for(kind)
        if not pdf.is_file() or not folder.get("id"):
            continue
        up = upload_pdf(drive, pdf, folder["id"], pdf.name)
        e["drive_url"] = up["url"]
        print(f"{kind} PDF → {up['url']}")
    acc["exports"] = exports
    seal_text(src, json.dumps(acc, indent=2))


if __name__ == "__main__":
    main()
