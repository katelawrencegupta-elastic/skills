#!/usr/bin/env python3
"""One-shot org report pipeline (fetch → slim → estimators → PDF).

Minimizes agent turns: run this once, then build canvas from *_canvas.json only.

  python run_org_report.py --org "GlaxoSmithKline" --report exec|full
  python run_org_report.py --org "GlaxoSmithKline" --report full --reuse /tmp/org_report.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from report_mode import (
    CANVAS_LAYOUT,
    add_report_arg,
    canvas_suggestion,
    normalize_report,
    pdf_name,
)


def _run(cmd: list[str], env: dict | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, env=env)


def _py() -> str:
    return sys.executable


def _sheets_py() -> str:
    return os.environ.get(
        "CLOUD_TRACKER_SHEETS_PY",
        str(Path.home() / ".config/cloud-tracker-sheets/venv/bin/python"),
    )


def _repo_root(scripts: Path) -> Path:
    """Project that holds .env. The current directory wins, then any parent of this skill."""
    if (Path.cwd() / ".env").is_file():
        return Path.cwd()
    for parent in scripts.parents:
        if (parent / ".env").is_file():
            return parent
    return Path.cwd()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", required=True)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--reuse", default=None, help="Skip fetch; use existing org_report.json")
    ap.add_argument("--skip-push", action="store_true")
    ap.add_argument("--skip-pdf", action="store_true")
    ap.add_argument("--skip-workload", action="store_true")
    ap.add_argument("--artifacts-root", default=None)
    add_report_arg(ap)
    args = ap.parse_args()
    report = normalize_report(args.report)

    scripts = Path(__file__).resolve().parent
    root = _repo_root(scripts)
    os.chdir(root)

    tmp_org = Path("/tmp/org_report.json")
    if args.reuse:
        org_json = Path(args.reuse)
        if not org_json.is_file():
            raise SystemExit(f"--reuse not found: {org_json}")
    else:
        fetch = [
            _py(),
            str(scripts / "fetch_org.py"),
            "--org",
            args.org,
            "--env",
            args.env,
            "--days",
            str(args.days),
            "--out",
            str(tmp_org),
            # compact JSON (no --pretty) — smaller on disk/context
        ]
        if args.skip_workload:
            fetch.append("--skip-workload")
        _run(fetch)
        org_json = tmp_org

    data = json.loads(org_json.read_text())
    # Seed artifacts dir via emit
    emit_cmd = [
        _py(),
        str(scripts / "emit_canvas_data.py"),
        "--org-json",
        str(org_json),
        "--report",
        report,
    ]
    info = json.loads(subprocess.check_output(emit_cmd, text=True))
    folder = Path(info["folder"])
    # Prefer artifacts under repo
    if args.artifacts_root:
        key = info["run_key"]
        folder = Path(args.artifacts_root) / key
        folder.mkdir(parents=True, exist_ok=True)
        info = json.loads(
            subprocess.check_output(
                emit_cmd + ["--out-dir", str(folder)],
                text=True,
            )
        )
        folder = Path(info["folder"])

    # Copy full org json into artifacts (scripts already may have)
    dest_org = folder / f"{info['run_key']}_org_report.json"
    if org_json.resolve() != dest_org.resolve():
        dest_org.write_text(org_json.read_text())
        # keep /tmp pointer for push defaults
        tmp_org.write_text(dest_org.read_text())

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
    result = {
        "ok": True,
        "report": report,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "folder": str(folder),
        "org_json": str(dest_org),
        "canvas": str(folder / f"{key}_canvas.json"),
        "offload": str(folder / f"{key}_offload.json"),
        "summary": str(summary_path),
        "pdf": str(folder / pdf_name(key, report)),
        "canvas_file_hint": canvas_file,
        "canvas_layout": CANVAS_LAYOUT[report],
        "chat_bullets_hint": summary.get("chat_bullets_hint"),
        "next": [
            f"Build canvas `{canvas_file}` ({CANVAS_LAYOUT[report]}) from *_canvas.json + *_offload.json ONLY",
            "Chat ≤5 bullets from *_summary.json",
        ],
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
