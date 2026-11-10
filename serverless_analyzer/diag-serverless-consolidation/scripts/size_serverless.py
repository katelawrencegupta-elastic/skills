#!/usr/bin/env python3
"""ECH > 50% rule: copy golden estimators into Drive folder <customer>_<date>, fill, read back.

Run with the Sheets venv python (google-api-python-client installed):

  ~/.config/serverless-analyzer/venv/bin/python size_serverless.py \
      --account-json serverless_analyzer/diag_artifacts/Acme_2026-11-02/Acme_2026-11-02_account.json

Produces a Drive folder <customer>_<date>. The account PDF stays in that folder.
Measured-retention consolidation PDFs go in full_retention/. 30-day PDFs go in 30d/.
Estimate sheets go in generated_estimates/full_retention or generated_estimates/30d.
Compression-applied sheets go in generated_estimates/compression_applied/full_retention or .../30d.
Deployment-plan sheets stay in generated_estimates/plan_sheets.
Each solution with a workload gets four sheets. A solution with no workload is skipped.
  <solution>_full retention_bulk
  <solution>_30d_bulk
  <solution>_full retention_compression-applied
  <solution>_30d_compression-applied
Observability and Security write GB/day, and the 30-day sheet writes 1 month.
Search disk on the 30-day sheet is measured store × (1 / written months).
Search disk on a compression-applied sheet is measured store × 0.60, and the
30-day compression sheet applies both. Ingest share stays put. Query rate does not scale.
Sheet titles are
  <customer>_<date>_<search|observability|security>_<full retention|30d>_<bulk|compression-applied>_serverless_estimate
Local .xlsx export is off unless `--xlsx` is passed. Search CSV reports still use that title. An estimate already in generated_estimates is moved into the retention folder its name calls for, on disk and on Drive.
  sizing.json ; account JSON `sizing.estimators` updated

Never mutates the golden templates.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from skill_paths import find_skill, scripts_dir  # noqa: E402

ORG_SKILL = find_skill("k360-org-report", HERE)
_account_root = find_skill("k360-account-analysis", HERE)
K360_SCRIPTS = scripts_dir(_account_root)
sys.path.insert(0, str(K360_SCRIPTS))
sys.path.insert(0, str(ORG_SKILL / "scripts"))
from privacy import seal_dir, seal_file, seal_text  # noqa: E402
from quote_checks import addon_off_value, g26_gate, money as sheet_money  # noqa: E402
try:
    from sheets_auth import get_creds  # noqa: E402
except ImportError as e:  # pragma: no cover
    raise SystemExit(f"sheets_auth.py not found under {ORG_SKILL}/scripts — set K360_ORG_SKILL_DIR") from e

# Golden templates (user-specified; copied, never edited in place):
#   Observability → https://docs.google.com/spreadsheets/d/1bu-qjVIab-rbWb7zvCEq6QEoLdKpi0vfw3yDj9_VJuE/edit?usp=drive_link
#   Security      → https://docs.google.com/spreadsheets/d/1b91Uoa2e4vBrep_7I4RoY_8Mr19poqaUgWl7LqhIOqY/edit?usp=drive_link
# Override with O11Y_SHEET_ID / SECURITY_SHEET_ID or --o11y-id / --security-id (ID or full URL).
DEFAULT_O11Y = "1bu-qjVIab-rbWb7zvCEq6QEoLdKpi0vfw3yDj9_VJuE"
DEFAULT_SECURITY = "1b91Uoa2e4vBrep_7I4RoY_8Mr19poqaUgWl7LqhIOqY"


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
    "total_raw_gb_day": "G26",
    "metered_gb_day": "G27",
    "monthly_steady_state": "D32",
    "annual_year1": "E32",
    "annual_steady_state": "F32",
}
SEC_READBACK = {
    "metered_gb_day": "G5",
    "monthly_steady_state": "E74",
    "annual_year1": "D36",
    "annual_year2": "E36",
    "annual_year3": "F36",
    "annual_steady_state": "E36",
    "cps_addon_monthly": "E73",
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
    found = drive.files().list(q=q, spaces="drive", fields="files(id,name,webViewLink)", pageSize=5).execute().get("files") or []
    if found:
        f = found[0]
        return {"id": f["id"], "name": f["name"], "url": f.get("webViewLink") or f"https://drive.google.com/drive/folders/{f['id']}"}
    body = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    meta = drive.files().create(body=body, fields="id,name,webViewLink").execute()
    return {"id": meta["id"], "name": meta["name"], "url": meta.get("webViewLink") or f"https://drive.google.com/drive/folders/{meta['id']}"}


def find_in_folder(drive, parent_id: str, name: str) -> dict | None:
    parent_id = require_customer_parent(parent_id)
    q = f"'{parent_id}' in parents and name = '{_esc(name)}' and trashed = false"
    files = _execute(lambda: drive.files().list(q=q, spaces="drive", fields="files(id,name,webViewLink,mimeType)", pageSize=5)).get("files") or []
    return files[0] if files else None


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


def export_xlsx(drive, spreadsheet_id: str, dest: Path) -> Path:
    from googleapiclient.http import MediaIoBaseDownload

    assert_not_template(spreadsheet_id)
    req = drive.files().export_media(fileId=spreadsheet_id, mimeType=XLSX_MIME)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
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
        drive.files().update(fileId=existing["id"], body={"trashed": True}).execute()
        existing = None
    if existing:
        assert_not_template(existing["id"])
        meta = drive.files().update(fileId=existing["id"], media_body=media, fields="id,name,webViewLink").execute()
    else:
        meta = drive.files().create(
            body={"name": title, "parents": [parent_id], "mimeType": GSHEET_MIME},
            media_body=media,
            fields="id,name,webViewLink",
        ).execute()
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
        meta = drive.files().update(fileId=existing["id"], media_body=media, fields="id,name,webViewLink").execute()
    else:
        meta = drive.files().create(body={"name": title, "parents": [parent_id], "mimeType": GDOC_MIME}, media_body=media, fields="id,name,webViewLink").execute()
    return {"id": meta["id"], "name": meta["name"], "url": meta.get("webViewLink") or f"https://docs.google.com/document/d/{meta['id']}/edit"}


def _execute(build, attempts: int = 6):
    """Run a Sheets request. Quota (429) and a brief outage (500, 502, 503) wait and try again."""
    from googleapiclient.errors import HttpError

    delay = 20
    for attempt in range(attempts):
        try:
            return build().execute()
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status not in (429, 500, 502, 503) or attempt + 1 == attempts:
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
    ranges = [f"'{tab}'!{a1}" for a1 in cells.values()]
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


def observability_input_cells(acc: dict, support: str, region, retention: int, daily: float, logs=None, metrics=None) -> dict:
    return {
        "G4": (acc.get("sizing") or {}).get("feature_tier") or "Complete",
        "G5": support,
        "G8": region,
        "G11": retention,
        "G14": "No",
        "G18": 0,
        "G21": round(float(daily) * 0.90, 2) if logs is None else logs,
        "G22": "0%",
        "G23": round(float(daily) * 0.10, 2) if metrics is None else metrics,
        "G24": 0,
        "G25": "100%",
    }


def security_input_cells(acc: dict, support: str, region, retention: int, daily: float) -> dict:
    return {
        "G4": round(float(daily), 2),
        "G6": retention,
        "G7": region,
        "G8": (acc.get("sizing") or {}).get("feature_tier") or "Complete",
        "G9": 0,
        "G21": support,
    }


def region_options(sheets, sid: str) -> list[str]:
    """All 'PROVIDER: region (Name)' dropdown strings in Estimator_Reference (version-agnostic)."""
    assert_not_template(sid)
    res = _execute(lambda: sheets.spreadsheets().values().get(spreadsheetId=sid, range=f"'{REF_TAB}'!A1:M450"))
    opts: list[str] = []
    for row in res.get("values", []):
        for cell in row:
            if isinstance(cell, str) and _REGION_OPT.match(cell.strip()) and cell.strip() not in opts:
                opts.append(cell.strip())
    return opts


# ----------------------------------------------------------------------------- region match
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _azure_aliases(region: str) -> set[str]:
    """eastus2 ⇄ us-east-2, westeurope ⇄ europe-west, …"""
    r = _norm(region)
    out = {r}
    m = re.match(r"^(east|west|north|south|central|southeast|northeast)(us|europe|asia|india|australia|uk|japan|brazil|canada|germany|france|norway|sweden|switzerland|uae|korea|africa|qatar|poland|italy|israel|spain)(\d*)$", r)
    if m:
        out.add(f"{m.group(2)}{m.group(1)}{m.group(3)}")
    m = re.match(r"^(us|europe|asia|india|australia|uk|japan|brazil|canada|germany|france|norway|sweden|switzerland|uae|korea|africa|qatar|poland|italy|israel|spain)(east|west|north|south|central|southeast|northeast)(\d*)$", r)
    if m:
        out.add(f"{m.group(2)}{m.group(1)}{m.group(3)}")
    return out


def es3_python() -> Path | None:
    env = os.environ.get("ES3_ESTIMATOR_PY")
    candidates = [Path(env)] if env else []
    candidates.append(Path.home() / ".config" / "es3-estimator" / "venv" / "bin" / "python")
    root = os.environ.get("ES3_ESTIMATOR_DIR")
    if root:
        candidates.append(Path(root) / "estimator" / "venv" / "bin" / "python")
        candidates.append(Path(root) / "venv" / "bin" / "python")
    candidates.append(Path.home() / "Documents" / "GItHub" / "es3-estimator-main" / "estimator" / "venv" / "bin" / "python")
    for path in candidates:
        if path.is_file():
            return path
    return None


def _md_cell(value) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def search_markdown(customer: str, when: str, se: dict, provider, region, result: dict) -> str:
    wl = (se.get("workload") or {}).get("search") or {}
    ing = (se.get("workload") or {}).get("ingest") or {}
    lines = [
        f"# {customer} — Search Serverless estimate — {when}",
        "",
        "Priced with the es3-estimator library (`estimate_serverless_pricing`), the same function the Jupyter notebook runs.",
        "On-demand is the quotable profile. Performant and high availability sit beside it; the notebook's own notes say those two can be distorted by training data.",
        "",
        "## Consolidated Search slice (from K360)",
        f"- Deployments: {se.get('deployments')}",
        f"- ECU (lookback): {se.get('ecu_lookback', 0):,.0f} · monthly avg {se.get('ecu_monthly_avg', 0):,.0f}",
        f"- Stored GB: {se.get('stored_gb', 0):,.0f}" + (" (estimated from ECU share — no telemetry)" if se.get("gb_estimated_from_ecu_share") else ""),
        f"- Provider / region: {provider} / {region}",
        "",
        "## Workload sent to the estimator",
        f"- Peak QPS {wl.get('peak', {}).get('qps')} · {wl.get('peak', {}).get('complexity_ms')} ms · {wl.get('peak', {}).get('pct_hours_mo')}% of hours",
        f"- Non-peak QPS {wl.get('nonpeak', {}).get('qps')} · {wl.get('nonpeak', {}).get('complexity_ms')} ms · {wl.get('nonpeak', {}).get('pct_hours_mo')}% of hours",
        f"- Idle {wl.get('idle_pct_hours_mo')}% of hours (billed by the estimator as the hours left in a 730 h month)",
        f"- Ingest {ing.get('docs_per_sec')} docs/s · avg doc {ing.get('avg_doc_bytes')} B · active {ing.get('active_hours_mo')} h/mo",
        "",
    ]
    inputs = result.get("inputs") or {}
    if inputs:
        lines.append(f"- Mapped to {inputs.get('provider')} {inputs.get('region')}, support {inputs.get('support_level')}, stored {inputs.get('stored_gb')} GB")
        lines.append("")
    profiles = result.get("profiles") or []
    if profiles:
        header = ["Search Power", "Total $/mo", "Search $/mo", "Ingest $/mo", "Storage $/mo", "Support $/mo", "Search VCU/h", "Idle VCU/h", "Limitation"]
        keys = ["Search Power Setting", "Total $/month", "Search $/month", "Ingest $/month", "Storage $/month", "Support $/month", "Provisioned Search VCU/h", "Idle Search VCU/h", "Profile Limitation"]
        lines.append("## Profiles")
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join("---" for _ in header) + " |")
        for row in profiles:
            lines.append("| " + " | ".join(_md_cell(row.get(key)) for key in keys) + " |")
        lines.append("")
    assumptions = result.get("assumptions") or []
    if assumptions:
        lines.append("## Assumptions")
        lines.extend(f"- {item}" for item in assumptions)
        lines.append("")
    if result.get("error"):
        lines.append(f"Estimator error: {result['error']}")
        lines.append("")
    return "\n".join(lines)


def run_es3(payload: dict) -> dict:
    py = es3_python()
    if py is None:
        return {
            "status": "error",
            "error": "es3-estimator venv missing. Create ~/.config/es3-estimator/venv and pip install pandas numpy joblib 'scikit-learn==1.7.2'.",
        }
    script = Path(__file__).resolve().parent / "es3_estimate.py"
    proc = subprocess.run(
        [str(py), str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=180,
    )
    raw = (proc.stdout or "").strip()
    try:
        out = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        out = {"status": "error", "error": f"estimator stdout was not JSON (exit {proc.returncode}): {raw[:400]}"}
    if proc.returncode != 0 and not out.get("error"):
        err = (proc.stderr or "").strip().splitlines()
        out["error"] = err[-1] if err else f"exit {proc.returncode}"
        out["status"] = out.get("status") or "error"
    if proc.stderr and out.get("status") == "error":
        out["stderr_tail"] = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
    return out


def match_region(provider: str | None, region: str | None, options: list[str]) -> tuple[str | None, str]:
    """→ (option string, how). how ∈ exact | alias | provider_default | none."""
    prov = (provider or "").upper()
    cands = [o for o in options if o.upper().startswith(prov)] if prov else options
    if not cands:
        return (options[0] if options else None), "none"
    want = _azure_aliases(region or "") if region else set()
    for o in cands:
        m = _REGION_OPT.match(o)
        if not m:
            continue
        tok = _norm(m.group(2))
        if tok in want:
            return o, "exact" if _norm(region) == tok else "alias"
    # loose: strip trailing digits ("us-east-1" vs "us-east1")
    want_loose = {re.sub(r"\d+$", "", w) for w in want}
    for o in cands:
        m = _REGION_OPT.match(o)
        if m and re.sub(r"\d+$", "", _norm(m.group(2))) in want_loose:
            return o, "alias"
    return cands[0], "provider_default"


DAYS_PER_MONTH = 30.416


def _suffix(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    return text if text.startswith("_") else f"_{text}"


ESTIMATE_FOLDER_NAME = "generated_estimates"
PLAN_FOLDER_NAME = "plan_sheets"
COMPRESSION_FOLDER_NAME = "compression_applied"
COMPRESSION_NAME_MARK = "compression-applied"
FULL_RETENTION_FOLDER = "full_retention"
THIRTY_DAY_FOLDER = "30d"
ESTIMATE_NAME_MARK = "serverless_estimate"


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
    """Parent directory for estimate sheets. Retention, compression, and plan sheets are children of it."""
    _estimate_parts(name_suffix)
    return ESTIMATE_FOLDER_NAME


def estimate_dir_parts(name_suffix: str | None) -> tuple[str, ...]:
    """Path under generated_estimates for this sizing.

    Bulk and k360 sheets go in full_retention or 30d.
    Compression-applied sheets go in compression_applied/full_retention or compression_applied/30d.
    """
    retention, basis = _estimate_parts(name_suffix)
    retention_dir = THIRTY_DAY_FOLDER if retention == "30d" else FULL_RETENTION_FOLDER
    if basis == "compression-applied":
        return (COMPRESSION_FOLDER_NAME, retention_dir)
    return (retention_dir,)


def estimate_subfolder(name_suffix: str | None) -> str:
    """Relative directory under generated_estimates for this sizing."""
    return "/".join(estimate_dir_parts(name_suffix))


def is_compression_estimate_name(name: str) -> bool:
    return COMPRESSION_NAME_MARK in (name or "")


def is_estimate_artifact_name(name: str) -> bool:
    return ESTIMATE_NAME_MARK in (name or "")


def retention_dir_for_filename(name: str) -> str:
    """30d when the sheet title carries that token. Otherwise full retention."""
    if "_30d_" in (name or "") or (name or "").endswith("_30d"):
        return THIRTY_DAY_FOLDER
    return FULL_RETENTION_FOLDER


def estimate_dest_parts_for_filename(name: str) -> tuple[str, ...] | None:
    """Where an existing estimate file belongs. Plan sheets and other files stay put."""
    if not is_estimate_artifact_name(name):
        return None
    retention = retention_dir_for_filename(name)
    if is_compression_estimate_name(name):
        return (COMPRESSION_FOLDER_NAME, retention)
    return (retention,)


def relocate_local_estimates(parent: Path) -> list[str]:
    """Move estimate files under generated_estimates into their retention folders."""
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
        if target.exists():
            target.unlink()
        item.rename(target)
        seal_file(target)
        moved.append(item.name)
    return moved


def _drive_child_files(drive, parent_id: str) -> list[dict]:
    parent_id = require_customer_parent(parent_id)
    files: list[dict] = []
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
        found = _execute(_list)
        for item in found.get("files") or []:
            if item.get("mimeType") == FOLDER_MIME:
                continue
            files.append(item)
        token = found.get("nextPageToken")
        if not token:
            return files


def _drive_folder_id(drive, estimates_id: str, parts: tuple[str, ...]) -> str | None:
    current = estimates_id
    for name in parts:
        found = find_in_folder(drive, current, name)
        if not found or found.get("mimeType") != FOLDER_MIME:
            return None
        current = found["id"]
    return current


def relocate_drive_estimates(drive, estimates_id: str) -> list[str]:
    """Move estimate files under generated_estimates into their retention folders."""
    estimates_id = require_customer_parent(estimates_id)
    candidates = [
        (),
        (COMPRESSION_FOLDER_NAME,),
        (FULL_RETENTION_FOLDER,),
        (THIRTY_DAY_FOLDER,),
        (COMPRESSION_FOLDER_NAME, FULL_RETENTION_FOLDER),
        (COMPRESSION_FOLDER_NAME, THIRTY_DAY_FOLDER),
    ]
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
    return moved


def open_estimate_place(drive, root_id: str, local_dir: Path, name_suffix: str | None) -> tuple[dict, Path, dict, list[str]]:
    """Drive folder and local directory for this sizing, plus the generated_estimates Drive folder.

    Sheets land in full_retention or 30d. Compression-applied sheets use that split
    under compression_applied. Files already in generated_estimates are moved to match.
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
        if dep.get("platform") == "serverless" or dep.get("ingest_source") == "inactive":
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

    <customer>_<date>_<solution>_<full retention|30d>_<bulk|compression-applied>_<region>_serverless_estimate
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

    A remainder under 0.001 month is the 30.416 truncation, not another month.
    365 / 30.416 is 12.000263 and stays 12. A real extra day still rounds up.
    """
    if months is None or months == "":
        return None
    try:
        value = float(months)
    except (TypeError, ValueError):
        return None
    whole = math.floor(value)
    if whole >= 1 and 0 <= (value - whole) < 0.001:
        value = float(whole)
    value = min(36.0, max(1.0, value))
    return int(min(36, max(1, math.ceil(value - 1e-9))))


def _months_for_sheet(block: dict, force: int | None) -> int | None:
    measured = _sheet_retention((block or {}).get("retention_months"))
    if measured is None:
        return None
    if force is not None:
        return int(min(36, max(1, force)))
    return measured


def _search_volume_scale(block: dict, force_months: int | None, name_suffix: str | None) -> float:
    """Scale Search disk for a 30-day read and for a compression-applied read.

    Full-retention bulk is 1. A forced month shrinks disk by that month divided
    by the written month, so 30 days is 1 / written months. Compression-applied
    multiplies by 0.60. Callers scale org store by the same factor so the
    ingest share stays put. Query rate is not scaled here.
    """
    from admin_diags import COMPRESSION_RATIO

    scale = 1.0
    written = _sheet_retention((block or {}).get("retention_months"))
    if force_months is not None and written:
        scale *= float(int(min(36, max(1, force_months)))) / float(written)
    _retention, basis = _estimate_parts(name_suffix)
    if basis == "compression-applied":
        scale *= COMPRESSION_RATIO
    return scale


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


def _scale_qps(workload: dict, share: float) -> dict:
    wl = json.loads(json.dumps(workload or {}))
    search = wl.get("search") or {}
    for band in ("peak", "nonpeak"):
        band_row = dict(search.get(band) or {})
        if band_row.get("qps") is not None:
            band_row["qps"] = round(float(band_row["qps"]) * share, 4)
        search[band] = band_row
    wl["search"] = search
    return wl


def _fill_regional_job(job: dict, creds) -> dict:
    """One regional project. The worker builds its own Sheets and Drive clients."""
    sheets, drive = services_from(creds)
    sol = job["solution"]
    if sol == "search":
        result = run_es3({
            "stored_gb": job["stored_gb"],
            "org_stored_gb": job["org_gb"],
            "provider": job["provider"],
            "region": job["region"],
            "support_tier": job["support"],
            "workload": job["workload"],
        })
        published = {}
        try:
            published = write_search_sheet(drive, Path(job["local_dir"]), job["parent_id"], job["title"], result)
        except Exception as ex:  # noqa: BLE001
            published = {"error": str(ex)}
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
        if published.get("error"):
            entry["error"] = published["error"]
        return entry
    copied, reused = copy_template(drive, job["template_id"], job["title"], job["parent_id"], reuse=job["reuse"])
    opts = region_options(sheets, copied["id"])
    reg, how = match_region(job["provider"], job["region"], opts)
    if sol == "security":
        clear_security_addons(sheets, copied["id"], job["tab"])
    if sol == "observability":
        cells = observability_input_cells(
            {"sizing": {"feature_tier": job.get("feature_tier")}},
            job["support"], reg, int(job["retention"]), job["daily"],
        )
    else:
        cells = security_input_cells(
            {"sizing": {"feature_tier": job.get("feature_tier")}},
            job["support"], reg, int(job["retention"]), job["daily"],
        )
    n = push_cells(sheets, copied["id"], job["tab"], cells, allowed_text=set(opts))
    rb = read_cells(sheets, copied["id"], job["tab"], job["readback"])
    entry = {
        "solution": sol, "region": job["region"], "title": copied["name"], "stored_gb": job["stored_gb"],
        "spreadsheet_id": copied["id"], "url": copied["url"], "reused_existing_copy": reused,
        "cells": cells, "cells_written": n, "region_match": f"{reg} ({how})",
        "readback": rb, "status": "filled", "daily_raw_gb": round(job["daily"], 2),
    }
    if sol == "observability":
        entry["g26_gate"] = g26_gate(job["daily"], rb.get("total_raw_gb_day"))
        if not entry["g26_gate"]["accepted"]:
            entry["readback_rejected"] = "G26 differs from the written bulk by more than 0.51 GB/day"
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
    from consolidation import REGION_ESTIMATE_MIN_USD, projected_region_monthly

    groups = ((acc.get("consolidation") or {}).get("architecture") or {}).get("groups") or []
    by_sol: dict[str, list] = {}
    for g in groups:
        by_sol.setdefault(g["solution"], []).append(g)
    con = (acc.get("sizing") or {}).get("consolidated") or {}
    org_gb = float((acc.get("storage") or {}).get("data_stored_gb") or 0)
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
        total_gb = sum(float(r.get("stored_gb") or 0) for r in rows) or 0.0
        for g in rows:
            gb = float(g.get("stored_gb") or 0)
            if sol == "search" and gb <= 0:
                continue
            if gb <= 0 and not (g.get("daily_raw_gb") or 0):
                continue
            source_months = g.get("retention_months") if g.get("retention_months") is not None else (con.get(sol) or {}).get("retention_months")
            projected = projected_region_monthly(
                sol,
                stored_gb=gb,
                daily_raw_gb=g.get("daily_raw_gb"),
                retention_months=source_months,
                solution_stored_gb=total_gb,
                workload=workload if sol == "search" else None,
            )
            if projected <= REGION_ESTIMATE_MIN_USD:
                continue
            region = g["region"]
            title = estimate_title(key, sol, name_suffix, region)
            retention = _months_for_sheet({"retention_months": source_months}, force_months)
            if sol != "search" and retention is None:
                continue
            if g.get("daily_raw_gb") is not None:
                daily = float(g["daily_raw_gb"])
            else:
                daily = gb / (float(retention or 3) * DAYS_PER_MONTH) if (retention or 3) else 0.0
            job = {
                "solution": sol,
                "region": region,
                "title": title,
                "stored_gb": gb,
                "parent_id": folder["id"],
                "local_dir": str(local_dir),
                "provider": provider,
                "support": support,
                "feature_tier": feature_tier,
                "skip_xlsx": skip_xlsx,
                "reuse": not fresh,
            }
            if sol == "search":
                share = gb / total_gb if total_gb else 0.0
                scale = _search_volume_scale({"retention_months": source_months}, force_months, name_suffix)
                job["stored_gb"] = gb * scale
                job["org_gb"] = org_gb * scale
                job["store_scale"] = scale
                job["workload"] = _scale_qps(workload, share)
            elif sol == "observability":
                job.update({
                    "template_id": o11y_id, "tab": O11Y_TAB, "readback": O11Y_READBACK,
                    "retention": int(retention), "daily": daily,
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


def _diag_inputs():
    import diag_inputs
    return diag_inputs


def assemble_plan_bars(specs: list[dict], fills: list[dict]) -> dict:
    """Join sheet read-backs onto plan bars. Input order is the cut order.

    One failed fill clears every bar. The 100% bar reuses the headline and has no sheet.
    """
    for fill in fills:
        if fill.get("error"):
            return {"bars": [], "error": str(fill["error"])}
    bars = []
    for spec in specs:
        if spec.get("reused_headline"):
            bars.append({
                "cut_pct": spec["cut_pct"],
                "label": spec["label"],
                "projects": spec.get("projects", 1),
                "monthly": None,
                "reused_headline": True,
            })
            continue
        monthly = 0.0
        priced = []
        for group in spec.get("groups") or []:
            if group.get("sheet") is None:
                priced.append({"names": group.get("names") or [], "monthly": 0.0, "bulk_gb_day": 0})
                continue
            fill = fills[group["sheet"]]
            monthly += float(fill["monthly"])
            priced.append({
                "names": group.get("names") or [],
                "monthly": round(float(fill["monthly"]), 2),
                "bulk_gb_day": group.get("bulk_gb_day"),
                "months_written": group.get("months_written"),
                "url": fill.get("url"),
            })
        bars.append({
            "cut_pct": spec["cut_pct"],
            "label": spec["label"],
            "projects": spec["projects"],
            "monthly": round(monthly, 2),
            "groups": priced,
        })
    return {"bars": bars}


def plan_sheet_jobs(rows: list[dict], *, run_key: str, name_suffix: str, force_months: int | None, o11y_id: str, parent_id: str, reuse: bool, provider, region_name, support: str, feature_tier, floor: int = 1) -> tuple[list[dict], list[dict], str | None]:
    """Bar specs plus the sheet jobs they point at. A missing month fails before any copy."""
    diag = _diag_inputs()
    specs: list[dict] = []
    jobs: list[dict] = []
    shared: dict[int, list] = {}
    for plan in diag.deployment_count_plans(rows, floor):
        pct = plan["cut_pct"]
        if pct <= 0 and plan["projects"] <= 1:
            specs.append({"cut_pct": 0, "label": "0%", "projects": 1, "reused_headline": True, "groups": []})
            continue
        packed = shared.get(plan["projects"])
        if packed is not None:
            specs.append({"cut_pct": pct, "label": plan["label"], "projects": plan["projects"], "groups": packed})
            continue
        groups = []
        for index, group in enumerate(plan["groups"], start=1):
            summary = diag.summarize(group, force_months)
            bulk = float(summary.get("bulk_gb_day") or 0)
            if bulk <= 0:
                groups.append({"sheet": None, "names": summary.get("names") or [], "bulk_gb_day": 0})
                continue
            written = force_months or summary.get("months_written")
            if not written:
                return [], [], "Observability deployment-count plan was not priced: a project has bulk and no measured retention."
            slug = re.sub(r"[\\/:\s]+", "-", str(region_name or "").strip()).strip("-")
            title = f"{run_key}_Observability_plan_{pct}pct_{plan['projects']}projects_{index}"
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
                "template_id": o11y_id,
                "title": title,
                "parent_id": parent_id,
                "reuse": reuse,
                "provider": provider,
                "region_name": region_name,
                "support": support,
                "feature_tier": feature_tier,
                "months_written": int(written),
                "bulk": bulk,
            })
        shared[plan["projects"]] = groups
        specs.append({"cut_pct": pct, "label": plan["label"], "projects": plan["projects"], "groups": groups})
    return specs, jobs, None


def fill_plan_sheet(job: dict, creds) -> dict:
    """Copy one plan workbook, write it, and read D32. A 429 or 503 is retried inside the request."""
    sheets, drive = services_from(creds)
    try:
        copied, _reused = copy_template(drive, job["template_id"], job["title"], job["parent_id"], reuse=job["reuse"])
        opts = region_options(sheets, copied["id"])
        reg, _how = match_region(job["provider"], job["region_name"], opts)
        cells = observability_input_cells(
            {"sizing": {"feature_tier": job.get("feature_tier")}},
            job["support"], reg, int(job["months_written"]), float(job["bulk"]),
        )
        push_cells(sheets, copied["id"], O11Y_TAB, cells, allowed_text=set(opts))
        rb = read_cells(sheets, copied["id"], O11Y_TAB, O11Y_READBACK)
        gate = g26_gate(float(job["bulk"]), rb.get("total_raw_gb_day"))
        if not gate["accepted"]:
            return {"error": (
                "Observability deployment-count plan was not priced: "
                f"{job['title']} G26 is {gate['sheet_raw_gb_day']} GB/day against {gate['written_bulk']} written."
            )}
        price = sheet_money(rb.get("monthly_steady_state"))
        if price is None:
            return {"error": f"Observability deployment-count plan was not priced: {job['title']} has no D32 read-back."}
        return {"monthly": round(price, 2), "url": copied.get("url")}
    except Exception as ex:  # noqa: BLE001
        return {"error": f"Observability deployment-count plan was not priced: {ex}"}


def size_deployment_plan(acc: dict, sheets, drive, folder: dict, *, o11y_id: str, support: str, provider, force_months: int | None, name_suffix: str, fresh: bool) -> dict:
    """Price the five Observability deployment-count bars. 100% reuses the headline sheet.

    The sheet copies share one pool of at most four workers. Passes stay sequential.
    """
    del sheets, drive
    rows = []
    for dep in (acc.get("solutions") or {}).get("by_deployment") or []:
        if dep.get("platform") == "serverless" or dep.get("ingest_source") == "inactive":
            continue
        if (dep.get("solution") or "search") != "observability":
            continue
        rows.append({
            "name": dep.get("name") or "",
            "bulk_gb_day": dep.get("daily_raw_gb") or 0,
            "store_gb": dep.get("gb") or 0,
            "retention_days": dep.get("retention_days"),
        })
    if not rows:
        return {"bars": [], "error": "Observability deployment-count plan was not priced: no active observability deployments."}
    from consolidation import logical_project_floor
    from diag_inputs import MINIMUM_PLAN_PROJECTS
    floor = max(MINIMUM_PLAN_PROJECTS, logical_project_floor(acc))
    specs, jobs, error = plan_sheet_jobs(
        rows,
        run_key=acc["meta"]["run_key"],
        name_suffix=name_suffix,
        force_months=force_months,
        o11y_id=o11y_id,
        parent_id=folder["id"],
        reuse=not fresh,
        provider=provider,
        region_name=(acc.get("sizing") or {}).get("region"),
        support=support,
        feature_tier=(acc.get("sizing") or {}).get("feature_tier"),
        floor=floor,
    )
    if error:
        return {"bars": [], "error": error}
    if not jobs:
        built = assemble_plan_bars(specs, [])
    else:
        try:
            creds = load_sheet_creds()
            fills = map_in_order(lambda job: fill_plan_sheet(job, creds), jobs)
        except Exception as ex:  # noqa: BLE001
            return {"bars": [], "error": f"Observability deployment-count plan was not priced: {ex}"}
        built = assemble_plan_bars(specs, fills)
    if not built.get("error"):
        built["floor"] = floor
    return built


# ----------------------------------------------------------------------------- main
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
    ap.add_argument("--search-sheets", action="store_true", help="Republish Search estimator output as Google Sheets and leave the other copies in place")
    ap.add_argument("--name-suffix", default="", help="Appended to estimator titles so a second sizing does not overwrite the first")
    ap.add_argument("--sizing-json", default=None, help="Filename for the sizing sidecar (default sizing.json)")
    ap.add_argument("--force-months", type=int, default=None, help="Write this whole month when a measured month already exists")
    ap.add_argument("--skip-search", action="store_true", help="Keep the Search estimator already on the account JSON")
    ap.add_argument("--deployment-plan", action="store_true", help="Price the Observability 0/25/50/75/100%% deployment-count bars")
    args = ap.parse_args()
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

    sheets, drive = services()
    root = ensure_drive_folder(drive, key)
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
            store_scale = _search_volume_scale(se, args.force_months, suffix)
            result = run_es3({
                "stored_gb": float(se.get("stored_gb") or 0) * store_scale,
                "org_stored_gb": float((acc.get("storage") or {}).get("data_stored_gb") or 0) * store_scale,
                "provider": provider,
                "region": search_scope or region,
                "support_tier": support,
                "workload": se.get("workload") or acc.get("workload") or {},
            }) if (se.get("stored_gb") or 0) > 0 else {}
            published = write_search_sheet(drive, estimate_dir, folder["id"], title_se, result) if result.get("report_csv") else {"error": result.get("error") or "no report"}
            entry = dict(estimators.get("search") or {"kind": "search", "title": title_se})
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
            lines.append(f"Search → {entry.get('url') or entry.get('error')}  (on-demand {rb.get('monthly_steady_state')})")
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
            acc, sheets, drive, plan_folder, o11y_id=args.o11y_id, support=support, provider=provider,
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
    estimators: dict = {}

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
        estimators["observability"] = {"kind": "observability", "status": "skipped — retention was not measured"}
        lines.append("Observability → skipped (retention was not measured)")
    else:
        title_o = estimate_title(key, "observability", suffix, o_scope)
        o_months = _months_for_sheet(o, args.force_months)
        copied, reused = copy_template(drive, args.o11y_id, title_o, folder["id"], reuse=not args.fresh)
        opts = region_options(sheets, copied["id"])
        reg, how = match_region(provider, o_scope or region, opts)
        daily = float(o.get("daily_raw_gb") or 0)
        cells = observability_input_cells(
            acc, support, reg, o_months, daily, logs=o.get("logs_gb_day"), metrics=o.get("metrics_gb_day"),
        )
        n = push_cells(sheets, copied["id"], O11Y_TAB, cells, allowed_text=set(opts))
        rb = read_cells(sheets, copied["id"], O11Y_TAB, O11Y_READBACK)
        entry = {"kind": "observability", "title": copied["name"], "spreadsheet_id": copied["id"], "url": copied["url"], "template_id": args.o11y_id, "reused_existing_copy": reused, "tab": O11Y_TAB, "cells": cells, "cells_written": n, "region_match": f"{reg} ({how})", "readback": rb, "status": "filled"}
        entry["g26_gate"] = g26_gate(daily, rb.get("total_raw_gb_day"))
        if not entry["g26_gate"]["accepted"]:
            entry["readback_rejected"] = "G26 differs from the written bulk by more than 0.51 GB/day"
        if not args.skip_xlsx:
            try:
                x = export_xlsx(drive, copied["id"], estimate_dir / f"{title_o}.xlsx")
                entry["local_xlsx"] = str(x)
            except Exception as ex:  # noqa: BLE001
                entry["local_xlsx_error"] = str(ex)
        estimators["observability"] = entry
        lines.append(f"Observability → {copied['url']}  ({n} cells; Y1 {rb.get('annual_year1')}, monthly {rb.get('monthly_steady_state')}; region {reg} [{how}])")

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
        estimators["security"] = {"kind": "security", "status": "skipped — retention was not measured"}
        lines.append("Security → skipped (retention was not measured)")
    else:
        title_s = estimate_title(key, "security", suffix, s_scope)
        s_months = _months_for_sheet(s, args.force_months)
        copied, reused = copy_template(drive, args.security_id, title_s, folder["id"], reuse=not args.fresh)
        opts = region_options(sheets, copied["id"])
        reg, how = match_region(provider, s_scope or region, opts)
        clear_security_addons(sheets, copied["id"], SEC_TAB)
        cells = security_input_cells(acc, support, reg, s_months, float(s.get("siem_gb_day") if s.get("siem_gb_day") is not None else s.get("daily_raw_gb") or 0))
        n = push_cells(sheets, copied["id"], SEC_TAB, cells, allowed_text=set(opts))
        rb = read_cells(sheets, copied["id"], SEC_TAB, SEC_READBACK)
        entry = {"kind": "security", "title": copied["name"], "spreadsheet_id": copied["id"], "url": copied["url"], "template_id": args.security_id, "reused_existing_copy": reused, "tab": SEC_TAB, "cells": cells, "cells_written": n, "region_match": f"{reg} ({how})", "readback": rb, "status": "filled"}
        if not args.skip_xlsx:
            try:
                x = export_xlsx(drive, copied["id"], estimate_dir / f"{title_s}.xlsx")
                entry["local_xlsx"] = str(x)
            except Exception as ex:  # noqa: BLE001
                entry["local_xlsx_error"] = str(ex)
        estimators["security"] = entry
        lines.append(f"Security → {copied['url']}  ({n} cells; Y1 {rb.get('annual_year1')}; region {reg} [{how}])")

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
        store_scale = _search_volume_scale(se, args.force_months, suffix)
        if has_search and (se.get("stored_gb") or 0) > 0:
            result = run_es3({
                "stored_gb": float(se.get("stored_gb") or 0) * store_scale,
                "org_stored_gb": float((acc.get("storage") or {}).get("data_stored_gb") or 0) * store_scale,
                "provider": provider,
                "region": search_scope or region,
                "support_tier": support,
                "workload": se.get("workload") or acc.get("workload") or {},
            })
        elif has_search:
            result = {"status": "skipped — search slice has no stored GB", "assumptions": ["Nothing to price."]}
        else:
            result = {"status": "skipped — no search workload detected (use --always-copy to force)"}
        published = {}
        if result.get("report_csv"):
            try:
                published = write_search_sheet(drive, estimate_dir, folder["id"], title_se, result)
            except Exception as ex:  # noqa: BLE001
                published = {"error": str(ex)}
        entry = {
            "kind": "search",
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
            "note": "on-demand is the quotable profile" if result.get("readback") else (result.get("error") or result.get("status")),
        }
        if result.get("error"):
            entry["error"] = result["error"]
        if published.get("error"):
            entry["error"] = published["error"]
            entry["note"] = f"{entry.get('note')}; Drive upload failed: {published['error']}"
        rb = result.get("readback") or {}
        lines.append(f"Search → {entry.get('url') or entry.get('local')}  (on-demand {rb.get('monthly_steady_state') or entry['status']}; region {result.get('region_match')})")
        estimators["search"] = entry
    project_only = {"observability", "security"} if args.skip_search else None
    fresh_projects = size_regional_projects(
        acc, sheets, drive, folder, o11y_id=args.o11y_id, security_id=args.security_id,
        support=support, provider=provider, skip_xlsx=args.skip_xlsx, fresh=args.fresh,
        local_dir=estimate_dir, customer=meta["customer"], when=meta["date"], name_suffix=suffix,
        only=project_only, force_months=args.force_months,
    )
    if args.skip_search:
        kept = [p for p in ((sz.get("estimators") or {}).get("projects") or []) if p.get("solution") == "search"]
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
    main()
