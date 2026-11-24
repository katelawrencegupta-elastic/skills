#!/usr/bin/env python3
"""Diagnostic Serverless consolidation.

Reuses the account-summary fetch, then prices Observability and Security from
diagnostic bundles. Search disk is the local store. The K360 stored-GB backup
is a separate document.

  python run_consolidation.py --org "Acme Corp" --reuse serverless_analyzer/diag_artifacts/<key>/<key>_org_report.json
  python run_consolidation.py --org "Acme Corp" --diags-dir serverless_analyzer/diag_artifacts/<org_id>_diags_<date>

The quote window is the last 90 billed days (--lookback-days, maximum 365). The account fetch stays a year.
ECH dollars are the K360 billing cost field for that window, as a month. One consolidation PDF is
written at the average index retention, rounded up to a whole month.

Run from the project root that holds `.env` (KIBANA_URL, ELASTIC_API_KEY, FOUND_ADMIN_API_KEY).
The account summary stays on run_account_analysis.py --skip-diags --skip-sizing.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from skill_paths import artifacts_root, find_skill, scripts_dir  # noqa: E402

_account_root = find_skill("k360-account-analysis", HERE)
K360_SCRIPTS = scripts_dir(_account_root)
sys.path.insert(0, str(K360_SCRIPTS))

from consolidation import apply_quote_lookback, billed_series, build_consolidation  # noqa: E402
from diag_inputs import ESTIMATE_MONTHS  # noqa: E402
from privacy import seal_text  # noqa: E402
from render_pdf import export_documents  # noqa: E402
from report_md import render  # noqa: E402
from run_account_analysis import (  # noqa: E402
    _est,
    _run as _run_command,
    _sheets_py,
    agent_brief,
    finish_account,
    parse_args,
    prepare_account,
)


from constants import QUOTE_TELEMETRY_ATTEMPTS, QUOTE_TELEMETRY_BUDGET_S, QUOTE_TELEMETRY_GRACE_S, TEMPLATE_MISMATCH_EXIT  # noqa: E402


def _run(cmd: list[str]) -> None:
    """Run one step. A template mismatch stops the whole run instead of quoting without it."""
    try:
        _run_command(cmd)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == TEMPLATE_MISMATCH_EXIT:
            raise SystemExit(
                "ERROR: an estimator copy does not match the layout this skill writes (see the message above). "
                "The run stopped before a quote was written."
            ) from exc
        raise


def fetch_with_budget(fetch, budget_s: float = QUOTE_TELEMETRY_BUDGET_S, attempts: int = QUOTE_TELEMETRY_ATTEMPTS):
    """Run `fetch` on a worker thread, give each attempt `budget_s`, and retry a timeout or failure.

    The quote-window telemetry fetch is many Elasticsearch calls in sequence. One slow call used
    to spend the whole budget and lose Search's price with no second try. A timed-out attempt's
    thread is abandoned (it is a daemon), so a retry starts a fresh one. Raises the last error.
    """
    import threading

    last: BaseException | None = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        box: dict = {}

        def _run() -> None:
            try:
                box["value"] = fetch()
            except BaseException as exc:  # noqa: BLE001 - carried to the caller
                box["error"] = exc

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(budget_s)
        if worker.is_alive():
            last = TimeoutError(f"quote-window telemetry fetch exceeded {int(budget_s)}s (attempt {attempt} of {attempts})")
        elif "error" in box:
            last = box["error"]
        else:
            return box["value"]
        print(f"NOTE: {last}", file=sys.stderr)
    raise last if last is not None else RuntimeError("quote-window telemetry fetch did not run")


class telemetry_lock:
    """One telemetry fetch at a time across runs on this machine.

    Parallel runs hit the same Elasticsearch cluster and each pushed the others past the
    time limit. The lock makes a second run wait its turn instead of failing.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fh = None

    def __enter__(self):
        try:
            import fcntl

            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.path, "a+")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except (ImportError, OSError):
            self.fh = None
        return self

    def __exit__(self, *exc):
        if self.fh:
            try:
                import fcntl

                fcntl.flock(self.fh, fcntl.LOCK_UN)
            finally:
                self.fh.close()
        return False


def fetch_proxy_workloads(read, *, lock_path: Path, budget_s: float = QUOTE_TELEMETRY_BUDGET_S,
                          grace_s: float = QUOTE_TELEMETRY_GRACE_S, attempts: int = QUOTE_TELEMETRY_ATTEMPTS):
    """Read the Search workload from the proxy logs: one run at a time on this machine, inside a time budget.

    `read(deadline)` takes a `time.monotonic()` instant. The deadline is what stops the reads: past it no
    call is made, so a region still unread comes back in `errors` and the regions read before it are kept.
    `fetch_with_budget` is the backstop for a call that hangs past its own socket timeout, so its limit is
    the deadline plus `grace_s`. The deadline starts when the lock is held, not while this run waits for it.
    """
    def attempt() -> dict:
        return read(time.monotonic() + budget_s)

    with telemetry_lock(lock_path):
        return fetch_with_budget(attempt, budget_s=budget_s + grace_s, attempts=attempts)


def note_limit(acc_or_sz: dict, code: str, detail: str, severity: str = "high") -> None:
    """Record a degraded step on the quote, not only on stderr.

    A warning that changes what the quote contains has to reach the artifact: the
    brief's `limits`, the chat bullets and the PDF all read
    `sizing.diagnostics.estimation_limits`. A message that only went to stderr is
    invisible to anyone reading the run afterwards, which is how a sizing step
    that never ran can look like a finished quote.
    """
    sz = acc_or_sz.get("sizing") if isinstance(acc_or_sz.get("sizing"), dict) else acc_or_sz
    limits = sz.setdefault("diagnostics", {}).setdefault("estimation_limits", [])
    if any(isinstance(row, dict) and row.get("code") == code for row in limits):
        return
    limits.append({"code": code, "detail": detail, "severity": severity})


def warn(acc_or_sz: dict | None, code: str, detail: str, severity: str = "high") -> None:
    """Print a degradation to stderr and record it on the quote."""
    print(f"WARN: {detail}", file=sys.stderr)
    if acc_or_sz is not None:
        note_limit(acc_or_sz, code, detail, severity)


def _retention_label(diagnostic: bool, retention_months: int | None, retention_days: int | None, *, index_average: bool = False) -> str:
    if not diagnostic:
        return "assumed month"
    if retention_months is None:
        return "not ascertained, no month written"
    if index_average:
        unit = "month" if int(retention_months) == 1 else "months"
        return f"index average rounded up to {int(retention_months)} {unit}"
    if retention_days and int(retention_months) != 12:
        return f"{int(retention_days)} days ({int(retention_months)} months)"
    return "one year (12 months)"


def method_stamp(lookback_days: int, *, diagnostic: bool = True, retention_months: int | None = 12, retention_days: int | None = None, index_average: bool = False) -> dict:
    """Which code and which rules produced this run.

    Two runs of one customer can only be compared when they carry the same stamp.
    `commit` is the checkout the scripts ran from. `uncommitted_changes` is true
    when that checkout held a change that could alter what the scripts do: a
    modification to a tracked file, or an untracked `.py` under a skill's
    `scripts/`, which an import can pick up. An untracked file that cannot change
    behaviour — a review note, a scratch document — is listed in
    `untracked_ignored` instead of blocking the run. Both are None when the
    scripts are not in a git checkout.
    """
    root = HERE.parent.parent

    def git(*argv: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    tracked = git("status", "--porcelain", "--untracked-files=no", "--", ".")
    everything = git("status", "--porcelain", "--untracked-files=all", "--", ".")
    untracked = [
        line[3:].strip() for line in (everything or "").splitlines()
        if line.startswith("?? ")
    ]
    importable = sorted(name for name in untracked if name.endswith(".py") and "/scripts/" in f"/{name}")
    ignored = sorted(name for name in untracked if name not in importable)
    dirty = None if tracked is None else bool(tracked) or bool(importable)
    return {
        "commit": git("rev-parse", "--short", "HEAD") or None,
        "uncommitted_changes": dirty,
        "untracked_importable": importable,
        "untracked_ignored": ignored,
        "ingest_basis": "metered diagnostic bulk" if diagnostic else "K360 stored GB ÷ assumed retention",
        "ech_dollars": "k360_cost",
        "quote_window_days": int(lookback_days),
        "retention": _retention_label(diagnostic, retention_months, retention_days, index_average=index_average),
        "integrity_pass": "every check",
    }


def preflight(args, *, network: bool = True) -> list[dict]:
    """Check everything a run needs, before it downloads anything.

    Each of these used to surface one at a time, mid-run, as a stderr line after
    minutes of work: a missing Sheets venv only showed up once there was a quote
    to fill, and a missing es3 interpreter only when a Search region came to be
    priced. Rows are `{check, ok, detail}`; `ok` None is "could not tell".
    """
    rows: list[dict] = []

    def row(check: str, ok: bool | None, detail: str) -> None:
        rows.append({"check": check, "ok": ok, "detail": detail})

    stamp = method_stamp(int(getattr(args, "lookback_days", 90) or 90))
    dirty = stamp.get("uncommitted_changes")
    if dirty is None:
        row("git checkout", None, "not a git checkout, so the tree cannot be proven clean")
    elif dirty:
        extra = stamp.get("untracked_importable") or []
        row("git checkout", False, "uncommitted changes" + (f", including untracked scripts: {', '.join(extra)}" if extra else ""))
    else:
        ignored = stamp.get("untracked_ignored") or []
        note = f" ({len(ignored)} untracked file(s) that cannot change behaviour)" if ignored else ""
        row("git checkout", True, f"clean at {stamp.get('commit')}{note}")

    env_path = Path(getattr(args, "env", ".env") or ".env")
    env: dict[str, str] = {}
    if not env_path.is_file():
        row(".env", False, f"not found at {env_path.resolve()}")
    else:
        try:
            from fetch_org import load_env
            env = load_env(env_path)
        except Exception as exc:  # noqa: BLE001
            row(".env", False, f"could not be read ({exc!r})")
        else:
            missing = [
                name for name, alts in (
                    ("KIBANA_URL", ("KIBANA_URL", "ELASTIC_KIBANA_URL")),
                    ("ELASTIC_API_KEY", ("ELASTIC_API_KEY", "ES_API_KEY")),
                    ("FOUND_ADMIN_API_KEY", ("FOUND_ADMIN_API_KEY",)),
                ) if not any(env.get(alt) for alt in alts)
            ]
            row(".env", not missing, f"missing {', '.join(missing)}" if missing else f"{env_path} has the keys a run needs")

    py = _sheets_py()
    row("Sheets venv", py.is_file(), str(py) if py.is_file() else f"missing at {py} — no estimator copy can be filled (references/setup.md)")

    # Search is priced on the Search estimator sheet. The es3 notebook is needed only for --search-notebook.
    if getattr(args, "search_notebook", False):
        from consolidation_regional import es3_estimator_missing, es3_estimator_python
        gone = es3_estimator_missing()
        row("es3 estimator", gone is None, es3_estimator_python() if gone is None else f"missing at {gone} — --search-notebook cannot price Search")

    reuse = getattr(args, "reuse", None)
    if reuse:
        path = Path(reuse)
        if not path.is_file():
            row("org report", False, f"--reuse not found: {path}")
        else:
            try:
                daily = (json.loads(path.read_text()).get("ecu") or {}).get("daily_totals")
            except Exception as exc:  # noqa: BLE001
                row("org report", False, f"could not be read ({exc!r})")
            else:
                row("org report", bool(daily), f"{len(daily)} daily billing days" if daily else "no daily billing series: fetched at week grain, so there is no quote window")

    from serverless_regions import SERVERLESS_REGIONS_READ, region_table_age_days, region_table_is_stale
    age = region_table_age_days()
    if age is None:
        row("region table", None, "the date it was read could not be parsed")
    elif region_table_is_stale():
        row("region table", False, f"Serverless regions read {SERVERLESS_REGIONS_READ}, {age} days ago: refresh serverless_regions.py if the page added regions")
    else:
        row("region table", True, f"Serverless regions read {SERVERLESS_REGIONS_READ}, {age} days ago")

    if network and env and not getattr(args, "skip_vpn_check", False):
        try:
            from urllib.parse import urlsplit
            from admin_diags import admin_base
            from vpn_check import aviatrix_status
            host = urlsplit(admin_base(env)).hostname or "admin.found.no"
            up, detail = aviatrix_status(host)
            row("admin access", bool(up), detail or host)
        except Exception as exc:  # noqa: BLE001
            row("admin access", None, f"could not be checked ({exc!r})")
    return rows


def print_preflight(rows: list[dict]) -> bool:
    """Print the table. True when nothing failed."""
    mark = {True: "ok  ", False: "FAIL", None: "?   "}
    width = max((len(r["check"]) for r in rows), default=10)
    for r in rows:
        print(f"  {mark[r['ok']]} {r['check']:<{width}}  {r['detail']}")
    failed = [r["check"] for r in rows if r["ok"] is False]
    print(f"preflight: {len(rows) - len(failed)}/{len(rows)} ok" + (f" — failed: {', '.join(failed)}" if failed else ""))
    return not failed


def run_manifest(args, acc: dict, started: float, *, preflight_rows: list[dict] | None = None) -> dict:
    """What this run was, in one file, so a finished batch can be audited.

    Reading a background run used to mean inferring from file timestamps and the
    wording of a note which code produced which artifact. This records it.
    """
    sz = acc.get("sizing") or {}
    meta = acc.get("meta") or {}
    cons = acc.get("consolidation") or {}
    integrity = cons.get("integrity") or {}
    org = meta.get("org") or {}
    redacted = {"env", "reuse", "diags_dir", "artifacts_root", "org"}
    return {
        "run_key": meta.get("run_key"),
        "started": dt.datetime.fromtimestamp(started, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_sec": round(time.time() - started, 1),
        "command": Path(sys.argv[0]).name,
        # Paths and the customer name stay out: the flags that shape the quote do not.
        "options": {
            key: value for key, value in sorted(vars(args).items())
            if key not in redacted and not key.startswith("_") and value not in (None, False)
        },
        "organization_ids": [str(i) for i in (org.get("ids") or []) if i],
        "method": meta.get("method"),
        "draft": bool(meta.get("draft")),
        "ingest_source": sz.get("ingest_source"),
        "diagnostics_status": sz.get("diagnostics_status"),
        "preflight": preflight_rows or [],
        "limits": [
            {"code": row.get("code"), "severity": row.get("severity")}
            for row in ((sz.get("diagnostics") or {}).get("estimation_limits") or [])
            if isinstance(row, dict)
        ],
        "integrity": {
            "passed": integrity.get("passed"),
            "score": integrity.get("score"),
            "failed": [r.get("check") for r in (integrity.get("checks") or []) if r.get("passed") is False],
            "warnings": [r.get("check") for r in (integrity.get("checks") or []) if r.get("warning")],
        },
        "savings": cons.get("savings"),
        "exports": sorted((acc.get("exports") or {}).keys()),
    }


def quote_options_from(args) -> dict:
    """The comparison flags, stored on the account so the consolidation is a pure function of it."""
    return {
        "ech_scope": getattr(args, "ech_scope", "priced") or "priced",
        "unmeasured": getattr(args, "unmeasured", "exclude") or "exclude",
        "min_priced_ecu_pct": float(getattr(args, "min_priced_ecu_pct", 90.0)),
        "keep_serverless_spend": bool(getattr(args, "keep_serverless_spend", False)),
    }


def _estimator_flags(args) -> list[str]:
    """Flags for size_serverless.py. xlsx export is off unless --xlsx was passed."""
    flags: list[str] = []
    if getattr(args, "xlsx", False):
        flags.append("--xlsx")
    if getattr(args, "always_copy", False):
        flags.append("--always-copy")
    if getattr(args, "fresh", False):
        flags.append("--fresh")
    if getattr(args, "allow_nearest_region", False):
        flags.append("--allow-nearest-region")
    if getattr(args, "search_notebook", False):
        flags.append("--search-notebook")
    return flags


def _half_dollar(rate) -> bool:
    try:
        return rate is not None and abs(float(rate) - 0.50) < 0.0001
    except (TypeError, ValueError):
        return False


def _estimator_quote(entry: dict) -> dict:
    keep = ("title", "url", "status", "readback", "region_match", "echo_gate", "readback_rejected", "cells")
    return {k: entry[k] for k in keep if k in entry}


def _search_power_brief(block: dict | None) -> dict | None:
    if not isinstance(block, dict):
        return None
    profiles = []
    for row in block.get("profiles") or []:
        if not isinstance(row, dict):
            continue
        monthly = row.get("Total $/month")
        if monthly is None:
            monthly = row.get("monthly") if row.get("monthly") is not None else row.get("monthly_steady_state")
        profiles.append({
            "profile": row.get("Search Power Setting") or row.get("profile") or row.get("name"),
            "monthly": monthly,
        })
    if not profiles:
        return None
    out = {"profiles": profiles}
    if block.get("region_match"):
        out["region_match"] = block["region_match"]
    return out


def _integrity_brief(block: dict | None) -> dict | None:
    if not isinstance(block, dict) or not (block.get("checks") or "score" in block):
        return None
    failed = [
        {"check": row.get("check"), "detail": row.get("detail"), "score": row.get("score")}
        for row in (block.get("checks") or [])
        if row.get("passed") is False
    ]
    warnings = [
        {"check": row.get("check"), "detail": row.get("detail"), "score": row.get("score")}
        for row in (block.get("checks") or [])
        if row.get("warning")
    ]
    return {
        "score": block.get("score"),
        "passed": block.get("passed"),
        "confidence": block.get("confidence"),
        "failed": failed,
        "warnings": warnings,
    }


def _plan_brief(plan) -> list[dict]:
    rows = plan.get("bars") if isinstance(plan, dict) else plan
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        out.append({"label": row.get("label") or "", "monthly": row.get("monthly"), "projects": row.get("projects")})
    return out


def _ordered_notes(cons: dict, sz: dict) -> list:
    """Estimation limits, then gate notes, then the rest. Nothing is dropped."""
    limits = ((sz.get("diagnostics") or {}).get("estimation_limits")) or []
    first = []
    for row in limits:
        if isinstance(row, dict):
            text = row.get("detail") or row.get("code")
            if text:
                first.append(str(text))
        elif row:
            first.append(str(row))
    gate_words = ("echo", "gate", "not quoted", "estimator list", "unverified")
    gate = []
    rest = []
    for note in cons.get("notes") or []:
        text = str(note)
        if any(word in text.lower() for word in gate_words):
            gate.append(note)
        else:
            rest.append(note)
    return first + gate + rest


def _region_matches(estimators: dict) -> list[dict]:
    rows = []
    for key, entry in (estimators or {}).items():
        if key == "projects" or not isinstance(entry, dict):
            continue
        if entry.get("region_match") or entry.get("readback_rejected"):
            rows.append({
                "solution": key,
                "region": entry.get("region"),
                "region_match": entry.get("region_match"),
                "readback_rejected": entry.get("readback_rejected"),
            })
    for entry in (estimators or {}).get("projects") or []:
        if isinstance(entry, dict) and entry.get("role") != "measured_retention" and (entry.get("region_match") or entry.get("readback_rejected")):
            rows.append({
                "solution": entry.get("solution"),
                "region": entry.get("region"),
                "region_match": entry.get("region_match"),
                "readback_rejected": entry.get("readback_rejected"),
            })
    return rows


def consolidation_brief(acc: dict) -> dict:
    """Quote slice for the diagnostic skill. The account-summary brief stays separate."""
    m = acc.get("meta") or {}
    sz = acc.get("sizing") or {}
    cons = acc.get("consolidation") or {}
    est_in = sz.get("estimators") if isinstance(sz.get("estimators"), dict) else {}
    estimators = {}
    for key, entry in est_in.items():
        if key == "projects" or not isinstance(entry, dict):
            continue
        estimators[key] = _estimator_quote(entry)
    projects = []
    for entry in est_in.get("projects") or []:
        if isinstance(entry, dict):
            projects.append(_estimator_quote(entry) | {
                "solution": entry.get("solution"),
                "region": entry.get("region"),
            })
    if projects:
        estimators["projects"] = projects
    exports = acc.get("exports") or {}
    rates = [
        {
            "name": row.get("name") or "",
            "raw_gb_day": row.get("raw_gb_day"),
            "metered_gb_day": row.get("metered_gb_day"),
            "usd_per_gb": row.get("usd_per_gb"),
            "at_half_dollar": _half_dollar(row.get("usd_per_gb")),
        }
        for row in (cons.get("metered_rates") or [])
        if isinstance(row, dict)
    ]
    out = {
        "customer": m.get("customer"),
        "run_key": m.get("run_key"),
        "date": m.get("date"),
        "ingest_source": cons.get("source") or sz.get("ingest_source") or "k360",
        "savings": {k: (cons.get("savings") or {}).get(k) for k in ("ech_basis", "ech_list_mo", "serverless_mo", "list_mo", "list_yr", "list_pct") if (cons.get("savings") or {}).get(k) is not None},
        "quotes": ((cons.get("reconciliation") or {}).get("quotes")) or [],
        "fleet": ((cons.get("sensitivity") or {}).get("fleet")) or [],
        "contract_rate": cons.get("contract_rate"),
        "ech_scope": cons.get("ech_scope"),
        "ml_eis_carryover": cons.get("ml_eis_carryover"),
        "region_support": cons.get("region_support"),
        "ech_recent_months": cons.get("ech_recent_months"),
        "ech_recent_months_priced": _priced_months_brief(cons.get("ech_recent_months_priced")),
        "estimate_months": cons.get("estimate_months"),
        "draft": bool(m.get("draft")),
        "notes": _ordered_notes(cons, sz),
        "limits": list(((sz.get("diagnostics") or {}).get("estimation_limits")) or []),
        "region_matches": _region_matches(est_in),
        "per_cluster_plan": cons.get("per_cluster_plan"),
        "diagnostics_coverage": sz.get("diagnostics") if (cons.get("source") == "diagnostics" or sz.get("ingest_source") == "diagnostics") else None,
        "excluded_deployments": m.get("excluded_deployments"),
        "method": m.get("method"),
        "deployment_plan": _plan_brief(cons.get("deployment_plan")),
        "metered_rates": rates,
        "estimators": estimators,
        "links": {
            "folder": sz.get("folder"),
            "drive": (sz.get("drive_folder") or {}).get("url"),
            "pdfs": {
                k: {field: v.get(field) for field in ("pdf", "html", "drive_url") if v.get(field)}
                for k, v in exports.items() if isinstance(v, dict)
            },
        },
    }
    org_ids = [str(i) for i in ((m.get("org") or {}).get("ids") or []) if i]
    if org_ids:
        out["organization_id"] = (m.get("org") or {}).get("id")
        out["organization_ids"] = org_ids
    power = _search_power_brief(cons.get("search_power"))
    if power:
        out["search_power"] = power
    integrity = _integrity_brief(cons.get("integrity"))
    if integrity:
        out["integrity"] = integrity
    return out


def _priced_months_brief(block) -> dict | None:
    """The page-1 comparison when deployment billing is unavailable."""
    if not isinstance(block, dict):
        return None
    return {
        "months": block.get("months") or [],
        "average": block.get("average"),
        "comparison": block.get("comparison"),
        "comparison_basis": block.get("comparison_basis"),
        "stable": block.get("stable"),
        "unpriced": block.get("unpriced") or [],
        "unvalidated": block.get("unvalidated") or {},
        "stays_on_ech_names": block.get("stays_on_ech_names") or [],
    }


def chat_lines(brief: dict) -> list[str]:
    """At most six lines: the headline, why confidence is held back, failed checks, limits, links.

    The links are the one line the reader cannot reconstruct, so they are kept and
    the limits give up their place first. The confidence line replaces the generic
    warning list, because every warning check is a weighted confidence driver and
    the drivers are already in the order worth acting on.
    """
    savings = brief.get("savings") or {}
    monthly = savings.get("serverless_mo")
    integrity = brief.get("integrity") or {}
    failed = [
        str(row.get("check"))
        for row in (integrity.get("failed") or [])
        if isinstance(row, dict) and row.get("check")
    ]
    draft = bool(brief.get("draft") or ((brief.get("method") or {}).get("uncommitted_changes")))
    conf = integrity.get("confidence") or {}
    band = conf.get("band")

    head: list[str] = []
    # "Verified" has to be shown, not assumed. A missing integrity block, or one that scored no check, has
    # `passed` None, and a $0 monthly is no quote, so neither may print a price as if it were verified.
    has_price = monthly is not None and float(monthly) > 0
    if not has_price or integrity.get("passed") is not True or failed or draft:
        head.append("not verified")
    elif band:
        head.append(f"Serverless ${float(monthly):,.0f}/mo · confidence {band} ({conf.get('score')})")
    else:
        head.append(f"Serverless ${float(monthly):,.0f}/mo")

    share = (brief.get("ech_scope") or {}).get("compared_share_pct")
    if share is not None and float(share) < 99.5:
        head[0] += f" · compared on {float(share):.0f}% of the ECH bill"

    drivers = [row for row in (conf.get("drivers") or []) if isinstance(row, dict)]
    warnings = [
        str(row.get("check"))
        for row in (integrity.get("warnings") or [])
        if isinstance(row, dict) and row.get("check")
    ]
    if band and band != "High" and drivers:
        named = [str(row.get("check")) for row in drivers[:2]]
        more = len(drivers) - len(named)
        tail = f", and {more} more" if more > 0 else ""
        head.append(f"Confidence {band} mainly on: {', '.join(named)}{tail}. The quote is still produced.")
    elif warnings:
        head.append("Warning: " + ", ".join(warnings) + ". The quote is still produced.")
    if failed:
        head.append("Failed checks: " + ", ".join(failed))

    links = brief.get("links") or {}
    shown = [links.get("drive")] if links.get("drive") else []
    # The consolidation PDF is the deliverable. The account PDF and the appendix are not linked here.
    cons_pdf = (links.get("pdfs") or {}).get("consolidation")
    if isinstance(cons_pdf, dict) and (cons_pdf.get("drive_url") or cons_pdf.get("pdf")):
        shown.append(cons_pdf.get("drive_url") or cons_pdf.get("pdf"))
    tail_lines = ["Links: " + " ".join(shown)] if shown else []

    limits = [row for row in (brief.get("limits") or []) if isinstance(row, dict)]
    rank = {"high": 0, "medium": 1, "low": 2}
    limits.sort(key=lambda row: rank.get(str(row.get("severity") or "low"), 3))
    # The contract is the top three limits by severity, and no more than six bullets in all.
    room = min(3, max(0, 6 - len(head) - len(tail_lines)))
    middle = [str(row.get("detail") or row.get("code") or "limit") for row in limits[:room]]
    return (head + middle + tail_lines)[:6]


def _with_appendix(docs: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """One appendix document, from the full-retention bulk quote, in the run folder."""
    source = next((doc for doc, kind in docs if kind == "consolidation"), None)
    if source is None:
        return docs
    return docs + [(source, "appendix")]


def quote_pdf_jobs(account: dict, diagnostic: dict | None, extras: list[tuple[dict | None, str]]) -> list[tuple[dict, str]]:
    """Account PDF plus each consolidation scenario that finished. One pool prints them."""
    diag_cons = (diagnostic or {}).get("consolidation") or {}
    if diagnostic is None or not diag_cons or diag_cons.get("error"):
        docs = [(account, "account")]
        cons = account.get("consolidation") or {}
        if cons and not cons.get("error"):
            docs.append((account, "consolidation"))
        return _with_appendix(docs)
    docs: list[tuple[dict, str]] = [(account, "account"), (diagnostic, "consolidation")]
    for doc, kind in extras:
        cons = (doc or {}).get("consolidation") or {}
        if doc is not None and cons and not cons.get("error"):
            docs.append((doc, kind))
    return _with_appendix(docs)


def _quote_block(acc: dict) -> dict:
    cons = acc.get("consolidation") or {}
    return {
        "source": cons.get("source") or "k360",
        "savings": {k: (cons.get("savings") or {}).get(k) for k in ("ech_basis", "ech_list_mo", "serverless_mo", "list_mo", "list_yr", "list_pct") if (cons.get("savings") or {}).get(k) is not None},
        "quotes": ((cons.get("reconciliation") or {}).get("quotes")) or [],
        "notes": (cons.get("notes") or [])[:4],
        "contract_rate": cons.get("contract_rate"),
        "coverage": (acc.get("sizing") or {}).get("diagnostics") or None,
    }


def _apply_diagnostic_report_view(acc: dict, diag_acc: dict) -> None:
    """Overlay diagnostic sizing fields in the report artifact without replacing billing/workload data."""
    for key in ("storage", "solutions"):
        if isinstance(diag_acc.get(key), dict):
            acc[key] = diag_acc[key]
    source_map = (diag_acc.get("sizing") or {}).get("source_precedence")
    if source_map:
        acc.setdefault("sizing", {})["source_precedence"] = source_map


def _money_note(diag_mo, k360_mo) -> str:
    return (
        f"Discrepancy: diagnostic bulk prices serverless at ${float(diag_mo):,.0f}/mo. "
        f"The K360 backup, stored GB ÷ assumed retention, prices it at ${float(k360_mo):,.0f}/mo. "
        "K360 does not provide a raw ingest total, so those two figures are not interchangeable."
    )


def _written_months(diag: dict, args) -> int | None:
    """The month written to the Observability and Security sheets, or None when none could be ascertained.

    `retention_month_basis` `unascertained` means no index had a retention age and no delete age was
    measured. No default month stands in for that.
    """
    meta = diag.get("meta") or {}
    if meta.get("retention_month_basis") == "unascertained":
        return None
    return int(meta.get("estimate_months") or getattr(args, "estimate_months", None) or ESTIMATE_MONTHS)


def _diagnostics_document(acc: dict, infra: dict, folder: Path, env: dict, args) -> dict | None:
    """Default consolidation: measured ingest and retention from admin-console diagnostics."""
    sz = acc.setdefault("sizing", {})
    if not sz.get("triggered") or args.skip_diags:
        if args.skip_diags:
            sz["diagnostics_status"] = "skipped via --skip-diags"
        return None
    from admin_diags import parse_diagnostics, pull_diagnostics
    from diag_account import apply_diagnostics

    org_meta = (acc.get("meta") or {}).get("org") or {}
    org_id = org_meta.get("id")
    org_ids = [str(i) for i in (org_meta.get("ids") or []) if i]
    if org_id and str(org_id) not in org_ids:
        org_ids.insert(0, str(org_id))
    if not org_ids and org_id:
        org_ids = [str(org_id)]
    when = (acc.get("meta") or {}).get("date")
    download_failed: list[dict] = []
    # apply_diagnostics rebuilds sizing.diagnostics from the bundles, so a limit recorded
    # before that call is discarded. Stage these and replay them onto the diagnostic account.
    pending: list[dict] = []

    def stage(code: str, detail: str, severity: str = "high") -> None:
        print(f"WARN: {detail}", file=sys.stderr)
        pending.append({"code": code, "detail": detail, "severity": severity})

    relaxed = ((((acc.get("ecu") or {}).get("daily_deployment_costs") or {}).get("status") or {}).get("reconciliation_relaxed"))
    if relaxed:
        stage(
            "billing_reconciliation_outside_window",
            f"daily deployment billing mismatched org billing on {relaxed['mismatch_days_total']} day(s), "
            f"{relaxed['mismatch_days_in_window']} of them inside the {relaxed['window_days']}-day quote window. "
            f"That is {relaxed['mismatch_days_in_window'] / relaxed['window_days']:.1%} of the window, under the 5% tolerance, "
            "so the ECH comparison was kept. The unattributed billing on those days is not in the comparison.",
            "medium",
        )

    if args.diags_dir:
        out_dir = Path(args.diags_dir)
        if not out_dir.is_dir():
            print(f"WARN: --diags-dir not found: {out_dir}", file=sys.stderr)
            sz["diagnostics_status"] = f"--diags-dir not found: {out_dir}"
            return None
    else:
        if not org_ids:
            print("WARN: no organization id — diagnostic consolidation unavailable", file=sys.stderr)
            sz["diagnostics_status"] = "no organization id"
            return None
        label = "+".join(org_ids) if len(org_ids) > 1 else org_ids[0]
        out_dir = folder.parent / f"{label}_diags_{when}"
        pull = True
        if not args.skip_vpn_check:
            from urllib.parse import urlsplit
            from admin_diags import admin_base
            from vpn_check import aviatrix_status
            try:
                host = urlsplit(admin_base(env)).hostname or "admin.found.no"
                vpn_up, vpn_detail = aviatrix_status(host)
            except (RuntimeError, ValueError) as ex:
                vpn_up, vpn_detail = False, str(ex)
            if not vpn_up:
                print(f"WARN: {vpn_detail}", file=sys.stderr)
                if not any(out_dir.glob("*.zip")):
                    sz["diagnostics_status"] = vpn_detail
                    return None
                stage(
                    "stale_diagnostic_bundles",
                    f"no fresh diagnostics could be pulled ({vpn_detail}), so the quote was measured from the "
                    f"diagnostic zips already in {out_dir}. Those bundles may predate the quote window.",
                )
                pull = False
        if pull:
            errors = []
            for oid in org_ids:
                try:
                    pulled = pull_diagnostics(env, oid, out_dir, exclude_ids=set(getattr(args, "exclude_ids", None) or []))
                    download_failed.extend(
                        {"name": r.get("name"), "id": r.get("id"), "error": r.get("error"), "attempts": r.get("attempts")}
                        for r in pulled if r.get("result") == "error"
                    )
                except Exception as ex:
                    errors.append(f"{oid}: {ex!r}")
                    stage("diagnostic_pull_failed", f"diagnostic pull failed for {oid} ({ex!r}). Deployments in that organization may have no bundle.")
            if download_failed and not getattr(args, "allow_missing_diags", False):
                names = ", ".join(str(r["name"]) for r in download_failed)
                raise SystemExit(
                    f"ERROR: {len(download_failed)} diagnostic download(s) failed after retries: {names}. "
                    "Rerun to collect them, or pass --allow-missing-diags to quote without them."
                )
            if errors and not any(out_dir.glob("*.zip")):
                sz["diagnostics_status"] = "; ".join(errors)
                return None
    zips = list(out_dir.glob("*.zip"))
    if not zips:
        print("WARN: no diagnostic bundles — no diagnostic Serverless estimate will be produced", file=sys.stderr)
        sz["diagnostics_status"] = "no diagnostic bundles"
        return None
    captured = datetime.fromtimestamp(max(p.stat().st_mtime for p in zips), timezone.utc)
    try:
        rows = parse_diagnostics(out_dir, captured=captured, exclude_ids=set(getattr(args, "exclude_ids", None) or []))
    except Exception as ex:
        print(f"WARN: diagnostic parse failed ({ex!r})", file=sys.stderr)
        sz["diagnostics_status"] = f"diagnostic parse failed ({ex!r})"
        return None
    if not rows:
        sz["diagnostics_status"] = "no diagnostic rows"
        return None
    admin_names = None
    try:
        from admin_diags import admin_base, list_deployments
        base = admin_base(env)
        key = env.get("FOUND_ADMIN_API_KEY")
        names: set[str] = set()
        for oid in org_ids:
            names.update(str(x.get("name")) for x in list_deployments(base, key, oid) if x.get("name"))
        admin_names = names or None
    except Exception as ex:
        stage(
            "admin_list_unavailable",
            f"the admin deployment list could not be read ({ex!r}), so a billed deployment with no bundle is "
            "counted as missing a diagnostic rather than decommissioned. Bundle coverage and priced share are "
            "understated by any deployment that no longer exists.",
        )
    options = quote_options_from(args)
    diag = apply_diagnostics(
        acc, rows, admin_names,
        unmeasured=options["unmeasured"],
    )
    missing = (diag["sizing"].get("diagnostics") or {}).get("fallback_names") or []
    if options["unmeasured"] == "fail" and missing:
        raise SystemExit(
            f"ERROR: {len(missing)} active deployment(s) have no diagnostic bundle: {', '.join(str(n) for n in missing)}. "
            "--unmeasured fail stops the run. Collect the diagnostic bundles or pass --unmeasured exclude."
        )
    for row in pending:
        note_limit(diag, **row)
    diag["sizing"]["quote_options"] = options
    diag.setdefault("meta", {})["method"] = method_stamp(
        args.lookback_days,
        retention_months=_written_months(diag, args),
        retention_days=(diag.get("meta") or {}).get("retention_days"),
        index_average=bool((diag.get("meta") or {}).get("retention_index_count")),
    )
    if download_failed:
        diag["sizing"].setdefault("diagnostics", {})["download_failed"] = download_failed
        names = ", ".join(str(row.get("name")) for row in download_failed)
        note_limit(
            diag, "diagnostic_download_failed",
            f"{len(download_failed)} diagnostic download(s) failed after retries and the run continued with "
            f"--allow-missing-diags: {names}. Those deployments add no ingest and no stored GB.",
        )
    apply_quote_lookback(diag, args.lookback_days)
    # Refresh only workload telemetry for the exact billed quote window. The
    # account report's usual 21-day workload sample cannot stand in for a
    # different billing window.
    try:
        # Search traffic is read from the service proxy request logs for each region's own Search deployments,
        # and ingest from the diagnostic bundles, so no workload is scaled by a store share.
        import search_inputs

        base, key = search_inputs.settings(env)
        fetched = fetch_proxy_workloads(
            lambda deadline: search_inputs.regional_workloads(
                diag, base, key, days=int(getattr(args, "proxy_days", 7) or 7), deadline=deadline,
            ),
            lock_path=folder.parent / ".telemetry.lock",
        )
        diag["workload"] = fetched
        (diag.get("sizing") or {}).get("consolidated", {}).setdefault("search", {})["workload"] = diag["workload"]
        for region, why in (fetched.get("errors") or {}).items():
            note_limit(diag, "search_proxy_workload_missing", f"Search in {region} was left unquoted: {why}")
    except (Exception, SystemExit) as ex:
        failed_workload = diag.setdefault("workload", {})
        failed_workload["quote_window_fetch_error"] = str(ex)
        failed_workload["quote_window_telemetry"] = {
            "status": "incomplete",
            "reason": f"proxy-log Search workload failed: {ex}",
        }
        (diag.get("sizing") or {}).get("consolidated", {}).setdefault("search", {})["workload"] = failed_workload
    apply_quote_lookback(diag, args.lookback_days)
    diag_path = folder / f"{acc['meta']['run_key']}_diags_account.json"
    seal_text(diag_path, json.dumps(diag, indent=2))
    py = _sheets_py()
    sizing_ran = False
    # The written month is the index average rounded up. --retention-days replaces it. With no month
    # ascertained none is forced: the sizing step leaves Observability and Security unfilled.
    written_months = _written_months(diag, args)
    extra = ["--sizing-json", "sizing.json"]
    if written_months is not None:
        extra += ["--force-months", str(written_months)]
    if args.fresh:
        extra.append("--fresh")
    if getattr(args, "search_notebook", False):
        # The projects and deployment-plan steps reuse `extra`, so the flag has to ride on it.
        extra.append("--search-notebook")
    if py.is_file():
        cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(diag_path), *extra]
        if args.xlsx:
            cmd.append("--xlsx")
        if args.always_copy:
            cmd.append("--always-copy")
        if args.fresh:
            cmd.append("--fresh")
        try:
            _run(cmd)
            sizing_ran = True
            diag = json.loads(diag_path.read_text())
        except subprocess.CalledProcessError as e:
            if not getattr(args, "allow_partial", False):
                print(f"ERROR: diagnostics sizing failed ({e}). The run stopped.", file=sys.stderr)
                raise SystemExit(2) from e
            warn(diag, "sizing_step_failed", f"the estimator sizing step failed ({e}), so no sheet read-back was collected. The document keeps the measured inputs and is a draft.")
            diag.setdefault("meta", {})["draft"] = True
    else:
        warn(diag, "sheets_venv_missing", f"the Sheets virtualenv is missing at {py}, so no estimator copy was filled and the quote has no sheet read-back. See references/setup.md.")
    try:
        diag["consolidation"] = build_consolidation(diag, infra)
    except Exception as ex:
        warn(diag, "consolidation_failed", f"building the consolidation failed ({ex!r}). No quote was produced.")
        diag["consolidation"] = {"triggered": True, "source": "diagnostics", "error": repr(ex)}
    seal_text(diag_path, json.dumps(diag, indent=2))
    if sizing_ran and (diag.get("consolidation") or {}).get("architecture", {}).get("per_region") and not (diag.get("consolidation") or {}).get("error"):
        try:
            cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(diag_path), "--projects", *extra]
            if args.xlsx:
                cmd.append("--xlsx")
            _run(cmd)
            diag = json.loads(diag_path.read_text())
            diag["consolidation"] = build_consolidation(diag, infra)
            seal_text(diag_path, json.dumps(diag, indent=2))
        except subprocess.CalledProcessError as e:
            if not getattr(args, "allow_partial", False):
                print(f"ERROR: diagnostics regional sizing failed ({e}). The run stopped.", file=sys.stderr)
                raise SystemExit(2) from e
            warn(diag, "regional_sizing_failed", f"per-region sizing failed ({e}), so each region is a stored-GB share of one fleet read-back instead of its own. The document is a draft.")
            diag.setdefault("meta", {})["draft"] = True
    if sizing_ran and py.is_file() and not (diag.get("consolidation") or {}).get("error"):
        if not args.skip_deployment_plan:
            plan_cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(diag_path), "--deployment-plan", *extra]
            if args.xlsx:
                plan_cmd.append("--xlsx")
            try:
                _run(plan_cmd)
            except subprocess.CalledProcessError as e:
                if not getattr(args, "allow_partial", False):
                    print(f"ERROR: deployment-count plan failed ({e}). The run stopped.", file=sys.stderr)
                    raise SystemExit(2) from e
                diag = json.loads(diag_path.read_text())
                warn(diag, "deployment_plan_failed", f"the deployment-count plan was not priced ({e}), so the quote has no consolidation bars and no per-cluster split. The document is a draft.")
                diag.setdefault("meta", {})["draft"] = True
                diag.setdefault("sizing", {})["deployment_plan"] = {
                    "bars": [],
                    "error": f"Observability deployment-count plan was not priced ({e}).",
                }
                seal_text(diag_path, json.dumps(diag, indent=2))
            diag = json.loads(diag_path.read_text())
            try:
                diag["consolidation"] = build_consolidation(diag, infra)
            except Exception as ex:
                warn(diag, "consolidation_failed", f"rebuilding the consolidation after the deployment plan failed ({ex!r}). No quote was produced.")
                diag["consolidation"] = {"triggered": True, "source": "diagnostics", "error": repr(ex)}
        if not (diag.get("consolidation") or {}).get("error"):
            diag.setdefault("consolidation", {})["retention_scenario"] = "rounded"
        seal_text(diag_path, json.dumps(diag, indent=2))
    seal_text(diag_path, json.dumps(diag, indent=2))
    sz["diagnostics_status"] = "ok"
    sz["diagnostics_dir"] = str(out_dir)
    return diag



def main() -> None:
    started = time.time()
    args = parse_args(description=__doc__)
    from diag_inputs import whole_months
    from constants import DAYS_PER_MONTH
    if getattr(args, "retention_days", None):
        if not 1 <= int(args.retention_days) <= 1095:
            raise SystemExit("--retention-days must be from 1 to 1095")
        args.estimate_months = whole_months(int(args.retention_days) / DAYS_PER_MONTH)
    else:
        args.estimate_months = ESTIMATE_MONTHS
    if getattr(args, "preflight", False):
        raise SystemExit(0 if print_preflight(preflight(args)) else 2)
    stamp = method_stamp(int(args.lookback_days))
    if not getattr(args, "allow_dirty", False):
        state = stamp.get("uncommitted_changes")
        if state is None:
            print(
                "ERROR: these scripts are not in a git checkout, so the tree cannot be proven clean. "
                "Pass --allow-dirty to mark the PDF DRAFT.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if state:
            extra = stamp.get("untracked_importable") or []
            detail = f" Untracked scripts an import could pick up: {', '.join(extra)}." if extra else ""
            print(
                "ERROR: uncommitted changes under serverless_analyzer."
                f"{detail} Commit them, or pass --allow-dirty to mark the PDF DRAFT.",
                file=sys.stderr,
            )
            raise SystemExit(2)
    if stamp.get("untracked_ignored"):
        print(
            f"NOTE: {len(stamp['untracked_ignored'])} untracked file(s) under serverless_analyzer cannot "
            "change what the scripts do, so they do not mark the run dirty. They are listed on the method stamp.",
            file=sys.stderr,
        )
    # --lookback-days and --min-priced-ecu-pct are validated by parse_args, which reports
    # them through argparse. A second copy here was unreachable and drifted out of step.
    checks = preflight(args, network=False)
    for row in checks:
        if row["ok"] is False and row["check"] in {".env", "org report"}:
            raise SystemExit(f"ERROR: {row['check']}: {row['detail']}")
        if row["ok"] is False and row["check"] in {"Sheets venv", "es3 estimator"}:
            print(f"WARN: {row['check']}: {row['detail']}", file=sys.stderr)
    if args.keep_serverless_spend and args.ech_scope == "priced":
        print("NOTE: --keep-serverless-spend has no effect with --ech-scope priced; only priced deployments are compared.", file=sys.stderr)
    # A consolidation run leaves stack-monitoring clusters out. The account summary does not.
    args.auto_exclude_monitoring = not getattr(args, "keep_monitoring", False)
    acc, infra, folder, env, acc_path = prepare_account(args)
    # Until a diagnostic quote replaces it, this account is the K360 backup.
    acc.setdefault("meta", {})["method"] = method_stamp(args.lookback_days, diagnostic=False)
    acc.setdefault("sizing", {})["quote_options"] = quote_options_from(args)
    if args.skip_diags and args.skip_sizing:
        finish_account(acc, folder, acc_path, args)
        return
    if not (acc.get("ecu") or {}).get("daily_totals"):
        raise SystemExit(
            "ERROR: this org report has no daily billing series, so there is no quote window to price. "
            "It was fetched at week grain (run_org_report.py writes that). Run without --reuse, or reuse an "
            "org report written by run_account_analysis.py or run_consolidation.py."
        )
    # 4. Diagnostics are the default ingest. K360 stored-GB ÷ retention is the backup.
    diag_acc = None
    if acc["sizing"]["triggered"] and not args.skip_diags:
        try:
            diag_acc = _diagnostics_document(acc, infra, folder, env, args)
        except Exception as ex:
            warn(acc, "diagnostics_unavailable", f"the diagnostic consolidation failed ({ex!r}). No measured quote was produced.")
            acc["sizing"]["diagnostics_status"] = f"diagnostics consolidation failed ({ex!r})"
            diag_acc = None
    seal_text(acc_path, json.dumps(acc, indent=2))

    k360_for_pdf = None
    quote_k360 = acc["sizing"]["triggered"] and args.k360_ingest
    if quote_k360:
        acc.setdefault("sizing", {})["ingest_source"] = "k360"
        # The backup is priced on the same billing window as a diagnostic quote, so --lookback-days
        # and --ech-scope mean the same thing on both. Its stored-GB ingest takes the window's pace.
        apply_quote_lookback(acc, args.lookback_days)
        backup = diag_acc is not None
        extra = ["--name-suffix", "from_k360", "--sizing-json", "sizing_from_k360.json"] if backup else []
        sizing_ran = False
        py = _sheets_py()
        if not py.is_file():
            warn(acc, "sheets_venv_missing", f"the Sheets virtualenv is missing at {py}, so no estimator copy was filled. See references/setup.md.")
            acc["sizing"]["estimators"] = {"error": f"sheets venv missing: {py}"}
            seal_text(acc_path, json.dumps(acc, indent=2))
        else:
            cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(acc_path), *extra]
            if args.xlsx:
                cmd.append("--xlsx")
            if args.always_copy:
                cmd.append("--always-copy")
            if args.fresh:
                cmd.append("--fresh")
            try:
                _run(cmd)
                sizing_ran = True
                acc = json.loads(acc_path.read_text())
            except subprocess.CalledProcessError as e:
                if not getattr(args, "allow_partial", False):
                    print(f"ERROR: K360 sizing step failed ({e}). The run stopped.", file=sys.stderr)
                    raise SystemExit(2) from e
                warn(acc, "sizing_step_failed", f"the K360 backup sizing step failed ({e}), so it has no sheet read-back. The report is a draft.")
                acc.setdefault("meta", {})["draft"] = True
                acc["sizing"]["estimators"] = {"error": f"size_serverless.py exit {e.returncode}"}
                seal_text(acc_path, json.dumps(acc, indent=2))
        try:
            acc["consolidation"] = build_consolidation(acc, infra)
        except Exception as ex:
            warn(acc, "consolidation_failed", f"building the K360 backup consolidation failed ({ex!r}).")
            acc["consolidation"] = {"triggered": True, "source": "k360", "error": repr(ex)}
        if diag_acc is None:
            why = (acc.get("sizing") or {}).get("diagnostics_status") or "diagnostic bundles were not available"
            notes = (acc.get("consolidation") or {}).setdefault("notes", [])
            notes.insert(0, f"Diagnostic bundles were not used ({why}). This quote is the K360 backup.")
        seal_text(acc_path, json.dumps(acc, indent=2))
        if sizing_ran and (acc.get("consolidation") or {}).get("architecture", {}).get("per_region") and not (acc.get("consolidation") or {}).get("error"):
            try:
                cmd = [str(py), str(HERE / "size_serverless.py"), "--account-json", str(acc_path), "--projects", *extra]
                if args.xlsx:
                    cmd.append("--xlsx")
                _run(cmd)
                acc = json.loads(acc_path.read_text())
                acc["consolidation"] = build_consolidation(acc, infra)
                if diag_acc is None:
                    why = (acc.get("sizing") or {}).get("diagnostics_status") or "diagnostic bundles were not available"
                    acc["consolidation"].setdefault("notes", []).insert(0, f"Diagnostic bundles were not used ({why}). This quote is the K360 backup.")
                seal_text(acc_path, json.dumps(acc, indent=2))
            except subprocess.CalledProcessError as e:
                if not getattr(args, "allow_partial", False):
                    print(f"ERROR: regional project sizing failed ({e}). The run stopped.", file=sys.stderr)
                    raise SystemExit(2) from e
                warn(acc, "regional_sizing_failed", f"per-region sizing failed ({e}), so each region is a stored-GB share of one fleet read-back. The report is a draft.")
                acc.setdefault("meta", {})["draft"] = True
        if backup:
            acc["k360_ingest"] = _quote_block(acc)
    elif acc["sizing"]["triggered"] and diag_acc is None:
        why = (acc.get("sizing") or {}).get("diagnostics_status") or "diagnostic bundles were not available"
        acc["consolidation"] = {
            "triggered": True,
            "source": "unavailable",
            "error": f"No measured ingest quote was produced: {why}. K360-derived ingest fallback is disabled.",
            "quotes": [],
            "notes": [
                f"No diagnostic ingest measurement was available ({why}). No Serverless estimate is quoted."
            ],
        }
        acc.setdefault("sizing", {})["ingest_source"] = "unavailable"
        seal_text(acc_path, json.dumps(acc, indent=2))

    if diag_acc is not None and acc.get("k360_ingest"):
        diag_mo = ((diag_acc.get("consolidation") or {}).get("savings") or {}).get("serverless_mo")
        k360_mo = (acc["k360_ingest"].get("savings") or {}).get("serverless_mo")
        if diag_mo is not None and k360_mo is not None:
            note = _money_note(diag_mo, k360_mo)
            acc["k360_ingest"].setdefault("notes", []).insert(0, note)
            (diag_acc.get("consolidation") or {}).setdefault("notes", []).insert(0, note)
            (acc.get("consolidation") or {}).setdefault("notes", []).insert(0, note)
        k360_for_pdf = copy.deepcopy(acc)

    if diag_acc is not None and not (diag_acc.get("consolidation") or {}).get("error"):
        dsz = diag_acc.get("sizing") or {}
        sz = acc.setdefault("sizing", {})
        for key in (
            "estimators", "consolidated", "notes", "diagnostics", "drive_folder", "drive_root",
            "sized_at", "source_precedence",
        ):
            if dsz.get(key) not in (None, {}, []):
                sz[key] = dsz[key]
        sz["ingest_source"] = "diagnostics"
        # Keep report-visible sizing measurements on the same diagnostic view as
        # the quote. Billing, roster, workload rates, and infrastructure metadata
        # remain from Cloud Tracker because diagnostics do not measure them.
        _apply_diagnostic_report_view(acc, diag_acc)
        acc["consolidation"] = diag_acc.get("consolidation")
        acc.setdefault("meta", {})["method"] = (diag_acc.get("meta") or {}).get("method")
        # The account now carries the diagnostic quote, so it carries that quote's window figures too.
        for key in ("quote_lookback_requested", "quote_lookback_days", "quote_daily_avg", "quote_monthly_ecu", "quote_usage_factor", "quote_ingest_factor"):
            if key in (diag_acc.get("ecu") or {}):
                acc.setdefault("ecu", {})[key] = diag_acc["ecu"][key]
        cost = (diag_acc.get("analysis") or {}).get("cost")
        if cost:
            acc.setdefault("analysis", {})["cost"] = cost
        seal_text(acc_path, json.dumps(acc, indent=2))

    # 4b. One estimate per title: after every sizing pass, trash older same-titled sheets.
    dedupe_py = _sheets_py()
    sized_acc = diag_acc if diag_acc is not None else acc
    if dedupe_py.is_file() and (sized_acc.get("sizing") or {}).get("drive_folder"):
        dedupe_src = folder / f"{acc['meta']['run_key']}_diags_account.json"
        if not dedupe_src.is_file():
            dedupe_src = acc_path
        try:
            _run([str(dedupe_py), str(HERE / "size_serverless.py"), "--account-json", str(dedupe_src), "--dedupe"])
        except subprocess.CalledProcessError as e:
            warn(acc, "estimate_dedupe_failed", f"removing older same-titled estimates failed ({e}). The Drive folder may hold more than one copy of a title.", "low")

    # 5. report
    run_key = acc["meta"]["run_key"]
    report_path = folder / f"{run_key}_account_report.md"
    seal_text(report_path, render(acc))

    # 6. PDF export. Every finished scenario is printed on one Chrome pool.
    if not args.skip_pdf:
        try:
            k360_doc = k360_for_pdf if k360_for_pdf and (k360_for_pdf.get("consolidation") or {}).get("source") == "k360" and not (k360_for_pdf.get("consolidation") or {}).get("error") else None
            if diag_acc is not None and not (diag_acc.get("consolidation") or {}).get("error"):
                jobs = quote_pdf_jobs(acc, diag_acc, [
                    (k360_doc, "consolidation_k360"),
                ])
            else:
                jobs = quote_pdf_jobs(acc, None, [])
            acc["exports"] = export_documents(jobs, folder)
            for kind, e in acc["exports"].items():
                if isinstance(e, dict) and e.get("pdf"):
                    print(f"{kind} PDF → {e['pdf']}", flush=True)
                elif isinstance(e, dict) and e.get("html"):
                    print(f"{kind} HTML → {e['html']}", flush=True)
                else:
                    print(f"{kind} → {e.get('error') if isinstance(e, dict) else e}", flush=True)
        except Exception as ex:
            warn(acc, "pdf_export_failed", f"the PDF export failed ({ex!r}).", "medium")
            acc["exports"] = {"error": repr(ex)}
        seal_text(acc_path, json.dumps(acc, indent=2))
        py = _sheets_py()
        if not args.skip_pdf_upload and (acc["sizing"].get("drive_folder") or {}).get("id") and py.is_file() and any((e or {}).get("pdf") for e in acc["exports"].values() if isinstance(e, dict)):
            try:
                _run([str(py), str(K360_SCRIPTS / "upload_pdfs.py"), "--account-json", str(acc_path)])
                acc = json.loads(acc_path.read_text())
            except subprocess.CalledProcessError as e:
                warn(acc, "pdf_upload_failed", f"the PDF upload to Drive failed ({e}). The PDFs are on disk only.", "low")

    brief = agent_brief(acc)
    brief_path = folder / f"{run_key}_brief.json"
    seal_text(brief_path, json.dumps(brief, separators=(",", ":")))
    printed = brief_path
    bullets = brief["chat_bullets"]
    if brief.get("ingest_source") == "diagnostics" or (acc.get("sizing") or {}).get("ingest_source") == "diagnostics":
        quote = consolidation_brief(acc)
        printed = folder / f"{run_key}_consolidation_brief.json"
        seal_text(printed, json.dumps(quote, separators=(",", ":")))
        bullets = chat_lines(quote)
    est_in = acc["sizing"].get("estimators") if isinstance(acc["sizing"].get("estimators"), dict) else {}
    seal_text(folder / f"{run_key}_summary.json", json.dumps({
        "run_key": run_key,
        "chat_bullets": bullets,
        "brief": str(printed),
        "links": brief["links"],
        "estimators": {k: view for k, v in est_in.items() if (view := _est(v))},
        "savings": brief["savings"],
        "ingest_source": brief.get("ingest_source"),
        "k360_ingest": brief.get("k360_ingest"),
    }, separators=(",", ":")))
    manifest_path = folder / f"{run_key}_run.json"
    seal_text(manifest_path, json.dumps(run_manifest(args, acc, started, preflight_rows=checks), indent=2))
    print(f"brief {printed}", flush=True)
    print(f"run {manifest_path}", flush=True)


if __name__ == "__main__":
    main()
