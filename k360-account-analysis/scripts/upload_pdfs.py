#!/usr/bin/env python3
"""Upload the canvas PDF exports to the customer's Drive folder (Sheets venv).

Only runs when sizing created a Drive folder (`sizing.drive_folder`); same-named files
in the folder are updated in place. Writes `exports.<kind>.drive_url` back to account.json.

  ~/.config/cloud-tracker-sheets/venv/bin/python upload_pdfs.py --account-json <key>_account.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from size_serverless import find_in_folder, services

PDF_MIME = "application/pdf"


def upload_pdf(drive, local: Path, parent_id: str, title: str) -> dict:
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(local), mimetype=PDF_MIME, resumable=False)
    existing = find_in_folder(drive, parent_id, title)
    if existing:
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
    folder = (acc.get("sizing") or {}).get("drive_folder") or {}
    exports = acc.get("exports") or {}
    if not folder.get("id"):
        print("no Drive folder (sizing not triggered) — PDFs kept local only")
        return
    _, drive = services()
    for kind, e in exports.items():
        pdf = Path(e.get("pdf") or "")
        if not pdf.is_file():
            continue
        up = upload_pdf(drive, pdf, folder["id"], pdf.name)
        e["drive_url"] = up["url"]
        print(f"{kind} PDF → {up['url']}")
    acc["exports"] = exports
    src.write_text(json.dumps(acc, indent=2))


if __name__ == "__main__":
    main()
