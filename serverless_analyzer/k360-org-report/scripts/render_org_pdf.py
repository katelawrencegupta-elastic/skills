#!/usr/bin/env python3
"""Render Org report HTML+PDF into the artifacts folder and upload to Drive.

  python render_org_pdf.py --report exec --canvas …/…_canvas.json
  python render_org_pdf.py --report full --org-json <folder>/<key>_org_report.json
"""
from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
from pathlib import Path

from report_mode import add_report_arg, html_name, normalize_report, pdf_name, seal_file, write_private


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


def _find_chrome() -> str | None:
    candidates = [
        os.environ.get("CHROME_PATH"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def _label(row, key: str) -> str:
    """Display text for one infrastructure row: `{"provider": "AWS", "deployments": 3}` → `AWS`."""
    if isinstance(row, dict):
        return str(row.get(key) or "—")
    return str(row) if row not in (None, "") else "—"


def range_text(rng) -> str:
    """`2025-11-14 → 2026-11-13 (365 days)` from the fetch range."""
    if not isinstance(rng, dict):
        return str(rng) if rng else "—"
    start, end, days = rng.get("start"), rng.get("end"), rng.get("days")
    text = f"{start or '?'} → {end or '?'}"
    return text + (f" ({days} days)" if days else "")


def runout_text(bd: dict) -> str:
    """Runout against the contract end, from the two dates themselves."""
    runout, end = bd.get("runout_date"), bd.get("nearest_contract_end")
    days = bd.get("runway_days")
    if not runout:
        return "no runout date" + (f"; contract end {str(end)[:10]}" if end else "")
    text = f"runout {str(runout)[:10]}" + (f" ({days}d)" if days is not None else "")
    if end:
        r, e = str(runout)[:10], str(end)[:10]
        text += f" {'before' if r < e else ('after' if r > e else 'on')} contract end {e}"
    return text


def save_text(fs: dict) -> str:
    """One sentence on the serverless comparison, for the snapshot box."""
    if not fs.get("lines"):
        return "No serverless comparison in this report."
    if "priced_n" in fs and not fs.get("priced_n"):
        return "No deployment could be compared with Serverless."
    try:
        save = float(fs.get("save_vs_ech_yr"))
    except (TypeError, ValueError):
        return "No serverless comparison in this report."
    scope = f" on {fs['priced_n']} compared deployments" if fs.get("priced_n") else ""
    if save >= 0:
        return f"Ballpark serverless saving {_money(save)}/yr{scope}."
    return f"Serverless is a ballpark {_money(-save)}/yr more than ECH{scope}."


def not_compared_text(fs: dict, max_names: int = 12) -> str:
    """Who was left off both sides of the ECH-vs-Serverless comparison, and why."""
    rows = fs.get("not_priced") or []
    n = int(fs.get("not_priced_n") or len(rows))
    if not n:
        return ""
    why = "; ".join(f"{v.get('n')} {reason}" for reason, v in (fs.get("not_priced_by_reason") or {}).items())
    names = ", ".join(f"{r.get('name')} ({_num(r.get('ecu_mo'), 0)} ECU/mo)" for r in rows[:max_names])
    more = f", and {n - max_names} more" if n > max_names else ""
    return (
        f"Not compared: {n} deployments, {_num(fs.get('not_priced_ecu_mo'), 0)} ECU/mo"
        + (f" ({why})" if why else "")
        + ". They are on neither side of the totals above"
        + (f": {names}{more}." if names else ".")
    )


def storage_text(storage: dict) -> str:
    """Stale snapshots and deployments left out of stored GB."""
    parts = []
    stale_n = storage.get("stale_n", len(storage.get("stale") or []))
    if stale_n:
        ages = [x.get("age_days") for x in storage.get("stale") or [] if x.get("age_days") is not None]
        parts.append(
            f"{stale_n} of {storage.get('deployments_with_telemetry')} storage snapshots are more than "
            f"{storage.get('stale_after_days', 45)} days old"
            + (f" (oldest snapshot {storage.get('as_of_oldest')})" if storage.get("as_of_oldest") else (f" (oldest {max(ages)} days)" if ages else ""))
        )
    gone_n = storage.get("not_billing_n", len(storage.get("not_billing") or []))
    if gone_n:
        parts.append(
            f"{gone_n} deployments with no bill in the last {storage.get('billing_window_days', 90)} days are left out of "
            f"stored GB ({_num(storage.get('not_billing_gb'), 1)} GB)"
        )
    return ". ".join(parts) + ("." if parts else "")


def recommendations(bd: dict, fs: dict, storage: dict) -> list[str]:
    """Recommendation lines that follow this org's own numbers."""
    recs: list[str] = []
    reasons = "; ".join(bd.get("reasons") or [])
    if bd.get("priority"):
        recs.append("Address overage / runway risk" + (f": {reasons}" if reasons else ""))
    elif bd.get("watch"):
        recs.append("Watch runway" + (f": {reasons}" if reasons else ""))
    else:
        recs.append("Monitor burndown; no overage flag")
    lines = [l for l in (fs.get("lines") or []) if (l.get("n") if "n" in l else float(l.get("ech_mo") or 0) > 0)]
    lines.sort(key=lambda l: -float(l.get("save_mo") or 0))
    saving = [l for l in lines if float(l.get("save_mo") or 0) > 0]
    for i, l in enumerate(saving):
        proxy = " (priced on the Observability ladders; confirm with the Security estimator)" if "proxy" in str(l.get("project") or "").lower() else ""
        recs.append(
            f"Move {l.get('name')} to {l.get('project')}: about {_money(l.get('save_mo'))}/mo less than ECH "
            f"({_money(l.get('ech_mo'))} → {_money(l.get('serverless_mo'))})"
            + (", the largest saving line" if i == 0 and len(saving) > 1 else "")
            + proxy
        )
    for l in lines:
        if float(l.get("save_mo") or 0) <= 0:
            recs.append(
                f"Keep {l.get('name')} on ECH for now: the Serverless estimate is {_money(-float(l.get('save_mo') or 0))}/mo more "
                f"({_money(l.get('ech_mo'))} → {_money(l.get('serverless_mo'))})"
            )
    if fs.get("not_priced_n"):
        recs.append(
            f"Size the {fs['not_priced_n']} deployments left out of the comparison "
            f"({_num(fs.get('not_priced_ecu_mo'), 0)} ECU/mo) before quoting a total"
        )
    if storage.get("stale_n", len(storage.get("stale") or [])):
        recs.append("Confirm stored GB with the customer: storage snapshots are stale")
    return [f"P{i} — {text}" for i, text in enumerate(recs)]


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
    summary = infra.get("summary") or {}
    cloud = summary.get("provider") or (_label(providers[0], "provider") if providers else "—")
    region = summary.get("region") or (_label(regions[0], "region") if regions else "—")
    if len(providers) > 1:
        cloud = f"{cloud} +{len(providers) - 1}"
    if len(regions) > 1:
        region = f"{region} +{len(regions) - 1}"
    ver = dep.get("by_version") or []
    v8 = sum(x.get("count") or 0 for x in ver if str(x.get("version", "")).startswith("8."))
    v9 = sum(x.get("count") or 0 for x in ver if str(x.get("version", "")).startswith("9."))
    kind_label = "Full report" if report == "full" else "Exec report"
    top_n = 15 if report == "full" else 8
    ecu_mo = canvas.get("ecu_mo") if canvas.get("ecu_mo") is not None else canvas.get("ecu_monthly_avg")

    snap = f"{_esc(bd.get('level'))}: {_esc(runout_text(bd))}. {_esc(save_text(fs))}"

    def dep_mo(i: dict):
        return i.get("ecu_mo") if i.get("ecu_mo") is not None else (i.get("ecu_monthly_avg") or i.get("ecu"))

    dep_rows = "".join(
        f"<tr><td class='name'>{_esc(i.get('name'))}</td><td>{_esc(i.get('version'))}</td>"
        f"<td class='num'>{_num(dep_mo(i), 1)}</td>"
        f"<td class='num'>{_num(i.get('ml_ecu'), 0) if i.get('ml_ecu') else '—'}</td></tr>"
        for i in items[:top_n]
    )
    save_rows = "".join(
        f"<tr><td>{_esc(r.get('name'))}</td><td>{_esc(r.get('project'))}</td>"
        f"<td class='num'>{_esc(r.get('n')) if r.get('n') is not None else '—'}</td>"
        f"<td class='num'>{_money(r.get('ech_mo'))}</td>"
        f"<td class='num'>{_money(r.get('serverless_mo'))}</td>"
        f"<td class='num'>{_money(r.get('save_mo'))}</td></tr>"
        for r in lines
    )
    save_rows += (
        f"<tr><td class='name'>Total, compared deployments</td><td></td>"
        f"<td class='num'>{_esc(fs.get('priced_n')) if fs.get('priced_n') is not None else '—'}</td>"
        f"<td class='num'>{_money(fs.get('full_ech_mo'))}</td>"
        f"<td class='num'>{_money(fs.get('serverless_total_mo'))}</td>"
        f"<td class='num'>{_money(fs.get('save_vs_ech_mo'))}</td></tr>"
    )
    not_compared = not_compared_text(fs)
    storage_note = storage_text(storage)
    truncated = canvas.get("truncated") or []
    truncated_note = (
        "This organization has more deployments than the fetch reads (" + ", ".join(truncated) + "). Totals are low."
        if truncated
        else ""
    )
    months = canvas.get("lookback_months")
    ecu_basis = (
        f"ECU / mo is lookback ECU ÷ {_num(months, 2)} months ({_esc(canvas.get('lookback_days'))} days ÷ 30.416). "
        "Section 10 uses the same divisor. 1 ECU is $1 as billed."
        if months
        else ""
    )

    try:
        save_yr = float(fs.get("save_vs_ech_yr"))
        save_3yr = float(fs.get("save_vs_ech_3yr"))
    except (TypeError, ValueError):
        save_yr = save_3yr = None
    if save_yr is None:
        totals_note = ""
    elif save_yr >= 0:
        totals_note = f"Saving {_money(save_yr)}/yr · 3 yr {_money(save_3yr)}, compared deployments only."
    else:
        totals_note = f"Serverless is {_money(-save_yr)}/yr more · 3 yr {_money(-save_3yr)} more, compared deployments only."

    rec_html = "".join(f"<li>{_esc(r)}</li>" for r in recommendations(bd, fs, storage))

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
<div class="meta">{_esc(range_text(canvas.get('range')))} · refreshed {_esc(canvas.get('generated_at'))} · org {_esc(canvas.get('org_id'))}</div>
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
<div class="stat"><div class="v">{_num(ecu_mo,0)}</div><div class="l">ECU / mo</div></div>
<div class="stat"><div class="v">{_num(storage.get('data_stored_gb'),1)} GB</div><div class="l">Data stored</div></div>
<div class="stat"><div class="v">{_esc(bd.get('runway_days'))}d</div><div class="l">Runway → {_esc(bd.get('runout_date'))}</div></div>
</div>
<p class="meta">{_esc(ecu_basis)} {_esc(storage_note)} {_esc(truncated_note)}</p>
<h3>Top deployments</h3>
<table><thead><tr><th>Deployment</th><th>Ver</th><th class="num">ECU / mo</th><th class="num">ML ECU (lookback)</th></tr></thead>
<tbody>{dep_rows}</tbody></table>
</section>
<section class="card"><h2>9. Projected usage &amp; recommendations</h2>
<ul>{rec_html}</ul>
<p class="meta">Projection {_money(bd.get('annual_projection'))}/yr · ARR {_money(canvas.get('arr'))}</p>
</section>
<section class="card"><h2>10. Serverless move analysis (full_split)</h2>
<table><thead><tr><th>Project</th><th>Method</th><th class="num">Deployments</th><th class="num">ECH $/mo</th>
<th class="num">Serverless $/mo</th><th class="num">Save $/mo</th></tr></thead>
<tbody>{save_rows}</tbody></table>
<p class="meta">{_esc(totals_note)}
Retention { _esc(fs.get('retention_months_assumed')) } mo · Security = Complete proxy · confirm estimators/ETT.
Do not put all data on ES3 Search (all_es3 anti-pattern).</p>
<p class="meta">{_esc(not_compared)}</p>
</section>
<div class="footer">Generated by k360-org-report · render_org_pdf.py</div>
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
    write_private(html_path, render_html(canvas, offload, report=report))
    print(f"HTML → {html_path}", flush=True)

    wrote_pdf = False
    if not args.skip_pdf:
        chrome = _find_chrome()
        if chrome:
            chrome_pdf(chrome, html_path, pdf_path)
            seal_file(pdf_path)
            wrote_pdf = True
            print(f"PDF  → {pdf_path}", flush=True)
        else:
            print(f"Chrome not found. HTML is the report → {html_path}", flush=True)

    if wrote_pdf and not args.skip_upload and org_json and org_json.is_file():
        py = os.environ.get(
            "SERVERLESS_SHEETS_PY",
            str(Path.home() / ".config/serverless-analyzer/venv/bin/python"),
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
