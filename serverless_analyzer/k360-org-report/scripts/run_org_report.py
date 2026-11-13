#!/usr/bin/env python3
"""One-shot org report pipeline (fetch → slim → estimators → PDF).

Minimizes agent turns: run this once, then build canvas from *_canvas.json only.

  python run_org_report.py --org "ORG NAME" --report exec|full
  python run_org_report.py --org "ORG NAME" --report full --reuse <artifacts folder>/<key>_org_report.json

`--reuse` takes the org JSON this pipeline wrote into that customer's artifacts
folder. It stops when the file is another organization's.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from skill_paths import artifacts_root
from report_mode import (
    CANVAS_LAYOUT,
    add_report_arg,
    canvas_suggestion,
    normalize_report,
    html_name,
    pdf_name,
    write_private,
)

REUSE_WARN_DAYS = 7


def _run(cmd: list[str], env: dict | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, env=env)


def _py() -> str:
    return sys.executable


def _sheets_py() -> str:
    return os.environ.get(
        "SERVERLESS_SHEETS_PY",
        str(Path.home() / ".config/serverless-analyzer/venv/bin/python"),
    )


def _repo_root(scripts: Path) -> Path:
    """Project that holds .env. The current directory wins, then any parent of this skill."""
    if (Path.cwd() / ".env").is_file():
        return Path.cwd()
    for parent in scripts.parents:
        if (parent / ".env").is_file():
            return parent
    return Path.cwd()


def fetch_day(org: dict) -> date | None:
    """The day an org JSON was fetched: `generated_at`, else the end of its range."""
    for text in (org.get("generated_at"), (org.get("range") or {}).get("end")):
        try:
            return date.fromisoformat(str(text or "")[:10])
        except ValueError:
            continue
    return None


def check_reused_org(org: dict, wanted: str, org_ids: list[str] | None = None, today: date | None = None) -> list[str]:
    """Stop when a --reuse file is another organization's. Returns warnings to print.

    The file's organization name has to be the one asked for, or start with it,
    ignoring case. With --org-id, at least one id has to be in the file. Nothing
    is fetched under --reuse, so this is the only place a wrong file is caught.
    """
    info = org.get("organization") or {}
    have = " ".join(str(info.get("name") or "").split())
    want = " ".join(str(wanted or "").split())
    if not have or not want or not have.casefold().startswith(want.casefold()):
        raise SystemExit(
            f"ERROR: --reuse is the org report for {have or 'an unnamed organization'!r}, and --org is {want!r}. "
            "Pass the org report for that customer, or drop --reuse to fetch it."
        )
    asked = {str(i) for i in (org_ids or []) if i}
    ids = {str(i) for i in ([info.get("id")] + list(info.get("ids") or [])) if i}
    if asked and ids and not (asked & ids):
        raise SystemExit(
            f"ERROR: --reuse covers organization ids {', '.join(sorted(ids))}, and --org-id is {', '.join(sorted(asked))}."
        )
    warnings: list[str] = []
    if have.casefold() != want.casefold():
        warnings.append(f"NOTE: --org {want!r} matched the reused report for {have!r}.")
    day = fetch_day(org)
    today = today or datetime.now(timezone.utc).date()
    if day is not None and (today - day).days > REUSE_WARN_DAYS:
        warnings.append(
            f"WARN: the reused org report was fetched on {day.isoformat()}, {(today - day).days} days ago. "
            "Its billing window ends on that day, not today. Drop --reuse for current billing."
        )
    return warnings


def fetch_command(args, scripts: Path, out: Path) -> list[str]:
    """The fetch_org.py call for this run. Every --org-id is passed through."""
    cmd = [
        _py(),
        str(scripts / "fetch_org.py"),
        "--org",
        args.org,
        "--env",
        args.env,
        "--days",
        str(args.days),
        "--out",
        str(out),
        # compact JSON (no --pretty) — smaller on disk/context
    ]
    for oid in args.org_id or []:
        cmd += ["--org-id", str(oid)]
    if args.skip_workload:
        cmd.append("--skip-workload")
    return cmd


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", required=True)
    ap.add_argument(
        "--org-id",
        action="append",
        default=None,
        help="Cloud organization id, passed to fetch_org.py. Repeat when one customer name covers several orgs",
    )
    ap.add_argument("--env", default=".env")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--reuse", default=None, help="Skip fetch; use existing org_report.json")
    ap.add_argument("--skip-push", action="store_true")
    ap.add_argument("--skip-pdf", action="store_true")
    ap.add_argument("--skip-workload", action="store_true")
    ap.add_argument("--artifacts-root", default=None, help="Parent of <customer>_<date>/ (default: serverless_analyzer/diag_artifacts)")
    add_report_arg(ap)
    args = ap.parse_args()
    report = normalize_report(args.report)

    scripts = Path(__file__).resolve().parent
    # A relative --reuse is relative to where the command was typed, not to the .env folder.
    reuse = Path(args.reuse).expanduser().resolve() if args.reuse else None
    root = _repo_root(scripts)
    os.chdir(root)

    art_root = args.artifacts_root or str(artifacts_root())
    tmp_dir = None
    if reuse is not None:
        org_json = reuse
        if not org_json.is_file():
            raise SystemExit(f"--reuse not found: {org_json}")
        for line in check_reused_org(json.loads(org_json.read_text()), args.org, args.org_id):
            print(line, file=sys.stderr, flush=True)
    else:
        # A directory of its own, readable only by this user. A fixed /tmp/org_report.json was
        # shared by every run, so a later run could pick up another customer's file.
        tmp_dir = Path(tempfile.mkdtemp(prefix="k360_org_"))
        org_json = tmp_dir / "org_report.json"
        try:
            _run(fetch_command(args, scripts, org_json))
        except BaseException:
            org_json.unlink(missing_ok=True)
            tmp_dir.rmdir()
            raise

    # Seed artifacts dir via emit
    emit_cmd = [
        _py(),
        str(scripts / "emit_canvas_data.py"),
        "--org-json",
        str(org_json),
        "--report",
        report,
        "--artifacts-root",
        art_root,
    ]
    info = json.loads(subprocess.check_output(emit_cmd, text=True))
    folder = Path(info["folder"])

    # The full org JSON lives in the artifacts folder. That copy is the one to --reuse.
    dest_org = folder / f"{info['run_key']}_org_report.json"
    if org_json.resolve() != dest_org.resolve():
        write_private(dest_org, org_json.read_text())
    if tmp_dir is not None:
        org_json.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass
    if org_json.resolve() != dest_org.resolve():
        # The summary names the org JSON it was built from. Point it at the copy that stays.
        subprocess.check_output(
            [_py(), str(scripts / "emit_canvas_data.py"), "--org-json", str(dest_org), "--out-dir", str(folder), "--report", report],
            text=True,
        )

    sheets = _sheets_py()
    if not args.skip_push:
        if not Path(sheets).is_file():
            print("WARN: sheets venv missing — skip push", file=sys.stderr)
        else:
            _run(
                [
                    sheets,
                    str(scripts / "push_estimator.py"),
                    "--org-json",
                    str(dest_org),
                    "--kind",
                    "both",
                    "--read-back",
                    "--artifacts-root",
                    str(folder.parent),
                    "--report",
                    report,
                ]
            )
            # refresh slim offload/summary after estimator_artifacts written
            _run(
                [
                    _py(),
                    str(scripts / "emit_canvas_data.py"),
                    "--org-json",
                    str(dest_org),
                    "--out-dir",
                    str(folder),
                    "--report",
                    report,
                ]
            )

    if not args.skip_pdf:
        key = info["run_key"]
        _run(
            [
                _py(),
                str(scripts / "render_org_pdf.py"),
                "--canvas",
                str(folder / f"{key}_canvas.json"),
                "--offload",
                str(folder / f"{key}_offload.json"),
                "--org-json",
                str(dest_org),
                "--out-dir",
                str(folder),
                "--report",
                report,
            ]
        )

    key = info["run_key"]
    summary_path = folder / f"{key}_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {}
    slug = key.rsplit("_", 1)[0] if "_" in key else key
    canvas_file = canvas_suggestion(slug, report)
    html_path = folder / html_name(key, report)
    pdf_path = folder / pdf_name(key, report)
    result = {
        "ok": True,
        "report": report,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "folder": str(folder),
        "org_json": str(dest_org),
        "canvas": str(folder / f"{key}_canvas.json"),
        "offload": str(folder / f"{key}_offload.json"),
        "summary": str(summary_path),
        "html": str(html_path) if html_path.is_file() else None,
        "pdf": str(pdf_path) if pdf_path.is_file() else None,
        "canvas_file_hint": canvas_file,
        "canvas_layout": CANVAS_LAYOUT[report],
        "chat_bullets_hint": summary.get("chat_bullets_hint"),
        "caveats": summary.get("caveats"),
        "reuse": f"--reuse {dest_org}",
        "next": [
            f"Build canvas `{canvas_file}` ({CANVAS_LAYOUT[report]}) from *_canvas.json + *_offload.json ONLY",
            "Chat ≤5 bullets from *_summary.json",
        ],
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
