"""Observability, Security, and Search fills."""
from __future__ import annotations

from sizing_shared import *  # noqa: F401,F403
from sheets_io import *  # noqa: F401,F403
from estimate_paths import *  # noqa: F401,F403

def ingest_basis(acc: dict) -> str:
    """`metered` for diagnostic bulk, `raw` for the K360 backup.

    Diagnostic bulk is the bulk request size on the primary shards, after ingest
    pipelines have run and before compression. That is what the estimators call
    metered ingest, so it goes into the cells that take metered volume. The K360
    backup has no such measurement and keeps the raw-data cells.
    """
    return "metered" if (acc.get("sizing") or {}).get("ingest_source") == "diagnostics" else "raw"


def observability_input_cells(acc: dict, support: str, region, retention: int, daily: float, logs=None, metrics=None, *, mix: dict | None = None, retention_metrics: int | None = None, basis: str = "raw") -> dict:
    """Cells for one Observability sheet.

    `metered` writes Option 1: G19 is the monthly metered GB for everything except
    TSDS metrics (logs and traces), G20 is the monthly metered GB of TSDS metrics,
    each GB/day × 730 ÷ 24, which is how the sheet turns them back into GB/day.
    The Option 2 rows are set to 0, so the sheet applies no expansion factor.

    `raw` writes Option 2: logs on G23 and metrics on G25, and the sheet expands
    them itself. Trace GB/day stays in the logs row there.

    Product-derived mix is required for nonzero Observability bulk. The estimator
    is not filled with a synthetic 90/10 split.
    """
    k8s_share = 0.0
    traces = 0.0
    if mix and mix.get("basis"):
        logs = float(mix.get("logs_gb_day") or 0)
        metrics = float(mix.get("metrics_gb_day") or 0)
        traces = float(mix.get("traces_gb_day") or 0)
        k8s_share = float(mix.get("k8s_logs_share") or 0)
    elif float(daily or 0) > 0:
        raise ValueError("Observability product mix is missing; refusing to invent a 90/10 split")
    else:
        logs = metrics = 0.0
    cells = {
        "G4": (acc.get("sizing") or {}).get("feature_tier") or "Complete",
        "G5": support,
        "G8": region,
        "G11": retention,
        "G12": retention if retention_metrics is None else retention_metrics,
        "G15": "No",
        "G27": "100%",
        "G33": "No",
    }
    if basis == "metered":
        cells.update({
            "G19": round((logs + traces) * HOURS_PER_MONTH / 24, 3),
            "G20": round(metrics * HOURS_PER_MONTH / 24, 3),
            "G23": 0, "G24": "0%", "G25": 0, "G26": 0,
        })
    else:
        if traces > 0:
            k8s_share = k8s_share * logs / (logs + traces) if (logs + traces) > 0 else 0.0
            logs += traces
        cells.update({
            "G19": 0, "G20": 0,
            "G23": round(logs, 2),
            "G24": round(k8s_share, 4) if k8s_share > 0 else "0%",
            "G25": round(metrics, 2),
            "G26": 0,
        })
    return cells


def observability_fill(acc: dict, support: str, region, retention: int, daily: float, mix: dict | None, forced: bool, basis: str) -> dict:
    """Observability cells from a measured mix.

    A forced month (--force-months) is written to both retention rows. Otherwise
    TSDS metrics keep their own recorded age. The written month is 12.
    """
    retention_metrics = None
    if mix and not forced and mix.get("metrics_retention_days"):
        retention_metrics = _sheet_retention(float(mix["metrics_retention_days"]) / DAYS_PER_MONTH)
    return observability_input_cells(acc, support, region, retention, daily, mix=mix, retention_metrics=retention_metrics, basis=basis)


def security_input_cells(acc: dict, support: str, region, retention: int, daily: float, *, expansion: float | None = None) -> dict:
    """Cells for one Security sheet.

    G4 is raw GB/day, and the sheet multiplies it by its expansion factor to get
    metered GB/day (G5). With an `expansion`, `daily` is metered and G4 is
    daily ÷ expansion, so G5 comes back as the GB/day that was measured.
    """
    raw = float(daily) / expansion if expansion else float(daily)
    return {
        "G4": round(raw, 4 if expansion else 2),
        "G6": retention,
        "G7": region,
        "G8": (acc.get("sizing") or {}).get("feature_tier") or "Complete",
        "G9": 0,
        "G21": support,
    }


def security_expansion(sheets, sid: str) -> float:
    """The Security sheet's raw-to-metered factor, read from the copy.

    G5 is G4 × that factor, so G5 ÷ G4 is the factor whatever cell holds it. A
    copy whose G4 is 0 falls back to the reference cell. A factor that cannot be
    read stops the fill.
    """
    rb = read_cells(sheets, sid, SEC_TAB, {"raw": "G4", "metered": "G5", "reference": "Estimator_Reference!C326"})
    raw, metered, reference = sheet_money(rb.get("raw")), sheet_money(rb.get("metered")), sheet_money(rb.get("reference"))
    factor = metered / raw if raw and metered and raw > 0 else reference
    if not factor or not 1.0 <= factor <= 10.0:
        raise RuntimeError(f"Security expansion factor could not be read from the sheet (G4 {rb.get('raw')!r}, G5 {rb.get('metered')!r})")
    return float(factor)


def fill_security(sheets, sid: str, account: dict, support: str, region, retention: int, daily: float, opts, basis: str) -> tuple[dict, int, dict, dict | None]:
    """Turn the add-ons off, write the cells, and read the dollars back.

    Returns the cells, how many were written, the read-back, and the echo gate.
    On the metered basis the gate compares G5, the sheet's metered GB/day, with
    the GB/day measured. A miss clears the monthly so the sheet is not quoted.
    """
    expansion = security_expansion(sheets, sid) if basis == "metered" else None
    clear_security_addons(sheets, sid, SEC_TAB)
    cells = security_input_cells(account, support, region, retention, daily, expansion=expansion)
    written = push_cells(sheets, sid, SEC_TAB, cells, allowed_text=set(opts))
    rb = security_readback(read_cells(sheets, sid, SEC_TAB, SEC_READBACK), retention)
    rb["ingest_basis"] = basis
    gate = None
    if basis == "metered":
        rb["expansion"] = expansion
        rb["echo_gb_day"] = rb.get("metered_gb_day")
        gate = echo_gate(daily, rb.get("metered_gb_day"))
        if not gate["accepted"]:
            rb["monthly_steady_state"] = None
            rb["annual_year1"] = None
            rb["annual_steady_state"] = None
    return cells, written, rb, gate


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


def run_es3(payload: dict) -> dict:
    py = es3_python()
    if py is None:
        return {
            "status": "error",
            "error": "es3-estimator venv missing. Create ~/.config/es3-estimator/venv and pip install pandas numpy joblib 'scikit-learn==1.7.2'.",
        }
    script = Path(__file__).resolve().parent / "es3_estimate.py"
    body = dict(payload)
    import sizing_shared
    if sizing_shared._ALLOW_NEAREST:
        body["allow_nearest_region"] = True
    try:
        proc = subprocess.run(
            [str(py), str(script)],
            input=json.dumps(body),
            text=True,
            capture_output=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "timeout"}
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


def _nearest_same_provider(provider: str | None, region: str | None, options: list[str]) -> str | None:
    """Closest same-provider dropdown option, by shared prefix of the region token."""
    from estimator_layout import _REGION_OPT as _opt
    from estimator_layout import _norm as _fold

    prov = str(provider or "").strip().upper()
    want = _fold(region)
    best = None
    best_score = -1.0
    for option in options or []:
        match = _opt.match(option)
        if not match or match.group(1).upper() != prov:
            continue
        token = _fold(match.group(2))
        shared = 0
        for left, right in zip(want, token):
            if left != right:
                break
            shared += 1
        score = shared / max(len(want), len(token), 1)
        if score > best_score:
            best_score = score
            best = option
    return best


def match_region(provider: str | None, region: str | None, options: list[str]) -> tuple[str | None, str]:
    """Shared matcher. No other region and no other cloud. Nearest is opt-in.

    how is exact, alias, nearest, or none. A missing match does not write a region cell.
    """
    from estimator_layout import match_region as _shared

    import sizing_shared
    option, how = _shared(provider, region, options)
    if option is not None or not sizing_shared._ALLOW_NEAREST:
        return option, how
    nearest = _nearest_same_provider(provider, region, options)
    if nearest is None:
        return None, "none"
    return nearest, "nearest"


def _region_rejected(region: str | None) -> str:
    return f"region {region} is not on the estimator list"

__all__ = [name for name in globals() if not name.startswith('__')]
