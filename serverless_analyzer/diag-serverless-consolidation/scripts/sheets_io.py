"""Drive and Sheets calls for one estimate."""
from __future__ import annotations

from sizing_shared import *  # noqa: F401,F403

def sheet_id(value: str) -> str:
    """Accept a bare spreadsheet id or a full docs.google.com URL."""
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value or "")
    return m.group(1) if m else (value or "").strip()


def template_ids() -> set[str]:
    """Golden templates and any id the operator pointed at those env overrides."""
    ids = {DEFAULT_O11Y, DEFAULT_SECURITY}
    for name in ("O11Y_SHEET_ID", "SECURITY_SHEET_ID"):
        raw = os.environ.get(name)
        if raw:
            ids.add(sheet_id(raw))
    return {item for item in ids if item}


def assert_not_template(spreadsheet_id: str) -> None:
    sid = sheet_id(str(spreadsheet_id or ""))
    if sid and sid in template_ids():
        raise RuntimeError(f"refusing to modify template {sid}")


def require_customer_parent(parent_id: str) -> str:
    parent = str(parent_id or "").strip()
    if not parent or sheet_id(parent) in template_ids():
        raise RuntimeError("upload parent must be the customer Drive folder for this run")
    return parent


_SHEET_LABELS = frozenset({
    "Complete", "Essentials", "Enterprise", "Standard", "Premium",
    "Gold", "Platinum", "No", "Yes", "no", "N", "FALSE", "False",
    "0%", "100%",
})
_NUMBER_TEXT = re.compile(r"\d+(\.\d+)?")
_FORMULA_PREFIX = "=+-@"


def cell_input_mode(value, *, allowed_text: set[str] | None = None) -> str:
    """RAW for numbers. USER_ENTERED only for known labels. Formula text is refused."""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return "RAW"
    text = str(value)
    if text.lstrip()[:1] in _FORMULA_PREFIX:
        raise ValueError(f"refusing formula-like sheet value {text!r}")
    if _NUMBER_TEXT.fullmatch(text):
        return "RAW"
    allowed = set(_SHEET_LABELS)
    if allowed_text:
        allowed.update(str(item) for item in allowed_text)
    if text not in allowed:
        raise ValueError(f"refusing sheet text {text!r}")
    return "USER_ENTERED"


def partition_cells(cells: dict, *, allowed_text: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    raw: list[dict] = []
    entered: list[dict] = []
    for a1, value in cells.items():
        if value is None or value == "":
            continue
        item = {"range": a1, "values": [[value]]}
        if cell_input_mode(value, allowed_text=allowed_text) == "RAW":
            raw.append(item)
        else:
            entered.append(item)
    return raw, entered

O11Y_TAB = "Observability Estimator"
SEC_TAB = "Security Estimator"
REF_TAB = "Estimator_Reference"

# Read-back cells (see ../references/sizing-cells.md)
O11Y_READBACK = {
    "total_raw_gb_day": "G28",
    "metered_gb_day": "G29",
    # GB/day the two ingest ladders price. On the metered basis these are G19 and G20 turned back into GB/day.
    "metered_ex_metrics_gb_day": "Estimator_Reference!C2",
    "metered_metrics_gb_day": "Estimator_Reference-Metrics!C2",
    "monthly_steady_state": "D38",
    "annual_year1": "E38",
    "annual_steady_state": "F38",
}
SEC_READBACK = {
    "metered_gb_day": "G5",
    "monthly_steady_state": "E74",
    "annual_year1": "D36",
    "annual_year2": "E36",
    "annual_year3": "F36",
    "annual_steady_state": "E36",
    "cps_addon_monthly": "E73",
    # The three lines a SIEM quote is made of, so any other line in E74 shows up.
    "ingest_monthly": "E61",
    "retention_monthly": "E62",
    "support_monthly": "E69",
}

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FOLDER_MIME = "application/vnd.google-apps.folder"
GDOC_MIME = "application/vnd.google-apps.document"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
_REGION_OPT = re.compile(r"^(AWS|AZURE|GCP)\s*:?\s*([a-z0-9\-]+)", re.IGNORECASE)


# ----------------------------------------------------------------------------- google
def sheet_workers() -> int:
    """Concurrent estimator copies. Clamped to 1–4. SHEET_WORKERS overrides the default of 4."""
    raw = os.environ.get("SHEET_WORKERS", "4")
    try:
        count = int(raw)
    except (TypeError, ValueError):
        count = 4
    return max(1, min(4, count))


def map_in_order(fn, items: list):
    """Run fn on each item and keep input order. One item, or one worker, stays on this thread."""
    if len(items) <= 1 or sheet_workers() == 1:
        return [fn(item) for item in items]
    workers = min(sheet_workers(), len(items))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def services_from(creds):
    """A fresh Sheets and Drive client. Each worker builds its own; the client is not thread-safe."""
    from googleapiclient.discovery import build

    return (
        build("sheets", "v4", credentials=creds, cache_discovery=False),
        build("drive", "v3", credentials=creds, cache_discovery=False),
    )


def load_sheet_creds():
    return get_creds(interactive=False)


def services():
    return services_from(load_sheet_creds())


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def ensure_drive_folder(drive, name: str, parent_id: str | None = None) -> dict:
    if parent_id:
        parent_id = require_customer_parent(parent_id)
    parent = f" and '{parent_id}' in parents" if parent_id else ""
    q = f"name = '{_esc(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false{parent}"
    found = _execute(lambda: drive.files().list(q=q, spaces="drive", fields="files(id,name,webViewLink)", pageSize=5)).get("files") or []
    if found:
        f = found[0]
        return {"id": f["id"], "name": f["name"], "url": f.get("webViewLink") or f"https://drive.google.com/drive/folders/{f['id']}"}
    body = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    meta = _execute(lambda: drive.files().create(body=body, fields="id,name,webViewLink"))
    return {"id": meta["id"], "name": meta["name"], "url": meta.get("webViewLink") or f"https://drive.google.com/drive/folders/{meta['id']}"}


def list_in_folder(drive, parent_id: str, name: str) -> list[dict]:
    """Every live file with this exact name in the folder, newest first."""
    parent_id = require_customer_parent(parent_id)
    q = f"'{parent_id}' in parents and name = '{_esc(name)}' and trashed = false"
    return _execute(lambda: drive.files().list(
        q=q, spaces="drive", fields="files(id,name,webViewLink,mimeType,createdTime)", pageSize=50, orderBy="createdTime desc",
    )).get("files") or []


def find_in_folder(drive, parent_id: str, name: str) -> dict | None:
    files = list_in_folder(drive, parent_id, name)
    return files[0] if files else None


def trash_file(drive, file_id: str) -> None:
    """Move a file to the Drive trash. It can still be restored from there."""
    assert_not_template(file_id)
    _execute(lambda: drive.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True))


def trash_same_title(drive, parent_id: str, name: str, keep_id: str | None) -> list[str]:
    """One estimate per title: trash every same-titled file in the folder except the one kept."""
    trashed = []
    for f in list_in_folder(drive, parent_id, name):
        if f["id"] != keep_id:
            trash_file(drive, f["id"])
            trashed.append(f["id"])
    return trashed


def dedupe_estimates(drive, root_id: str, *, dry_run: bool = False) -> list[dict]:
    """Keep the newest sheet for each title anywhere under the run folder and trash the rest.

    The title carries run key, solution, retention window, bulk or compression-applied, and
    region, so two sheets with one title are the same estimate. PDFs are not touched.
    """
    seen: dict[str, list[dict]] = {}

    def walk(folder_id: str, path: str) -> None:
        token = None
        while True:
            r = _execute(lambda: drive.files().list(
                q=f"'{folder_id}' in parents and trashed = false", spaces="drive",
                fields="nextPageToken,files(id,name,mimeType,createdTime)", pageSize=200, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ))
            for f in r.get("files") or []:
                if f["mimeType"] == FOLDER_MIME:
                    walk(f["id"], f"{path}/{f['name']}")
                elif f["mimeType"] == GSHEET_MIME:
                    seen.setdefault(f["name"], []).append({**f, "path": path})
            token = r.get("nextPageToken")
            if not token:
                break

    walk(root_id, "")
    removed = []
    for name, group in seen.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda f: f.get("createdTime") or "", reverse=True)
        for f in group[1:]:
            if not dry_run:
                trash_file(drive, f["id"])
            removed.append({"name": name, "id": f["id"], "path": f["path"], "kept": group[0]["id"]})
    return removed


def _sheet_record(meta: dict) -> dict:
    assert_not_template(meta["id"])
    return {
        "id": meta["id"],
        "name": meta.get("name"),
        "url": meta.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{meta['id']}/edit",
    }


def copy_template(drive, template_id: str, title: str, parent_id: str, *, reuse: bool) -> tuple[dict, bool]:
    parent_id = require_customer_parent(parent_id)
    if reuse:
        existing = find_in_folder(drive, parent_id, title)
        if existing:
            return _sheet_record(existing), True
    meta = _execute(lambda: drive.files().copy(fileId=template_id, body={"name": title, "parents": [parent_id]}, supportsAllDrives=True, fields="id,name,webViewLink"))
    return _sheet_record(meta), False


# Exit status when an estimator copy does not have the layout this skill writes.
TEMPLATE_MISMATCH_EXIT = 3


class TemplateMismatch(RuntimeError):
    """The estimator copy is not the layout these cell addresses were written for."""


def verify_template(sheets, sid: str, kind: str, tab: str) -> dict:
    """Read the labels beside every cell the skill uses, and the sheet's version.

    Raises TemplateMismatch before anything is written when a label is not where
    it is expected or the version is older than the recorded layout. The check
    reads the copy, never the golden template.
    """
    assert_not_template(sid)
    cells, ranges = layout_ranges(kind, tab)
    res = _execute(lambda: sheets.spreadsheets().values().batchGet(
        spreadsheetId=sid, ranges=ranges, valueRenderOption="FORMATTED_VALUE",
    ))
    check = check_value_ranges(kind, cells, res.get("valueRanges") or [])
    if check["problems"]:
        raise TemplateMismatch(
            f"{kind.title()} estimator copy {sid} does not match the layout this skill writes. "
            + "; ".join(check["problems"][:6])
            + ". Nothing was written and no read-back was taken. "
            "Point the template id at the current estimator, or update estimator_layout.py and the cell map together."
        )
    return {"version": check["version"], "newer_than_validated": check["newer"]}


def copy_verified(drive, sheets, template_id: str, title: str, parent_id: str, *, reuse: bool, kind: str, tab: str) -> tuple[dict, bool, dict]:
    """Copy (or reuse) an estimator and check its layout before any cell is written.

    A copy made by this call is trashed when the check fails, so an unfilled
    sheet does not stay behind under an estimate title.
    """
    copied, reused = copy_template(drive, template_id, title, parent_id, reuse=reuse)
    try:
        layout = verify_template(sheets, copied["id"], kind, tab)
    except TemplateMismatch:
        if not reused:
            try:
                trash_file(drive, copied["id"])
            except Exception:  # noqa: BLE001
                pass
        raise
    # A new copy replaces the previous one only after the new copy has the expected layout.
    if not reused:
        trash_same_title(drive, parent_id, title, copied["id"])
    return copied, reused, layout


def export_xlsx(drive, spreadsheet_id: str, dest: Path) -> Path:
    from googleapiclient.http import MediaIoBaseDownload

    assert_not_template(spreadsheet_id)
    req = drive.files().export_media(fileId=spreadsheet_id, mimeType=XLSX_MIME)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        for attempt in range(6):
            try:
                _, done = dl.next_chunk()
                break
            except (TimeoutError, ConnectionError, OSError):
                if attempt == 5:
                    raise
                time.sleep(min(20 * (2 ** attempt), 60))
    dest.write_bytes(buf.getvalue())
    seal_file(dest)
    return dest


def upload_csv_as_sheet(drive, local: Path, parent_id: str, title: str) -> dict:
    """Publish the es3 report CSV as a Google Sheet. A same-named Doc is replaced."""
    from googleapiclient.http import MediaFileUpload

    parent_id = require_customer_parent(parent_id)
    media = MediaFileUpload(str(local), mimetype="text/csv", resumable=False)
    existing = find_in_folder(drive, parent_id, title)
    if existing and existing.get("mimeType") != GSHEET_MIME:
        assert_not_template(existing["id"])
        _execute(lambda: drive.files().update(fileId=existing["id"], body={"trashed": True}))
        existing = None
    if existing:
        assert_not_template(existing["id"])
        meta = _execute(lambda: drive.files().update(fileId=existing["id"], media_body=media, fields="id,name,webViewLink"))
    else:
        meta = _execute(lambda: drive.files().create(
            body={"name": title, "parents": [parent_id], "mimeType": GSHEET_MIME},
            media_body=media,
            fields="id,name,webViewLink",
        ))
    trash_same_title(drive, parent_id, title, meta["id"])
    return {"id": meta["id"], "name": meta["name"], "url": meta.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{meta['id']}/edit"}


def write_search_sheet(drive, local_dir: Path, parent_id: str, title: str, result: dict) -> dict:
    """Write the estimator's own CSV report and publish it as a Google Sheet."""
    csv_text = result.get("report_csv") or ""
    if not csv_text:
        return {"error": result.get("error") or "estimator returned no report CSV"}
    path = local_dir / f"{title}.csv"
    seal_text(path, csv_text)
    uploaded = upload_csv_as_sheet(drive, path, parent_id, title)
    return {"local": str(path), "url": uploaded["url"], "spreadsheet_id": uploaded["id"]}


def upload_text_as_doc(drive, local: Path, parent_id: str, title: str) -> dict:
    from googleapiclient.http import MediaFileUpload

    parent_id = require_customer_parent(parent_id)
    media = MediaFileUpload(str(local), mimetype="text/plain", resumable=False)
    existing = find_in_folder(drive, parent_id, title)
    if existing:
        assert_not_template(existing["id"])
        meta = _execute(lambda: drive.files().update(fileId=existing["id"], media_body=media, fields="id,name,webViewLink"))
    else:
        meta = _execute(lambda: drive.files().create(body={"name": title, "parents": [parent_id], "mimeType": GDOC_MIME}, media_body=media, fields="id,name,webViewLink"))
    return {"id": meta["id"], "name": meta["name"], "url": meta.get("webViewLink") or f"https://docs.google.com/document/d/{meta['id']}/edit"}


def _execute(build, attempts: int = 6):
    """Run a Drive or Sheets request. Quota, 5xx, and a dropped connection wait and try again."""
    delay = 20
    for attempt in range(attempts):
        try:
            return build().execute()
        except (TimeoutError, ConnectionError):
            if attempt + 1 == attempts:
                raise
        except Exception as exc:
            # HttpError is identified by name so a unit test can run without the Google client.
            status = getattr(getattr(exc, "resp", None), "status", None)
            if type(exc).__name__ != "HttpError" or status not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise
        time.sleep(delay)
        delay = min(delay * 2, 60)
    raise RuntimeError("Sheets request was not sent")


def push_cells(sheets, sid: str, tab: str, cells: dict, *, allowed_text: set[str] | None = None) -> int:
    assert_not_template(sid)
    raw, entered = partition_cells(cells, allowed_text=allowed_text)

    def _batch(option: str, batch: list[dict]) -> None:
        data = [{"range": f"'{tab}'!{item['range']}", "values": item["values"]} for item in batch]
        _execute(lambda: sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=sid, body={"valueInputOption": option, "data": data},
        ))

    if raw:
        _batch("RAW", raw)
    if entered:
        _batch("USER_ENTERED", entered)
    return len(raw) + len(entered)


def read_cells(sheets, sid: str, tab: str, cells: dict[str, str]) -> dict:
    assert_not_template(sid)
    ranges = [cell_range(tab, a1) for a1 in cells.values()]
    res = _execute(lambda: sheets.spreadsheets().values().batchGet(
        spreadsheetId=sid, ranges=ranges, valueRenderOption="UNFORMATTED_VALUE",
    ))
    vals = [((vr.get("values") or [[None]])[0] or [None])[0] for vr in res.get("valueRanges", [])]
    return dict(zip(cells.keys(), vals))


SEC_ADDON_CELLS = [
    "G11", "G12", "G14", "G15", "G16", "G17", "G18", "G20",
    "G23", "G24", "G25", "G27", "G28", "G29", "G30", "G31", "G32", "G33", "G34",
]


def clear_security_addons(sheets, sid: str, tab: str) -> dict:
    """Turn template add-ons off on a copy. The golden template is not written."""
    assert_not_template(sid)
    try:
        meta = _execute(lambda: sheets.spreadsheets().get(
            spreadsheetId=sid,
            ranges=[f"'{tab}'!G11:G34"],
            fields="sheets(data(startRow,rowData(values(formattedValue,dataValidation))))",
        ))
    except Exception as ex:  # noqa: BLE001
        return {"error": str(ex)}
    blocks = ((meta.get("sheets") or [{}])[0].get("data") or [])
    if not blocks:
        return {}
    block = blocks[0]
    start = int(block.get("startRow") or 10)
    cells = {}
    for offset, row in enumerate(block.get("rowData") or []):
        cell_name = f"G{start + offset + 1}"
        if cell_name not in SEC_ADDON_CELLS:
            continue
        values = (row or {}).get("values") or [{}]
        cell = values[0] or {}
        off = addon_off_value(cell.get("dataValidation"), cell.get("formattedValue"))
        if off is not None:
            cells[cell_name] = off
    if cells:
        push_cells(sheets, sid, tab, cells)
    return cells

__all__ = [name for name in globals() if not name.startswith('__')]
