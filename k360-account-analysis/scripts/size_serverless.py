#!/usr/bin/env python3
"""ECH > 50% rule: copy golden estimators into Drive folder <customer>_<date>, fill, read back.

Run with the Sheets venv python (google-api-python-client installed):

  ~/.config/cloud-tracker-sheets/venv/bin/python size_serverless.py \
      --account-json artifacts/Acme_2026-11-02/Acme_2026-11-02_account.json

Produces (local folder + Drive folder of the same name):
  <customer>_<date>_Observability_Serverless_estimate   (Sheets copy + .xlsx export)
  <customer>_<date>_Security_Serverless_estimate        (Sheets copy + .xlsx export)
  <customer>_<date>_Search_Serverless_estimate.csv      (es3-estimator report; uploaded as a Google Sheet)
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
from datetime import datetime, timezone
from pathlib import Path

ORG_SKILL = Path(os.environ.get("K360_ORG_SKILL_DIR") or Path.home() / ".cursor/skills/k360-org-report")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ORG_SKILL / "scripts"))
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

O11Y_TAB = "Observability Estimator"
SEC_TAB = "Security Estimator"
REF_TAB = "Estimator_Reference"

# Read-back cells (see references/sizing-cells.md)
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
def services():
    from googleapiclient.discovery import build

    creds = get_creds(interactive=False)
    return (
        build("sheets", "v4", credentials=creds, cache_discovery=False),
        build("drive", "v3", credentials=creds, cache_discovery=False),
    )


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def ensure_drive_folder(drive, name: str) -> dict:
    q = f"name = '{_esc(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false"
    found = drive.files().list(q=q, spaces="drive", fields="files(id,name,webViewLink)", pageSize=5).execute().get("files") or []
    if found:
        f = found[0]
        return {"id": f["id"], "name": f["name"], "url": f.get("webViewLink") or f"https://drive.google.com/drive/folders/{f['id']}"}
    meta = drive.files().create(body={"name": name, "mimeType": FOLDER_MIME}, fields="id,name,webViewLink").execute()
    return {"id": meta["id"], "name": meta["name"], "url": meta.get("webViewLink") or f"https://drive.google.com/drive/folders/{meta['id']}"}


def find_in_folder(drive, parent_id: str, name: str) -> dict | None:
    q = f"'{parent_id}' in parents and name = '{_esc(name)}' and trashed = false"
    files = drive.files().list(q=q, spaces="drive", fields="files(id,name,webViewLink,mimeType)", pageSize=5).execute().get("files") or []
    return files[0] if files else None


def copy_template(drive, template_id: str, title: str, parent_id: str, *, reuse: bool) -> tuple[dict, bool]:
    if reuse:
        existing = find_in_folder(drive, parent_id, title)
        if existing:
            return {"id": existing["id"], "name": existing["name"], "url": existing.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{existing['id']}/edit"}, True
    meta = drive.files().copy(fileId=template_id, body={"name": title, "parents": [parent_id]}, supportsAllDrives=True, fields="id,name,webViewLink").execute()
    return {"id": meta["id"], "name": meta["name"], "url": meta.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{meta['id']}/edit"}, False


def export_xlsx(drive, spreadsheet_id: str, dest: Path) -> Path:
    from googleapiclient.http import MediaIoBaseDownload

    req = drive.files().export_media(fileId=spreadsheet_id, mimeType=XLSX_MIME)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    dest.write_bytes(buf.getvalue())
    return dest


def upload_csv_as_sheet(drive, local: Path, parent_id: str, title: str) -> dict:
    """Publish the es3 report CSV as a Google Sheet. A same-named Doc is replaced."""
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(local), mimetype="text/csv", resumable=False)
    existing = find_in_folder(drive, parent_id, title)
    if existing and existing.get("mimeType") != GSHEET_MIME:
        drive.files().update(fileId=existing["id"], body={"trashed": True}).execute()
        existing = None
    if existing:
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
    path.write_text(csv_text)
    uploaded = upload_csv_as_sheet(drive, path, parent_id, title)
    return {"local": str(path), "url": uploaded["url"], "spreadsheet_id": uploaded["id"]}


def upload_text_as_doc(drive, local: Path, parent_id: str, title: str) -> dict:
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(local), mimetype="text/plain", resumable=False)
    existing = find_in_folder(drive, parent_id, title)
    if existing:
        meta = drive.files().update(fileId=existing["id"], media_body=media, fields="id,name,webViewLink").execute()
    else:
        meta = drive.files().create(body={"name": title, "parents": [parent_id], "mimeType": GDOC_MIME}, media_body=media, fields="id,name,webViewLink").execute()
    return {"id": meta["id"], "name": meta["name"], "url": meta.get("webViewLink") or f"https://docs.google.com/document/d/{meta['id']}/edit"}


def push_cells(sheets, sid: str, tab: str, cells: dict) -> int:
    data = [{"range": f"'{tab}'!{a1}", "values": [[v]]} for a1, v in cells.items() if v is not None and v != ""]
    if not data:
        return 0
    sheets.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={"valueInputOption": "USER_ENTERED", "data": data}).execute()
    return len(data)


def read_cells(sheets, sid: str, tab: str, cells: dict[str, str]) -> dict:
    ranges = [f"'{tab}'!{a1}" for a1 in cells.values()]
    res = sheets.spreadsheets().values().batchGet(
        spreadsheetId=sid, ranges=ranges, valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    vals = [((vr.get("values") or [[None]])[0] or [None])[0] for vr in res.get("valueRanges", [])]
    return dict(zip(cells.keys(), vals))


SEC_ADDON_CELLS = [
    "G11", "G12", "G14", "G15", "G16", "G17", "G18", "G20",
    "G23", "G24", "G25", "G27", "G28", "G29", "G30", "G31", "G32", "G33", "G34",
]


def clear_security_addons(sheets, sid: str, tab: str) -> dict:
    """Turn template add-ons off on a copy. The golden template is not written."""
    try:
        meta = sheets.spreadsheets().get(
            spreadsheetId=sid,
            ranges=[f"'{tab}'!G11:G34"],
            fields="sheets(data(startRow,rowData(values(formattedValue,dataValidation))))",
        ).execute()
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
    res = sheets.spreadsheets().values().get(spreadsheetId=sid, range=f"'{REF_TAB}'!A1:M450").execute()
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


def _sheet_retention(months) -> int | None:
    """Estimator retention rows are the integers 1–36. A missing month is not invented."""
    if months is None or months == "":
        return None
    try:
        value = float(months)
    except (TypeError, ValueError):
        return None
    value = min(36.0, max(1.0, value))
    return int(min(36, max(1, math.ceil(value - 1e-9))))


def _months_for_sheet(block: dict, force: int | None) -> int | None:
    measured = _sheet_retention((block or {}).get("retention_months"))
    if measured is None:
        return None
    if force is not None:
        return int(min(36, max(1, force)))
    return measured


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


def size_regional_projects(acc: dict, sheets, drive, folder: dict, *, o11y_id: str, security_id: str, support: str, provider, skip_xlsx: bool, fresh: bool, local_dir: Path, customer: str, when: str, only: set[str] | None = None, name_suffix: str = "", force_months: int | None = None) -> list[dict]:
    """One estimator copy per serverless project when a solution spans regions."""
    groups = ((acc.get("consolidation") or {}).get("architecture") or {}).get("groups") or []
    by_sol: dict[str, list] = {}
    for g in groups:
        by_sol.setdefault(g["solution"], []).append(g)
    con = (acc.get("sizing") or {}).get("consolidated") or {}
    org_gb = float((acc.get("storage") or {}).get("data_stored_gb") or 0)
    search_slice = con.get("search") or {}
    workload = search_slice.get("workload") or acc.get("workload") or {}
    key = acc["meta"]["run_key"]
    projects: list[dict] = []
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
            region = g["region"]
            title = f"{key}_{sol.title()}_Serverless_{region}{name_suffix}"
            source_months = g.get("retention_months") if g.get("retention_months") is not None else (con.get(sol) or {}).get("retention_months")
            retention = _months_for_sheet({"retention_months": source_months}, force_months)
            if sol != "search" and retention is None:
                continue
            if g.get("daily_raw_gb") is not None:
                daily = float(g["daily_raw_gb"])
            else:
                daily = gb / (float(retention or 3) * DAYS_PER_MONTH) if (retention or 3) else 0.0
            if sol == "search":
                share = gb / total_gb if total_gb else 0.0
                result = run_es3({
                    "stored_gb": gb,
                    "org_stored_gb": org_gb,
                    "provider": provider,
                    "region": region,
                    "support_tier": support,
                    "workload": _scale_qps(workload, share),
                })
                published = {}
                try:
                    published = write_search_sheet(drive, local_dir, folder["id"], title, result)
                except Exception as ex:  # noqa: BLE001
                    published = {"error": str(ex)}
                entry = {
                    "solution": sol, "region": region, "title": title, "stored_gb": gb,
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
                projects.append(entry)
                continue
            if sol == "observability":
                template, tab, readback = o11y_id, O11Y_TAB, O11Y_READBACK
            elif sol == "security":
                template, tab, readback = security_id, SEC_TAB, SEC_READBACK
            else:
                continue
            copied, reused = copy_template(drive, template, title, folder["id"], reuse=not fresh)
            opts = region_options(sheets, copied["id"])
            reg, how = match_region(provider, region, opts)
            if sol == "security":
                clear_security_addons(sheets, copied["id"], tab)
            if sol == "observability":
                cells = observability_input_cells(acc, support, reg, int(retention), daily)
            else:
                cells = security_input_cells(acc, support, reg, int(retention), daily)
            n = push_cells(sheets, copied["id"], tab, cells)
            rb = read_cells(sheets, copied["id"], tab, readback)
            entry = {
                "solution": sol, "region": region, "title": copied["name"], "stored_gb": gb,
                "spreadsheet_id": copied["id"], "url": copied["url"], "reused_existing_copy": reused,
                "cells": cells, "cells_written": n, "region_match": f"{reg} ({how})",
                "readback": rb, "status": "filled", "daily_raw_gb": round(daily, 2),
            }
            if sol == "observability":
                entry["g26_gate"] = g26_gate(daily, rb.get("total_raw_gb_day"))
                if not entry["g26_gate"]["accepted"]:
                    entry["readback_rejected"] = "G26 differs from the written bulk by more than 0.51 GB/day"
            if not skip_xlsx:
                try:
                    entry["local_xlsx"] = str(export_xlsx(drive, copied["id"], local_dir / f"{title}.xlsx"))
                except Exception as ex:  # noqa: BLE001
                    entry["local_xlsx_error"] = str(ex)
            projects.append(entry)
    return projects


def _diag_inputs():
    root = Path(__file__).resolve().parents[2] / "diag-serverless-consolidation" / "scripts"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import diag_inputs
    return diag_inputs


def size_deployment_plan(acc: dict, sheets, drive, folder: dict, *, o11y_id: str, support: str, provider, force_months: int | None, name_suffix: str, fresh: bool) -> dict:
    """Price the five Observability deployment-count bars. 100% reuses the headline sheet."""
    diag = _diag_inputs()
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
    key = acc["meta"]["run_key"]
    region_name = (acc.get("sizing") or {}).get("region")
    bars = []
    try:
        for plan in diag.deployment_count_plans(rows):
            pct = plan["cut_pct"]
            if pct == 100:
                bars.append({"cut_pct": 100, "label": "100%", "projects": 1, "monthly": None, "reused_headline": True})
                continue
            monthly = 0.0
            priced = []
            for index, group in enumerate(plan["groups"], start=1):
                summary = diag.summarize(group, force_months)
                bulk = float(summary.get("bulk_gb_day") or 0)
                if bulk <= 0:
                    priced.append({"names": summary.get("names") or [], "monthly": 0.0, "bulk_gb_day": 0})
                    continue
                written = force_months or summary.get("months_written")
                if not written:
                    return {"bars": [], "error": "Observability deployment-count plan was not priced: a project has bulk and no measured retention."}
                title = f"{key}_Observability_plan_{pct}_{index}{name_suffix}"
                copied, _reused = copy_template(drive, o11y_id, title, folder["id"], reuse=not fresh)
                opts = region_options(sheets, copied["id"])
                reg, _how = match_region(provider, region_name, opts)
                cells = observability_input_cells(acc, support, reg, int(written), bulk)
                push_cells(sheets, copied["id"], O11Y_TAB, cells)
                rb = read_cells(sheets, copied["id"], O11Y_TAB, O11Y_READBACK)
                gate = g26_gate(bulk, rb.get("total_raw_gb_day"))
                if not gate["accepted"]:
                    return {
                        "bars": [],
                        "error": (
                            "Observability deployment-count plan was not priced: "
                            f"{title} G26 is {gate['sheet_raw_gb_day']} GB/day against {gate['written_bulk']} written."
                        ),
                    }
                price = sheet_money(rb.get("monthly_steady_state"))
                if price is None:
                    return {"bars": [], "error": f"Observability deployment-count plan was not priced: {title} has no D32 read-back."}
                monthly += price
                priced.append({
                    "names": summary.get("names") or [],
                    "monthly": round(price, 2),
                    "bulk_gb_day": bulk,
                    "months_written": int(written),
                    "url": copied.get("url"),
                })
            bars.append({
                "cut_pct": pct,
                "label": plan["label"],
                "projects": plan["projects"],
                "monthly": round(monthly, 2),
                "groups": priced,
            })
    except Exception as ex:  # noqa: BLE001
        return {"bars": [], "error": f"Observability deployment-count plan was not priced: {ex}"}
    return {"bars": bars}


# ----------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account-json", required=True)
    ap.add_argument("--o11y-id", default=os.environ.get("O11Y_SHEET_ID", DEFAULT_O11Y), type=sheet_id, help="Observability golden template (id or URL)")
    ap.add_argument("--security-id", default=os.environ.get("SECURITY_SHEET_ID", DEFAULT_SECURITY), type=sheet_id, help="Security golden template (id or URL)")
    ap.add_argument("--support", default=None, help="Support tier for both sheets (default from JSON: Premium)")
    ap.add_argument("--skip-xlsx", action="store_true")
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

    acc_path = Path(args.account_json)
    acc = json.loads(acc_path.read_text())
    _fill_missing_doc_bytes(acc_path, acc)
    sz = acc.get("sizing") or {}
    if not sz:
        raise SystemExit("account JSON has no `sizing` block — run analyze.py first")
    meta = acc["meta"]
    key = meta["run_key"]
    local_dir = acc_path.parent
    local_dir.mkdir(parents=True, exist_ok=True)
    con = sz["consolidated"]
    suffix = _suffix(args.name_suffix)
    sizing_name = args.sizing_json or "sizing.json"
    support = args.support or sz.get("support_tier") or "Premium"
    provider, region = sz.get("provider"), sz.get("region")

    sheets, drive = services()
    folder = ensure_drive_folder(drive, key)
    lines = [f"Local folder → {local_dir}", f"Drive folder → {folder['url']}"]
    if args.projects:
        projects = size_regional_projects(
            acc, sheets, drive, folder, o11y_id=args.o11y_id, security_id=args.security_id,
            support=support, provider=provider, skip_xlsx=args.skip_xlsx, fresh=args.fresh,
            local_dir=local_dir, customer=meta["customer"], when=meta["date"], name_suffix=suffix,
            force_months=args.force_months,
        )
        sz.setdefault("estimators", {})["projects"] = projects
        sz["drive_folder"] = folder
        sz["sized_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        acc["sizing"] = sz
        acc_path.write_text(json.dumps(acc, indent=2))
        (local_dir / sizing_name).write_text(json.dumps(sz, indent=2))
        for p in projects:
            rb = p.get("readback") or {}
            lines.append(f"{p['solution']} {p['region']} → {p.get('url') or p.get('error')}  (monthly {rb.get('monthly_steady_state')})")
        print("\n".join(lines))
        return
    if args.search_sheets:
        estimators = sz.setdefault("estimators", {})
        se = con["search"]
        title_se = f"{key}_Search_Serverless_estimate{suffix}"
        result = run_es3({
            "stored_gb": se.get("stored_gb") or 0,
            "org_stored_gb": (acc.get("storage") or {}).get("data_stored_gb") or 0,
            "provider": provider,
            "region": region,
            "support_tier": support,
            "workload": se.get("workload") or acc.get("workload") or {},
        }) if (se.get("stored_gb") or 0) > 0 else {}
        published = write_search_sheet(drive, local_dir, folder["id"], title_se, result) if result.get("report_csv") else {"error": result.get("error") or "no report"}
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
            local_dir=local_dir, customer=meta["customer"], when=meta["date"], only={"search"},
            name_suffix=suffix,
        )
        kept = [p for p in (estimators.get("projects") or []) if p.get("solution") != "search"]
        estimators["projects"] = fresh + kept
        for p in fresh:
            prb = p.get("readback") or {}
            lines.append(f"{p['solution']} {p['region']} → {p.get('url') or p.get('error')}  (monthly {prb.get('monthly_steady_state')})")
        sz["drive_folder"] = folder
        sz["sized_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        acc["sizing"] = sz
        acc_path.write_text(json.dumps(acc, indent=2))
        (local_dir / sizing_name).write_text(json.dumps(sz, indent=2))
        print("\n".join(lines))
        return
    if args.deployment_plan:
        plan = size_deployment_plan(
            acc, sheets, drive, folder, o11y_id=args.o11y_id, support=support, provider=provider,
            force_months=args.force_months, name_suffix=suffix, fresh=args.fresh,
        )
        sz["deployment_plan"] = plan
        sz["drive_folder"] = folder
        sz["sized_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        acc["sizing"] = sz
        acc_path.write_text(json.dumps(acc, indent=2))
        (local_dir / sizing_name).write_text(json.dumps(sz, indent=2))
        lines.append(plan.get("error") or f"Deployment plan → {len(plan.get('bars') or [])} bars")
        print("\n".join(lines))
        return
    estimators: dict = {}

    # ---------------- Observability
    o = con["observability"]
    title_o = f"{key}_Observability_Serverless_estimate{suffix}"
    o_months = _months_for_sheet(o, args.force_months)
    if o_months is None:
        estimators["observability"] = {"kind": "observability", "status": "skipped — retention was not measured"}
        lines.append("Observability → skipped (retention was not measured)")
    elif o.get("has_workload") or args.always_copy:
        copied, reused = copy_template(drive, args.o11y_id, title_o, folder["id"], reuse=not args.fresh)
        opts = region_options(sheets, copied["id"])
        reg, how = match_region(provider, region, opts)
        daily = float(o.get("daily_raw_gb") or 0)
        cells = observability_input_cells(
            acc, support, reg, o_months, daily, logs=o.get("logs_gb_day"), metrics=o.get("metrics_gb_day"),
        )
        n = push_cells(sheets, copied["id"], O11Y_TAB, cells)
        rb = read_cells(sheets, copied["id"], O11Y_TAB, O11Y_READBACK)
        entry = {"kind": "observability", "title": copied["name"], "spreadsheet_id": copied["id"], "url": copied["url"], "template_id": args.o11y_id, "reused_existing_copy": reused, "tab": O11Y_TAB, "cells": cells, "cells_written": n, "region_match": f"{reg} ({how})", "readback": rb, "status": "filled"}
        entry["g26_gate"] = g26_gate(daily, rb.get("total_raw_gb_day"))
        if not entry["g26_gate"]["accepted"]:
            entry["readback_rejected"] = "G26 differs from the written bulk by more than 0.51 GB/day"
        if not args.skip_xlsx:
            try:
                x = export_xlsx(drive, copied["id"], local_dir / f"{title_o}.xlsx")
                entry["local_xlsx"] = str(x)
            except Exception as ex:  # noqa: BLE001
                entry["local_xlsx_error"] = str(ex)
        estimators["observability"] = entry
        lines.append(f"Observability → {copied['url']}  ({n} cells; Y1 {rb.get('annual_year1')}, monthly {rb.get('monthly_steady_state')}; region {reg} [{how}])")
    else:
        estimators["observability"] = {"kind": "observability", "status": "skipped — no observability workload detected (use --always-copy to force)"}
        lines.append("Observability → skipped (no workload)")

    # ---------------- Security
    s = con["security"]
    title_s = f"{key}_Security_Serverless_estimate{suffix}"
    s_months = _months_for_sheet(s, args.force_months)
    if s_months is None:
        estimators["security"] = {"kind": "security", "status": "skipped — retention was not measured"}
        lines.append("Security → skipped (retention was not measured)")
    elif s.get("has_workload") or args.always_copy:
        copied, reused = copy_template(drive, args.security_id, title_s, folder["id"], reuse=not args.fresh)
        opts = region_options(sheets, copied["id"])
        reg, how = match_region(provider, region, opts)
        clear_security_addons(sheets, copied["id"], SEC_TAB)
        cells = security_input_cells(acc, support, reg, s_months, float(s.get("siem_gb_day") if s.get("siem_gb_day") is not None else s.get("daily_raw_gb") or 0))
        n = push_cells(sheets, copied["id"], SEC_TAB, cells)
        rb = read_cells(sheets, copied["id"], SEC_TAB, SEC_READBACK)
        entry = {"kind": "security", "title": copied["name"], "spreadsheet_id": copied["id"], "url": copied["url"], "template_id": args.security_id, "reused_existing_copy": reused, "tab": SEC_TAB, "cells": cells, "cells_written": n, "region_match": f"{reg} ({how})", "readback": rb, "status": "filled"}
        if not args.skip_xlsx:
            try:
                x = export_xlsx(drive, copied["id"], local_dir / f"{title_s}.xlsx")
                entry["local_xlsx"] = str(x)
            except Exception as ex:  # noqa: BLE001
                entry["local_xlsx_error"] = str(ex)
        estimators["security"] = entry
        lines.append(f"Security → {copied['url']}  ({n} cells; Y1 {rb.get('annual_year1')}; region {reg} [{how}])")
    else:
        estimators["security"] = {"kind": "security", "status": "skipped — no security workload detected (use --always-copy to force)"}
        lines.append("Security → skipped (no workload)")

    # ---------------- Search (es3-estimator notebook library)
    se = con["search"]
    title_se = f"{key}_Search_Serverless_estimate{suffix}"
    if args.skip_search:
        estimators["search"] = (sz.get("estimators") or {}).get("search") or {"kind": "search", "status": "kept from the previous sizing"}
        lines.append("Search → kept from the previous sizing")
    else:
        has_search = bool(se.get("has_workload") or (se.get("stored_gb") or 0) > 0 or args.always_copy)
        result: dict = {}
        if has_search and (se.get("stored_gb") or 0) > 0:
            result = run_es3({
                "stored_gb": se.get("stored_gb") or 0,
                "org_stored_gb": (acc.get("storage") or {}).get("data_stored_gb") or 0,
                "provider": provider,
                "region": region,
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
                published = write_search_sheet(drive, local_dir, folder["id"], title_se, result)
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
        local_dir=local_dir, customer=meta["customer"], when=meta["date"], name_suffix=suffix,
        only=project_only, force_months=args.force_months,
    )
    if args.skip_search:
        kept = [p for p in ((sz.get("estimators") or {}).get("projects") or []) if p.get("solution") == "search"]
        estimators["projects"] = fresh_projects + kept
    else:
        estimators["projects"] = fresh_projects

    sz["folder"] = str(local_dir)
    sz["drive_folder"] = folder
    sz["estimators"] = estimators
    sz["sized_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    sz["support_tier"] = support
    acc["sizing"] = sz
    acc_path.write_text(json.dumps(acc, indent=2))
    (local_dir / sizing_name).write_text(json.dumps(sz, indent=2))
    lines.append(f"Updated {acc_path.name} + {sizing_name}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
