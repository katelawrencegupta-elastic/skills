#!/usr/bin/env python3
"""Render Org report HTML+PDF into the artifacts folder and upload to Drive.

  python render_org_pdf.py --report exec --canvas …/…_canvas.json
  python render_org_pdf.py --report full --org-json /tmp/org_report.json
"""
from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
from pathlib import Path

from report_mode import add_report_arg, html_name, normalize_report, pdf_name


def _esc(x) -> str:
    return html.escape("" if x is None else str(x))


def _money(n) -> str:
    try:
        return f"${float(n):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _num(n, d=0) -> str:
    try:
        return f"{float(n):,.{d}f}"
    except (TypeError, ValueError):
        return "—"


def _find_chrome() -> str:
    candidates = [
        os.environ.get("CHROME_PATH"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    raise SystemExit("Chrome not found — set CHROME_PATH")


def render_html(canvas: dict, offload: dict, report: str = "exec") -> str:
    org = _esc(canvas.get("org"))
    bd = canvas.get("burndown") or {}
    dep = canvas.get("deployments") or {}
    infra = canvas.get("infrastructure") or {}
    storage = canvas.get("storage") or {}
    fs = offload.get("full_split") or {}
    lines = fs.get("lines") or []
    items = canvas.get("items") or []
    providers = infra.get("providers") or []
    regions = infra.get("regions") or []
    cloud = providers[0] if providers else "—"
    region = regions[0] if regions else "—"
    ver = dep.get("by_version") or []
    v8 = sum(x.get("count") or 0 for x in ver if str(x.get("version", "")).startswith("8."))
    v9 = sum(x.get("count") or 0 for x in ver if str(x.get("version", "")).startswith("9."))
    kind_label = "Full report" if report == "full" else "Exec report"
    top_n = 15 if report == "full" else 8

    snap = (
        f"{_esc(bd.get('level'))}: runout {_esc(bd.get('runout_date'))} "
        f"({_esc(bd.get('runway_days'))}d) before {_esc(bd.get('nearest_contract_end'))}. "
        f"Ballpark serverless save {_money(fs.get('save_vs_ech_yr'))}/yr."
    )

    dep_rows = "".join(
        f"<tr><td class='name'>{_esc(i.get('name'))}</td><td>{_esc(i.get('version'))}</td>"
        f"<td class='num'>{_num(i.get('ecu_monthly_avg') or i.get('ecu'), 1)}</td>"
        f"<td class='num'>{_num(i.get('ml_ecu'), 0) if i.get('ml_ecu') else '—'}</td></tr>"
        for i in items[:top_n]
    )
    save_rows = "".join(
        f"<tr><td>{_esc(r.get('name'))}</td><td>{_esc(r.get('project'))}</td>"
        f"<td class='num'>{_money(r.get('ech_mo'))}</td>"
        f"<td class='num'>{_money(r.get('serverless_mo'))}</td>"
        f"<td class='num'>{_money(r.get('save_mo'))}</td></tr>"
        for r in lines
    )
    save_rows += (
        f"<tr><td class='name'>Total</td><td></td>"
        f"<td class='num'>{_money(fs.get('full_ech_mo'))}</td>"
        f"<td class='num'>{_money(fs.get('serverless_total_mo'))}</td>"
        f"<td class='num'>{_money(fs.get('save_vs_ech_mo'))}</td></tr>"
    )

    recs = [
        "P0 — Address overage / runway risk before contract end",
        "P1 — Move o11y+logs to Observability Serverless (largest save line)",
        "P2 — Right-size idle/low-QPS search deps; ES3 for search-only GB",
        "P3 — Security project (Complete proxy) — confirm with Security estimator/ETT",
    ]
    if not bd.get("priority"):
        recs[0] = "P0 — Monitor burndown; no priority overage flag"

    rec_html = "".join(f"<li>{_esc(r)}</li>" for r in recs)

    env_heading = (
        "1–10. Environment & analysis (full)"
        if report == "full"
        else "1–8. Environment summary"
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>{org} — {kind_label}</title>
<style>
@page {{ size: letter; margin: 0.55in; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  color:#111827; background:#fff; margin:0; font-size:12px; line-height:1.4; }}
h1 {{ font-size:22px; margin:0 0 4px; font-weight:650; }}
h2 {{ font-size:14px; margin:0 0 8px; font-weight:600; }}
h3 {{ font-size:12.5px; margin:14px 0 6px; font-weight:600; color:#374151; }}
.meta {{ color:#6b7280; margin-bottom:12px; }}
.pill {{ border:1px solid #e5e7eb; border-radius:999px; padding:2px 10px; font-size:11px;
  color:#374151; background:#f9fafb; margin-right:6px; }}
.pill.warn {{ background:#fffbeb; border-color:#fcd34d; color:#92400e; }}
.callout {{ border:1px solid #fcd34d; background:#fffbeb; border-radius:8px; padding:10px 12px; margin:10px 0 14px; }}
.callout .t {{ font-weight:600; margin-bottom:4px; }}
.stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin:8px 0 12px; }}
.stat {{ border:1px solid #e5e7eb; border-radius:8px; padding:8px 10px; }}
.stat .v {{ font-size:16px; font-weight:650; }}
.stat .l {{ font-size:10.5px; color:#6b7280; margin-top:2px; }}
.card {{ border:1px solid #e5e7eb; border-radius:8px; padding:12px; margin:12px 0; break-inside:avoid; }}
table {{ width:100%; border-collapse:collapse; font-size:11px; }}
th, td {{ padding:5px 6px; border-bottom:1px solid #e5e7eb; text-align:left; }}
th {{ color:#6b7280; font-weight:550; font-size:10.5px; }}
td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
td.name {{ font-weight:550; }}
.footer {{ color:#9ca3af; font-size:10px; margin-top:18px; }}
</style></head><body>
<div><h1 style="display:inline">{org}</h1>
<span class="pill">{kind_label}</span>
<span class="pill">{_esc(canvas.get('segment'))}</span>
<span class="pill">{_esc(canvas.get('geo'))}</span>
<span class="pill warn">{_esc(bd.get('level'))}</span></div>
<div class="meta">{_esc(canvas.get('range'))} · refreshed {_esc(canvas.get('generated_at'))} · org {_esc(canvas.get('org_id'))}</div>
<div class="callout"><div class="t">Executive snapshot</div>{snap}</div>
<section class="card"><h2>{env_heading}</h2>
<div class="stats">
<div class="stat"><div class="v">{_esc(cloud)}</div><div class="l">Cloud</div></div>
<div class="stat"><div class="v">{_esc(region)}</div><div class="l">Region</div></div>
<div class="stat"><div class="v">{_esc(dep.get('ech'))} ECH</div><div class="l">Deployments</div></div>
<div class="stat"><div class="v">{v8} / {v9}</div><div class="l">8.x / 9.x</div></div>
</div>
<div class="stats">
<div class="stat"><div class="v">{_num(canvas.get('total_ecu'),0)}</div><div class="l">ECU (lookback)</div></div>
<div class="stat"><div class="v">{_num(canvas.get('ecu_monthly_avg'),0)}</div><div class="l">ECU / mo</div></div>
<div class="stat"><div class="v">{_num(storage.get('data_stored_gb'),1)} GB</div><div class="l">Data stored</div></div>
<div class="stat"><div class="v">{_esc(bd.get('runway_days'))}d</div><div class="l">Runway → {_esc(bd.get('runout_date'))}</div></div>
</div>
<h3>Top deployments</h3>
<table><thead><tr><th>Deployment</th><th>Ver</th><th class="num">ECU / mo</th><th class="num">ML</th></tr></thead>
<tbody>{dep_rows}</tbody></table>
</section>
<section class="card"><h2>9. Projected usage &amp; recommendations</h2>
<ul>{rec_html}</ul>
<p class="meta">Projection {_money(bd.get('annual_projection'))}/yr · ARR {_money(canvas.get('arr'))}</p>
</section>
<section class="card"><h2>10. Serverless move analysis (full_split)</h2>
<table><thead><tr><th>Project</th><th>Method</th><th class="num">ECH $/mo</th>
<th class="num">Serverless $/mo</th><th class="num">Save $/mo</th></tr></thead>
<tbody>{save_rows}</tbody></table>
<p class="meta">Save {_money(fs.get('save_vs_ech_yr'))}/yr · 3yr {_money(fs.get('save_vs_ech_3yr'))}.
Retention { _esc(fs.get('retention_months_assumed')) } mo · Security = Complete proxy · confirm estimators/ETT.
Do not put all data on ES3 Search (all_es3 anti-pattern).</p>
</section>
<div class="footer">Generated by cloud-tracker-org-report · render_org_pdf.py</div>
</body></html>
"""


def chrome_pdf(chrome: str, html_path: Path, pdf_path: Path) -> None:
    html_uri = html_path.resolve().as_uri()
    cmd = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}",
        html_uri,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--canvas", default=None)
    ap.add_argument("--offload", default=None)
    ap.add_argument("--org-json", default=None, help="If set, emit canvas/offload when missing")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--skip-upload", action="store_true")
    ap.add_argument("--skip-pdf", action="store_true", help="HTML only")
    add_report_arg(ap)
    args = ap.parse_args()
    report = normalize_report(args.report)

    scripts = Path(__file__).resolve().parent
    canvas_path = Path(args.canvas) if args.canvas else None
    offload_path = Path(args.offload) if args.offload else None
    org_json = Path(args.org_json) if args.org_json else None

    if org_json and (not canvas_path or not canvas_path.is_file()):
        out_dir = Path(args.out_dir) if args.out_dir else None
        cmd = [
            sys.executable,
            str(scripts / "emit_canvas_data.py"),
            "--org-json",
            str(org_json),
            "--report",
            report,
        ]
        if out_dir:
            cmd += ["--out-dir", str(out_dir)]
        info = json.loads(subprocess.check_output(cmd, text=True))
        canvas_path = Path(info["canvas"])
        offload_path = Path(info["offload"])
        out_dir = Path(info["folder"])
    else:
        if not canvas_path or not canvas_path.is_file():
            raise SystemExit("Need --canvas or --org-json")
        out_dir = Path(args.out_dir) if args.out_dir else canvas_path.parent
        if offload_path is None:
            # convention: sibling *_offload.json
            guess = canvas_path.name.replace("_canvas.json", "_offload.json")
            offload_path = canvas_path.parent / guess

    canvas = json.loads(canvas_path.read_text())
    offload = json.loads(offload_path.read_text()) if offload_path.is_file() else {}
    key = canvas_path.name.replace("_canvas.json", "")
    html_path = out_dir / html_name(key, report)
    pdf_path = out_dir / pdf_name(key, report)
    html_path.write_text(render_html(canvas, offload, report=report))
    print(f"HTML → {html_path}", flush=True)

    if not args.skip_pdf:
        chrome_pdf(_find_chrome(), html_path, pdf_path)
        print(f"PDF  → {pdf_path}", flush=True)

    if not args.skip_upload and not args.skip_pdf and org_json and org_json.is_file():
        py = os.environ.get(
            "CLOUD_TRACKER_SHEETS_PY",
            str(Path.home() / ".config/cloud-tracker-sheets/venv/bin/python"),
        )
        if Path(py).is_file():
            subprocess.check_call(
                [
                    py,
                    str(scripts / "push_estimator.py"),
                    "--org-json",
                    str(org_json),
                    "--upload-pdf-only",
                    "--report",
                    report,
                ]
            )
        else:
            print("NOTE: sheets venv python missing — skip Drive PDF upload", file=sys.stderr)


if __name__ == "__main__":
    main()
