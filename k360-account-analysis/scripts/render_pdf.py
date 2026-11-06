#!/usr/bin/env python3
"""PDF export of both canvases (account analysis §0–10 + consolidation savings §11).

Canvases are live React apps and can't be printed headlessly, so this renders the same
sections, in the same order, from `<key>_account.json` into print-ready HTML (inline SVG
charts, stdlib only) and prints them with headless Chrome:

  <key>_Account_analysis.{html,pdf}
  <key>_Serverless_consolidation_savings.{html,pdf}   (only when account.json has `consolidation`)
  <key>_Serverless_consolidation_from_k360.{html,pdf}   (backup consolidation, stored GB ÷ assumed retention; --k360-ingest)

  python render_pdf.py --account-json artifacts/<key>/<key>_account.json [--skip-pdf] [--only account|consolidation]

Chrome: $CHROME_PATH, else /Applications/Google Chrome.app or Chromium.app.
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import math
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from analyze import component_summary, display_solution

# Elastic brand: blue, pink, teal, yellow, then ink and danger for extra series.
PALETTE = ["#0B64DD", "#00BFB3", "#F04E98", "#153385", "#F5A700", "#BD271E", "#69707D", "#48A0FF", "#007A73", "#C78A00"]
TONE = {"high": "#BD271E", "medium": "#8A6100", "low": "#0B64DD", "danger": "#BD271E", "warning": "#8A6100", "info": "#0B64DD", "success": "#007A73", "neutral": "#69707D"}

ACCOUNT_SUFFIX = "Account_analysis"
CONSOLIDATION_SUFFIX = "Serverless_consolidation_savings"
DIAGNOSTICS_SUFFIX = "Serverless_consolidation_from_diagnostics"
K360_BACKUP_SUFFIX = "Serverless_consolidation_from_k360"


def _from_diagnostics(acc: dict) -> bool:
    c = acc.get("consolidation") or {}
    return c.get("source") == "diagnostics" or (acc.get("sizing") or {}).get("ingest_source") == "diagnostics"


# --------------------------------------------------------------------------- formatting
def esc(x) -> str:
    return html.escape("—" if x is None else str(x))


def n0(x) -> str:
    try:
        return f"{float(x):,.0f}"
    except (TypeError, ValueError):
        return "—"


def n1(x) -> str:
    try:
        return f"{float(x):,.1f}"
    except (TypeError, ValueError):
        return "—"


def usd(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _horizon_cell(h: dict) -> str:
    """3- or 6-month projection cell: ECU by date, remaining balance, contract $."""
    bits = [f"{n0(h.get('ecu'))} ECU by {h.get('as_of')}"]
    if h.get("balance") is not None:
        bits.append(f"balance {n0(h.get('balance'))}")
    if h.get("usd") is not None:
        bits.append(usd(h.get("usd")))
    return " · ".join(bits)


def pct(x) -> str:
    return "—" if x is None else f"{x}%"


def table(headers: list[str], rows: list[list], align: list[str] | None = None, tones: list[str | None] | None = None, *, boxed: bool = False) -> str:
    align = align or ["left"] + ["right"] * (len(headers) - 1)
    th = "".join(f"<th style='text-align:{a}'>{esc(h)}</th>" for h, a in zip(headers, align))
    body = []
    for i, r in enumerate(rows):
        tone = (tones or [None] * len(rows))[i] if tones else None
        if tone == "warning":
            style = " style='background:#FFF8E6'"
        elif tone == "danger":
            style = " style='background:#FDECEC;color:#BD271E'"
        elif tone:
            style = f" style='background:{TONE[tone]}18'"
        else:
            style = ""
        body.append(f"<tr{style}>" + "".join(f"<td style='text-align:{a}'>{c if isinstance(c, Raw) else esc(c)}</td>" for c, a in zip(r, align)) + "</tr>")
    cls = " class='boxed'" if boxed else ""
    return f"<table{cls}><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table>"


class Raw(str):
    """Pre-escaped HTML cell."""


def link(url: str | None, label: str) -> Raw:
    return Raw(f"<a href='{html.escape(url)}'>{esc(label)}</a>") if url else Raw(esc(label))


def stat(value, label, tone: str | None = None, tag: str | None = None, *, estimate: bool = False) -> str:
    color = f" style='color:{TONE[tone]}'" if tone else ""
    tag_html = f"<span class='tag{'' if estimate else ' fact'}'>{esc(tag)}</span>" if tag else ""
    return f"<div class='stat'><div class='v'{color}>{esc(value)}{tag_html}</div><div class='l'>{esc(label)}</div></div>"


def callout(title: str, body: str, tone: str = "info") -> str:
    return f"<div class='callout' style='border-left-color:{TONE.get(tone, TONE['info'])}'><b>{esc(title)}</b><div>{body}</div></div>"


def caption(text: str) -> str:
    return f"<div class='cap'>{esc(text)}</div>"


def estimator_label(text) -> str:
    """Plain name for a pricing source. Internal keys such as es3-estimator stay in the data."""
    low = str(text or "").strip().lower()
    if "es3" in low:
        return "Search Serverless, on-demand"
    if "security" in low and ("sheet" in low or "quotable" in low):
        return "Security Serverless"
    if low == "sheet" or ("observability" in low and ("sheet" in low or "quotable" in low)):
        return "Observability Serverless"
    if low.startswith("omitted"):
        return "Not priced"
    return str(text or "")


def bullets(items: list[str]) -> str:
    lis = "".join(f"<li>{esc(x)}</li>" for x in items if x)
    return f"<ul class='notes'>{lis}</ul>" if lis else ""


def section(title: str, body: str, *, new_page: bool = False) -> str:
    cls = "sec newpage" if new_page else "sec"
    return f"<section class='{cls}'><h2>{esc(title)}</h2>{body}</section>"


def grid(*cols: str, ratio: str | None = None, gap: str | None = None) -> str:
    tpl = ratio or " ".join(["1fr"] * len(cols))
    style = f"grid-template-columns:{tpl}"
    if gap:
        style += f";gap:{gap}"
    return f"<div class='grid' style='{style}'>" + "".join(f"<div>{c}</div>" for c in cols) + "</div>"


def panel(body: str) -> str:
    return f"<div class='panel'>{body}</div>"


# --------------------------------------------------------------------------- SVG charts
def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * e >= v:
            return m * e
    return 10 * e


def _short(v: float) -> str:
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e3:
        return f"{v / 1e3:.0f}k"
    return f"{v:.0f}"


def _legend(names: list[str], colors: list[str] | None = None) -> str:
    colors = colors or PALETTE
    return "<div class='legend'>" + "".join(f"<span><i style='background:{colors[i % len(colors)]}'></i>{esc(n)}</span>" for i, n in enumerate(names)) + "</div>"


def bar_chart(categories: list[str], series: list[tuple[str, list[float]]], *, stacked: bool = False, width: int = 640, height: int = 230,
              ref: tuple[float, str] | None = None, show_values: bool = False, prefix: str = "", colors: list[str] | None = None, exact_values: bool = False) -> str:
    colors = colors or PALETTE
    pl, pr, pt, pb = 52, 10, 12, 42
    w, h = width - pl - pr, height - pt - pb
    n = max(len(categories), 1)
    totals = [sum(s[1][i] or 0 for s in series) for i in range(n)] if stacked else [max((s[1][i] or 0) for s in series) for i in range(n)]
    vmax = _nice_max(max(totals + [ref[0] if ref else 0]))

    def lab(v: float) -> str:
        return f"{prefix}{n0(v) if exact_values else _short(v)}"

    out = [f"<svg viewBox='0 0 {width} {height}' width='100%' xmlns='http://www.w3.org/2000/svg' font-size='12' font-family='inherit'>"]
    for k in range(5):
        y = pt + h - h * k / 4
        out.append(f"<line x1='{pl}' x2='{pl + w}' y1='{y:.1f}' y2='{y:.1f}' stroke='#D3DAE6'/><text x='{pl - 6}' y='{y + 3:.1f}' text-anchor='end' fill='#69707D'>{lab(vmax * k / 4)}</text>")
    slot = w / n
    groups = 1 if stacked else len(series)
    bw = slot * 0.7 / groups
    for i, cat in enumerate(categories):
        x0 = pl + slot * i + slot * 0.15
        base = 0.0
        for j, (_, vals) in enumerate(series):
            v = float(vals[i] or 0)
            bh = h * v / vmax
            if stacked:
                y = pt + h - h * (base + v) / vmax
                out.append(f"<rect x='{x0:.1f}' y='{y:.1f}' width='{slot * 0.7:.1f}' height='{bh:.1f}' fill='{colors[j % len(colors)]}'/>")
                base += v
            else:
                x = x0 + bw * j
                y = pt + h - bh
                out.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bw:.1f}' height='{bh:.1f}' fill='{colors[j % len(colors)]}'/>")
                if show_values and v:
                    out.append(f"<text x='{x + bw / 2:.1f}' y='{y - 3:.1f}' text-anchor='middle' fill='#343741'>{lab(v)}</text>")
        if stacked and show_values and base:
            out.append(f"<text x='{x0 + slot * 0.35:.1f}' y='{pt + h - h * base / vmax - 3:.1f}' text-anchor='middle' fill='#343741'>{lab(base)}</text>")
        lx = x0 + slot * 0.35
        rot = " transform='rotate(-35 {:.1f} {:.1f})'".format(lx, pt + h + 14) if n > 8 and max((len(c) for c in categories), default=0) > 6 else ""
        out.append(f"<text x='{lx:.1f}' y='{pt + h + 14}' text-anchor='{'end' if n > 8 else 'middle'}' fill='#343741'{rot}>{esc(cat)}</text>")
    if ref:
        y = pt + h - h * ref[0] / vmax
        out.append(f"<line x1='{pl}' x2='{pl + w}' y1='{y:.1f}' y2='{y:.1f}' stroke='#69707D' stroke-dasharray='4 3'/><text x='{pl + w}' y='{y - 4:.1f}' text-anchor='end' fill='#343741'>{esc(ref[1])}</text>")
    out.append("</svg>")
    legend = _legend([s[0] for s in series], colors) if len(series) > 1 else ""
    return "".join(out) + legend


def hbar_chart(items: list[tuple[str, float]], *, width: int = 520, label_w: int = 150, show_pct_of: float | None = None) -> str:
    bh, gap = 16, 6
    height = len(items) * (bh + gap) + 8
    vmax = _nice_max(max([v for _, v in items] + [1]))
    w = width - label_w - 70
    out = [f"<svg viewBox='0 0 {width} {height}' width='100%' xmlns='http://www.w3.org/2000/svg' font-size='12' font-family='inherit'>"]
    for i, (lab, v) in enumerate(items):
        y = 4 + i * (bh + gap)
        bw = w * (v or 0) / vmax
        out.append(f"<text x='{label_w - 6}' y='{y + 12}' text-anchor='end' fill='#343741'>{esc(lab)}</text>")
        out.append(f"<rect x='{label_w}' y='{y}' width='{bw:.1f}' height='{bh}' fill='{PALETTE[0]}'/>")
        tail = f" ({100 * v / show_pct_of:.1f}%)" if show_pct_of else ""
        out.append(f"<text x='{label_w + bw + 4:.1f}' y='{y + 12}' fill='#343741'>{n0(v)}{tail}</text>")
    out.append("</svg>")
    return "".join(out)


def line_chart(categories: list[str], values: list[float] | None = None, *, series: list[tuple[str, list]] | None = None, width: int = 640, height: int = 220, show_values: bool = True, colors: list[str] | None = None) -> str:
    series = series or [("", values or [])]
    colors = colors or PALETTE
    nums = [float(v) for _, vals in series for v in vals if v is not None]
    pl, pr, pt, pb = 52, 14, 16, 34
    w, h = width - pl - pr, height - pt - pb
    vmax = _nice_max(max(nums + [1]))
    n = len(categories)
    xs = [pl + (w * i / max(n - 1, 1)) for i in range(n)]
    out = [f"<svg viewBox='0 0 {width} {height}' width='100%' xmlns='http://www.w3.org/2000/svg' font-size='12' font-family='inherit'>"]
    for k in range(5):
        y = pt + h - h * k / 4
        out.append(f"<line x1='{pl}' x2='{pl + w}' y1='{y:.1f}' y2='{y:.1f}' stroke='#D3DAE6'/><text x='{pl - 6}' y='{y + 3:.1f}' text-anchor='end' fill='#69707D'>{_short(vmax * k / 4)}</text>")
    for si, (_, vals) in enumerate(series):
        color = colors[si % len(colors)]
        run: list[tuple[float, float, float]] = []

        def flush() -> None:
            if len(run) >= 2:
                out.append("<polyline fill='none' stroke='{}' stroke-width='2' points='{}'/>".format(color, " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in run)))
            for x, y, v in run:
                out.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2.2' fill='{color}'/>")
                if show_values and n <= 16:
                    out.append(f"<text x='{x:.1f}' y='{y - 6:.1f}' text-anchor='middle' fill='#343741'>{_short(v)}</text>")
            run.clear()

        padded = list(vals)[:n]
        padded += [None] * (n - len(padded))
        for i, v in enumerate(padded):
            if v is None:
                flush()
            else:
                run.append((xs[i], pt + h - h * float(v) / vmax, float(v)))
        flush()
    step = 2 if n > 14 else 1
    for i, c in enumerate(categories):
        if i % step and i != n - 1:
            continue
        out.append(f"<text x='{xs[i]:.1f}' y='{pt + h + 14}' text-anchor='middle' fill='#343741'>{esc(c)}</text>")
    out.append("</svg>")
    legend = _legend([name for name, _ in series if name], colors) if any(name for name, _ in series) else ""
    return "".join(out) + legend


def donut(items: list[tuple[str, float]], *, size: int = 170) -> str:
    items = [(l, float(v or 0)) for l, v in items if (v or 0) > 0]
    total = sum(v for _, v in items) or 1
    r, ri, c = size / 2 - 4, size / 2 - 30, size / 2
    out = [f"<svg viewBox='0 0 {size} {size}' width='{size}' height='{size}' xmlns='http://www.w3.org/2000/svg'>"]
    a0 = -math.pi / 2
    for i, (_, v) in enumerate(items):
        frac = v / total
        if frac >= 0.9999:
            out.append(f"<circle cx='{c}' cy='{c}' r='{(r + ri) / 2}' fill='none' stroke='{PALETTE[i % len(PALETTE)]}' stroke-width='{r - ri}'/>")
            break
        a1 = a0 + 2 * math.pi * frac
        large = 1 if frac > 0.5 else 0
        p = [(c + r * math.cos(a0), c + r * math.sin(a0)), (c + r * math.cos(a1), c + r * math.sin(a1)), (c + ri * math.cos(a1), c + ri * math.sin(a1)), (c + ri * math.cos(a0), c + ri * math.sin(a0))]
        out.append(f"<path d='M{p[0][0]:.2f},{p[0][1]:.2f} A{r},{r} 0 {large} 1 {p[1][0]:.2f},{p[1][1]:.2f} L{p[2][0]:.2f},{p[2][1]:.2f} A{ri},{ri} 0 {large} 0 {p[3][0]:.2f},{p[3][1]:.2f} Z' fill='{PALETTE[i % len(PALETTE)]}'/>")
        a0 = a1
    out.append("</svg>")
    legend = "<div class='legend col'>" + "".join(f"<span><i style='background:{PALETTE[i % len(PALETTE)]}'></i>{esc(l)} — {100 * v / total:.1f}%</span>" for i, (l, v) in enumerate(items)) + "</div>"
    return f"<div class='donut'>{''.join(out)}{legend}</div>"


def architecture_svg(groups: list[dict], projects: list[dict], *, width: int = 940) -> str:
    """ECH deployments grouped by solution and region, each wired to its own serverless project."""
    if not groups or not projects:
        return ""
    sol_color = {"search": PALETTE[0], "observability": PALETTE[1], "security": PALETTE[3]}
    by_key = {(p.get("solution"), p.get("region")): p for p in projects}
    paired = all((g["solution"], g["region"]) in by_key for g in groups)
    lx, lw, rw = 4, 360, 330
    rx = width - rw - 4
    head, bh, gap = 26, 48, 10
    n_regions = len({g["region"] for g in groups})
    height = head + len(groups) * (bh + gap) + 4
    out = [f"<svg viewBox='0 0 {width} {height}' width='100%' xmlns='http://www.w3.org/2000/svg' font-size='12' font-family='inherit'>"]
    n_deps = sum(g.get("n") or 0 for g in groups)
    right_title = "Serverless: one project per region" if n_regions > 1 and paired else "Serverless: one project per solution"
    out.append(f"<text x='{lx}' y='14' font-weight='600' fill='#343741'>ECH today: {n_deps} deployments</text>")
    out.append(f"<text x='{rx}' y='14' font-weight='600' fill='#343741'>{right_title}</text>")
    for i, g in enumerate(groups):
        y = head + i * (bh + gap)
        col = sol_color.get(g["solution"], PALETTE[6])
        out.append(f"<rect x='{lx}' y='{y}' width='{lw}' height='{bh}' rx='6' fill='#F5F7FA' stroke='#D3DAE6'/>")
        out.append(f"<rect x='{lx}' y='{y}' width='5' height='{bh}' rx='2' fill='{col}'/>")
        out.append(f"<text x='{lx + 14}' y='{y + 19}' font-weight='600' fill='#343741'>{esc(g['solution'].title())} · {esc(g['region'])}</text>")
        dep_word = "deployment" if g["n"] == 1 else "deployments"
        out.append(f"<text x='{lx + 14}' y='{y + 37}' fill='#69707D'>{g['n']} {dep_word} · {n0(g.get('ech_ecu_mo'))} ECU/mo · {n0(g.get('stored_gb'))} GB</text>")
        p = by_key.get((g["solution"], g["region"])) if paired else None
        if p:
            mid_y = y + bh / 2
            out.append(f"<line x1='{lx + lw}' y1='{mid_y:.1f}' x2='{rx}' y2='{mid_y:.1f}' stroke='#98A2B3' stroke-width='1.4'/>")
            out.append(f"<rect x='{rx}' y='{y}' width='{rw}' height='{bh}' rx='6' fill='#ffffff' stroke='{col}' stroke-width='1.6'/>")
            out.append(f"<text x='{rx + 14}' y='{y + 19}' font-weight='700' fill='#343741'>{esc(p['solution'].title())} · {esc(p['region'])}</text>")
            price = f"{usd(p['serverless_mo'])}/mo estimate" if p.get("serverless_mo") is not None else "not priced"
            out.append(f"<text x='{rx + 14}' y='{y + 37}' fill='#343741'>{n0(p.get('stored_gb'))} GB · {esc(price)}</text>")
    out.append("</svg>")
    return "".join(out)


def _gb(x) -> str:
    try:
        g = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{g:,.2f} GB" if g < 10 else f"{g:,.1f} GB"


def _kb(x) -> str:
    try:
        return f"{float(x):,.2f} KB"
    except (TypeError, ValueError):
        return "—"


def architecture_table(acc: dict) -> str:
    """One row per ECH deployment: ECU share of the lookback, plus stored size and documents."""
    cl = acc.get("cloud") or {}
    deps = [d for d in (cl.get("by_deployment") or []) if (d.get("platform") or "ech") != "serverless" and d.get("name")]
    if not deps:
        return ""
    lookback = float((acc.get("ecu") or {}).get("total_lookback") or 0)
    total = lookback if lookback > 0 else sum(float(d.get("ecu") or 0) for d in deps)
    stor = {r.get("name"): r for r in ((acc.get("storage") or {}).get("by_deployment") or [])}
    classified = {d.get("name"): d for d in ((acc.get("solutions") or {}).get("by_deployment") or [])}
    providers = {d.get("provider") for d in deps if d.get("provider")}
    deps.sort(key=lambda d: -float(d.get("ecu") or 0))
    headers = ["Deployment", "Version", "Region", "Solution", "AZ", "ECU", "Utilization", "Stored", "Documents", "Avg doc"]
    rows, tones = [], []
    for d in deps:
        name = d.get("name")
        info = classified.get(name) or {}
        solution = display_solution(info, d)
        size = stor.get(name) or {}
        zones = d.get("zones")
        ecu = float(d.get("ecu") or 0)
        share = round(100 * ecu / total, 1) if total else None
        region = d.get("region") or "—"
        if len(providers) > 1 and d.get("provider"):
            region = f"{d.get('provider')} {region}"
        row = [name, d.get("version") or "—", region]
        row += [
            solution.title(),
            f"{int(zones)} AZ" if isinstance(zones, (int, float)) else "—",
            n0(ecu),
            pct(share),
            _gb(size.get("data_stored_gb")) if name in stor else "—",
            n0(size.get("docs_count")) if name in stor else "—",
            _kb(size.get("avg_doc_kb")) if name in stor else "—",
        ]
        rows.append(row)
        tones.append("warning" if zones == 1 else None)
    n_left = 4
    align = ["left"] * n_left + ["right"] * (len(headers) - n_left)
    return table(headers, rows, align, tones, boxed=True)


def deployment_topology(acc: dict) -> str:
    """Region, then each deployment and the nodes on its Cloud SKUs."""
    cl = acc.get("cloud") or {}
    deps = [d for d in (cl.get("by_deployment") or []) if (d.get("platform") or "ech") != "serverless" and d.get("name")]
    if not deps:
        return ""
    summary = cl.get("summary") or {}
    regions = [r for r in (cl.get("regions") or []) if r.get("region")]
    if not regions:
        seen = []
        for d in deps:
            if d.get("region") and d.get("region") not in seen:
                seen.append(d["region"])
        regions = [{"region": r, "ecu": None, "pct": None} for r in seen]
    out = ["<h3>Nodes</h3><div class='topo'>"]
    shown_cap = 12
    hidden = 0
    for region in regions:
        rows = [d for d in deps if d.get("region") == region.get("region")]
        rows.sort(key=lambda d: -float(d.get("ecu") or 0))
        hidden += max(len(rows) - shown_cap, 0)
        provider = next((d.get("provider") for d in rows if d.get("provider")), summary.get("provider"))
        title = " ".join(p for p in (provider, region.get("region")) if p)
        share = f" · {region.get('pct')}%" if region.get("pct") is not None else ""
        body = []
        for dep in rows[:shown_cap]:
            nodes = component_summary(dep) or (f"{int(dep['zones'])} AZ" if isinstance(dep.get("zones"), (int, float)) else "—")
            body.append([dep.get("name"), nodes])
        out.append("<div class='region'>")
        out.append(f"<div class='rh'>{esc(title)} · {len(rows)} deployments · {n0(region.get('ecu'))} ECU{esc(share)}</div>")
        out.append(table(["Deployment", "Nodes"], body, ["left", "left"], boxed=True))
        out.append("</div>")
    out.append("</div>")
    note = "Node counts are the zone count on each component's Cloud SKU. ECH places one node in each zone. Extra autoscaled nodes are not a separate field."
    if hidden:
        note = f"Top {shown_cap} deployments in each region. " + note
    return "".join(out) + caption(note)


def regional_architecture_svg(acc: dict, *, width: int = 1040) -> str:
    """Provider, then each region, then deployments grouped by availability zone."""
    cl = acc.get("cloud") or {}
    deps = [d for d in (cl.get("by_deployment") or []) if (d.get("platform") or "ech") != "serverless" and d.get("name")]
    regions = [r for r in (cl.get("regions") or []) if r.get("region")]
    if not deps or not regions:
        return ""
    sol_by = {d.get("name"): (d.get("solution") or "") for d in ((acc.get("solutions") or {}).get("by_deployment") or [])}
    summary = cl.get("summary") or {}
    provider = " ".join(p for p in (summary.get("provider"), summary.get("plan")) if p) or "ECH"
    sol_color = {"search": PALETTE[0], "observability": PALETTE[1], "security": PALETTE[3]}
    sol_label = {"search": "Search", "observability": "Observability", "security": "Security"}

    def az_sort(z) -> int:
        return -(int(z) if isinstance(z, (int, float)) else -1)

    def grouped(rows: list) -> list[tuple]:
        buckets: dict = {}
        for d in rows:
            buckets.setdefault(d.get("zones"), []).append(d)
        for rows_ in buckets.values():
            rows_.sort(key=lambda d: -float(d.get("ecu") or 0))
        return sorted(buckets.items(), key=lambda kv: az_sort(kv[0]))

    cols = []
    for region in regions:
        rows = [d for d in deps if d.get("region") == region.get("region")]
        cols.append((region, grouped(rows), rows))
    multi = len(cols) > 1
    cap = 6 if multi else 8
    card_h, card_gap = (22, 4) if multi else (28, 6)
    head, pad = 24, 8

    def block_h(n: int) -> int:
        return head + pad + n * card_h + max(n - 1, 0) * card_gap + pad

    def box(x, y, w, h, fill, stroke) -> str:
        return f"<rect x='{x:.1f}' y='{y:.1f}' width='{w:.1f}' height='{h:.1f}' rx='6' fill='{fill}' stroke='{stroke}'/>"

    def text(x, y, value, *, anchor="start", weight="400", size=12, fill="#343741") -> str:
        return (
            f"<text x='{x:.1f}' y='{y:.1f}' text-anchor='{anchor}' font-size='{size}' "
            f"font-weight='{weight}' fill='{fill}'>{esc(value)}</text>"
        )

    def cards(x, y, w, rows, extra) -> tuple[str, int]:
        parts = []
        yy = y
        for dep in rows:
            solution = sol_by.get(dep.get("name")) or "search"
            parts.append(box(x, yy, w, card_h, "#ffffff", "#D3DAE6"))
            parts.append(f"<rect x='{x:.1f}' y='{yy:.1f}' width='4' height='{card_h}' fill='{sol_color.get(solution, PALETTE[6])}'/>")
            parts.append(text(x + 12, yy + card_h * 0.68, str(dep.get("name") or ""), size=12 if not multi else 11))
            ver = dep.get("version") or ""
            if ver:
                parts.append(text(x + w - 8, yy + card_h * 0.68, str(ver), anchor="end", size=11, fill="#69707D"))
            yy += card_h + card_gap
        if extra:
            parts.append(text(x + 12, yy + 14, f"+{extra} more", size=11, fill="#69707D"))
            yy += card_h
        return "".join(parts), yy

    present = []
    for dep in deps:
        sol = sol_by.get(dep.get("name")) or "search"
        if sol not in present:
            present.append(sol)
    one_az = any(d.get("zones") == 1 for d in deps)
    legend = "<div class='legend'>" + "".join(
        f"<span><i style='background:{sol_color.get(s, PALETTE[6])}'></i>{esc(sol_label.get(s, s.title()))}</span>" for s in present
    )
    if one_az:
        legend += f"<span><i style='background:{TONE['warning']}'></i>1 availability zone</span>"
    legend += "</div>"

    parts = []
    if not multi:
        region, groups, rows = cols[0]
        shown = [(z, g[:cap], max(len(g) - cap, 0)) for z, g in groups]
        n = max(len(shown), 1)
        gap = 16
        col_w = (width - gap * (n - 1)) / n
        max_h = max((block_h(len(g) + (1 if extra else 0)) for _, g, extra in shown), default=40)
        cloud_w, cloud_h, region_w, region_h, vgap = 300, 36, 460, 48, 20
        y_cloud, y_region = 2, 2 + cloud_h + vgap
        y_groups = y_region + region_h + vgap
        height = y_groups + max_h + 6
        parts.append(f"<svg viewBox='0 0 {width} {height}' width='100%' xmlns='http://www.w3.org/2000/svg' font-family='inherit'>")
        cx = (width - cloud_w) / 2
        parts.append(box(cx, y_cloud, cloud_w, cloud_h, "#F5F7FA", "#D3DAE6"))
        parts.append(text(width / 2, y_cloud + 23, f"{provider} · {len(deps)} deployments", anchor="middle", weight="700", size=13))
        parts.append(f"<line x1='{width / 2}' y1='{y_cloud + cloud_h}' x2='{width / 2}' y2='{y_region}' stroke='#D3DAE6' stroke-width='1.5'/>")
        rx = (width - region_w) / 2
        parts.append(box(rx, y_region, region_w, region_h, "#F5F7FA", PALETTE[0]))
        share = f" · {region.get('pct')}%" if region.get("pct") is not None else ""
        parts.append(text(width / 2, y_region + 20, str(region.get("region")), anchor="middle", weight="700", size=14))
        parts.append(text(width / 2, y_region + 38, f"{len(rows)} deployments · {n0(region.get('ecu'))} ECU{share}", anchor="middle", size=12, fill="#69707D"))
        bus = y_region + region_h + vgap / 2
        parts.append(f"<line x1='{width / 2}' y1='{y_region + region_h}' x2='{width / 2}' y2='{bus}' stroke='#D3DAE6' stroke-width='1.5'/>")
        xs = [i * (col_w + gap) for i in range(n)]
        if n > 1:
            parts.append(f"<line x1='{xs[0] + col_w / 2:.1f}' y1='{bus}' x2='{xs[-1] + col_w / 2:.1f}' y2='{bus}' stroke='#D3DAE6' stroke-width='1.5'/>")
        for i, (z, group_rows, extra) in enumerate(shown):
            x = xs[i]
            parts.append(f"<line x1='{x + col_w / 2:.1f}' y1='{bus}' x2='{x + col_w / 2:.1f}' y2='{y_groups}' stroke='#D3DAE6' stroke-width='1.5'/>")
            warn = z == 1
            parts.append(box(x, y_groups, col_w, max_h, "#FFF8E6" if warn else "#ffffff", TONE["warning"] if warn else "#D3DAE6"))
            label = f"{int(z)} availability zone" + ("s" if z != 1 else "") if isinstance(z, (int, float)) else "Availability zones"
            parts.append(text(x + 10, y_groups + 17, label, weight="700", size=12, fill=TONE["warning"] if warn else "#343741"))
            drawn, _ = cards(x + 8, y_groups + head, col_w - 16, group_rows, extra)
            parts.append(drawn)
        parts.append("</svg>")
    else:
        gap = 16
        col_w = (width - gap * (len(cols) - 1)) / len(cols)
        cloud_h, vgap = 36, 20
        col_heights = []
        packed = []
        for region, groups, rows in cols:
            shown = [(z, g[:cap], max(len(g) - cap, 0)) for z, g in groups]
            inner = 40 + sum(block_h(len(g) + (1 if extra else 0)) + 8 for _, g, extra in shown)
            col_heights.append(inner)
            packed.append((region, shown, rows, inner))
        uniform = max(col_heights) if col_heights else 40
        height = 2 + cloud_h + vgap + uniform + 6
        y_groups = 2 + cloud_h + vgap
        parts.append(f"<svg viewBox='0 0 {width} {height}' width='100%' xmlns='http://www.w3.org/2000/svg' font-family='inherit'>")
        parts.append(box(0, 2, width, cloud_h, "#F5F7FA", "#D3DAE6"))
        parts.append(text(width / 2, 25, f"{provider} · {len(deps)} deployments · {len(cols)} regions", anchor="middle", weight="700", size=13))
        for i, (region, shown, rows, _) in enumerate(packed):
            x = i * (col_w + gap)
            parts.append(f"<line x1='{x + col_w / 2:.1f}' y1='{2 + cloud_h}' x2='{x + col_w / 2:.1f}' y2='{y_groups}' stroke='#D3DAE6' stroke-width='1.5'/>")
            parts.append(box(x, y_groups, col_w, uniform, "#ffffff", PALETTE[0]))
            share = f" · {region.get('pct')}%" if region.get("pct") is not None else ""
            parts.append(text(x + 10, y_groups + 18, str(region.get("region")), weight="700", size=13))
            parts.append(text(x + 10, y_groups + 34, f"{len(rows)} deployments · {n0(region.get('ecu'))} ECU{share}", size=11, fill="#69707D"))
            yy = y_groups + 44
            for z, group_rows, extra in shown:
                warn = z == 1
                gh = block_h(len(group_rows) + (1 if extra else 0))
                parts.append(box(x + 8, yy, col_w - 16, gh, "#FFF8E6" if warn else "#F5F7FA", TONE["warning"] if warn else "#D3DAE6"))
                label = f"{int(z)} AZ" if isinstance(z, (int, float)) else "AZ"
                parts.append(text(x + 16, yy + 16, label, weight="700", size=11, fill=TONE["warning"] if warn else "#343741"))
                drawn, _ = cards(x + 16, yy + head, col_w - 32, group_rows, extra)
                parts.append(drawn)
                yy += gh + 8
        parts.append("</svg>")
    return "<div style='break-inside:avoid'>" + "".join(parts) + legend + "</div>"


# --------------------------------------------------------------------------- page shell
CSS = """
@page { size: Letter landscape; margin: 11mm 12mm 11mm 12mm; }
* { box-sizing: border-box; }
body { font-family: Inter, 'Helvetica Neue', Arial, sans-serif; font-size: 12.5px; color: #343741; margin: 0; background: #fff; }
.masthead { background: #0B64DD; color: #fff; margin: 0 0 14px; padding: 12px 16px 10px; border-bottom: 4px solid #F04E98; border-radius: 6px; }
.masthead h1 { color: #fff; font-size: 22px; font-weight: 700; letter-spacing: -0.02em; margin: 0; }
.brand { font-size: 13px; font-weight: 800; letter-spacing: -0.03em; margin-bottom: 4px; }
.brand i { display: inline-block; width: 8px; height: 8px; margin-left: 6px; background: #F04E98; border-radius: 2px; vertical-align: 1px; }
.masthead .sub { color: rgba(255,255,255,.88); margin: 4px 0 0; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 15px; font-weight: 700; color: #0B64DD; margin: 0 0 10px; padding-bottom: 4px; border-bottom: 2px solid #0B64DD; break-after: avoid; }
h3 { font-size: 13px; font-weight: 700; color: #153385; margin: 0 0 6px; }
.sub { color: #69707D; margin-bottom: 2px; }
.cap { color: #69707D; font-size: 11px; margin: 4px 0 0; }
ul.notes { margin: 4px 0 8px 18px; padding: 0; color: #343741; font-size: 12px; }
ul.notes li { margin: 2px 0; }
.sec { margin: 0 0 2px; } .newpage { break-before: page; }
.stats { display: grid; gap: 12px; margin: 0 0 12px; align-items: stretch; }
.stat { min-width: 0; display: flex; flex-direction: column; justify-content: flex-start; background: #fff; border: 1px solid #D3DAE6; border-top: 3px solid #0B64DD; border-radius: 6px; padding: 8px 10px 10px; break-inside: avoid; }
.stat .l { order: -1; color: #69707D; font-size: 10px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; line-height: 1.3; min-height: 2.6em; }
.stat .v { font-size: 16px; font-weight: 700; line-height: 1.25; color: #153385; margin-top: 2px; }
.stat .tag { display: inline-block; font-size: 9px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; border-radius: 3px; padding: 1px 5px; margin-left: 6px; vertical-align: 2px; color: #8A6100; background: #FFF8E6; }
.stat .tag.fact { color: #153385; background: #E6F0FF; }
.grid { display: grid; gap: 12px; align-items: stretch; margin: 0 0 12px; }
.grid > div { min-width: 0; display: flex; flex-direction: column; }
.grid > div > .panel, .grid > div > .card { flex: 1 1 auto; margin: 0; }
.grid > div:not(:has(.panel)):not(:has(.card)):not(:has(.grid)) { border: 1px solid #D3DAE6; border-radius: 6px; background: #fff; padding: 12px 14px; }
.panel { border: 1px solid #D3DAE6; border-radius: 6px; background: #fff; padding: 12px 14px; break-inside: avoid; margin: 0 0 12px; }
.panel h3 { margin-top: 0; }
table { width: 100%; margin: 2px 0 0; border: 1px solid #D3DAE6; border-radius: 4px; border-collapse: separate; border-spacing: 0; overflow: hidden; background: #fff; }
th, td { border-bottom: 1px solid #EEF2F7; padding: 3px 8px; vertical-align: top; line-height: 1.25; }
th { background: #E6F0FF; color: #153385; font-weight: 700; font-size: 10.5px; letter-spacing: .03em; text-transform: uppercase; }
td { font-size: 12px; }
tbody tr:last-child td { border-bottom: none; }
tr { break-inside: avoid; }
.callout { border: 1px solid #D3DAE6; border-left: 4px solid #0B64DD; border-radius: 4px; background: #F5F7FA; padding: 8px 10px; margin: 0 0 12px; break-inside: avoid; }
.callout div { margin-top: 2px; color: #343741; }
.rec { border: 1px solid #D3DAE6; border-left: 4px solid #0B64DD; border-radius: 6px; background: #fff; padding: 10px 14px; margin: 0 0 10px; break-inside: avoid; }
.rec.panel { background: #fff; margin: 0 0 10px; }
.rec .t { font-weight: 700; color: #153385; } .rec .sev { font-size: 10px; font-weight: 700; letter-spacing: .04em; margin-left: 6px; }
.rec .e { color: #343741; margin-top: 4px; } .rec .docs { margin-top: 6px; }
.card { border: 1px solid #D3DAE6; border-radius: 6px; background: #fff; padding: 10px 12px; break-inside: avoid; margin: 0; }
.card .hd { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; font-weight: 700; color: #153385; margin-bottom: 6px; }
.card .hd span { color: #69707D; font-weight: 600; font-size: 11px; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 11px; color: #343741; margin-top: 6px; }
.legend.col { flex-direction: column; gap: 3px; }
.legend i { display: inline-block; width: 9px; height: 9px; margin-right: 4px; vertical-align: -1px; border-radius: 2px; }
.donut { display: flex; gap: 12px; align-items: center; }
svg { display: block; max-width: 100%; height: auto; }
a { color: #0B64DD; text-decoration: none; }
.topo { display: grid; gap: 12px; margin-top: 8px; }
.topo .region { border: 1px solid #D3DAE6; border-radius: 6px; background: #fff; padding: 8px 12px 4px; }
.topo .rh { font-weight: 700; color: #153385; margin-bottom: 2px; }
.topo td:first-child { white-space: nowrap; width: 18%; }
.foot { color: #69707D; font-size: 11px; border-top: 1px solid #D3DAE6; margin-top: 14px; padding-top: 6px; }
"""


def page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(title)}</title><style>{CSS}</style></head><body>{body}</body></html>"


def consolidation_report_exists(acc: dict) -> bool:
    """Cost (§9) and sizing (§10) live on the consolidation PDF when that report ran."""
    c = acc.get("consolidation") or {}
    return bool((acc.get("sizing") or {}).get("triggered") and c and not c.get("error"))


def cost_sizing_sections(acc: dict, *, sizing: bool = True) -> list[str]:
    m = acc.get("meta") or {}
    an = acc.get("analysis") or {}
    sz = acc.get("sizing") or {}
    est = sz.get("estimators") or {}
    out = [section("9. Cost-savings analysis", recs(an.get("cost") or []), new_page=True)]
    if not sz.get("triggered"):
        body = f"<p>{'Skipped (' + esc(sz.get('skipped_reason')) + ')' if sz.get('skipped_reason') else 'Not triggered'} — ECH share {esc(sz.get('ech_share_pct'))}% (threshold {esc(sz.get('threshold_pct'))}%).</p>"
    else:
        cons = sz.get("consolidated") or {}
        body = f"<p>Triggered — ECH share {esc(sz.get('ech_share_pct'))}%. Local folder <code>{esc(sz.get('folder'))}</code>" + (f" · {link((sz.get('drive_folder') or {}).get('url'), 'Drive folder')}" if sz.get("drive_folder") else "") + "</p>"
        body += table(["Solution", "Deployments", "ECU/mo", "Stored GB", "Retention (mo)", "Daily raw GB", "GB estimated?"],
                      [[k.title(), v.get("deployments"), n0(v.get("ecu_monthly_avg")), n0(v.get("stored_gb")), v.get("retention_months"), n1(v.get("daily_raw_gb")), "yes" if v.get("gb_estimated_from_ecu_share") else "no"] for k, v in cons.items()])
        cards = []
        for k in ("observability", "security", "search"):
            x = est.get(k) or {}
            rb = x.get("readback") or {}
            status = x.get("status") or ("pending")
            inner = ""
            if rb:
                def _shown(v):
                    return usd(v) if isinstance(v, (int, float)) else v
                inner += "<div class='stats' style='grid-template-columns:repeat(3,1fr)'>" + "".join(stat(_shown(rb.get(kk)), lbl) for kk, lbl in (("monthly_steady_state", "monthly"), ("annual_year1", "year 1"), ("annual_steady_state", "steady state"), ("annual_year2", "year 2"), ("annual_year3", "year 3")) if rb.get(kk)) + "</div>"
            if x.get("region_match"):
                inner += caption(f"Region: {x['region_match']}")
            if x.get("note"):
                inner += caption(x["note"])
            inner += str(link(x.get("url"), "Open estimator")) if x.get("url") else caption(str(status))
            title = {"search": "Search Serverless", "observability": "Observability Serverless", "security": "Security Serverless"}.get(k, k.title())
            badge = "on-demand" if k == "search" and rb else ("Complete" if rb else "not priced")
            cards.append(f"<div class='card'><div class='hd'>{esc(title)}<span>{esc(badge)}</span></div>{inner}</div>")
        body += grid(*cards)
    body += f"<div class='foot'>Source: K360 cloud-tracker (billing, telemetry, features, organization_pivot) · generated {esc(m.get('generated_at'))} · only estimator read-backs are quotable $</div>"
    if sizing:
        out.append(section("10. Serverless sizing (ECH > 50% rule)", body, new_page=True))
    return out


# Title phrases → live Elastic docs. First match wins per entry; a recommendation can collect several.
_DOC_LINKS: list[tuple[tuple[str, ...], list[tuple[str, str]]]] = [
    (("single-zone", "availability zone"), [
        ("Resiliency in Elastic Cloud Hosted", "https://www.elastic.co/docs/deploy-manage/production-guidance/availability-and-resilience/resilience-in-ech"),
        ("Fault tolerance", "https://www.elastic.co/docs/deploy-manage/deploy/elastic-cloud/ec-customize-deployment-components"),
    ]),
    (("upgrade", "lagging stack", "stack version"), [
        ("Upgrade a deployment", "https://www.elastic.co/docs/deploy-manage/upgrade"),
    ]),
    (("ingest", "autoscaling", "bursty"), [
        ("Autoscaling", "https://www.elastic.co/docs/deploy-manage/autoscaling"),
    ]),
    (("query cost", "qps", "shard"), [
        ("Size your shards", "https://www.elastic.co/docs/deploy-manage/production-guidance/optimize-performance/size-shards"),
    ]),
    (("small document", "tsds", "logsdb", "large average"), [
        ("Time series data stream", "https://www.elastic.co/docs/manage-data/data-store/data-streams/time-series-data-stream-tsds"),
        ("Logs data stream", "https://www.elastic.co/docs/manage-data/data-store/data-streams/logs-data-stream"),
    ]),
    (("frozen tier", "searchable snapshot", "index lifecycle", "ilm"), [
        ("Index lifecycle management", "https://www.elastic.co/docs/manage-data/lifecycle/index-lifecycle-management"),
    ]),
    (("ml node", "machine learning", "elser", "inference"), [
        ("Machine learning", "https://www.elastic.co/docs/explore-analyze/machine-learning"),
        ("Elastic Inference", "https://www.elastic.co/docs/explore-analyze/elastic-inference"),
    ]),
]


def doc_links(rec: dict) -> str:
    text = f"{rec.get('title') or ''} {rec.get('evidence') or ''} {rec.get('action') or ''}".lower()
    seen: set[str] = set()
    parts: list[str] = []
    for keys, items in _DOC_LINKS:
        if not any(k in text for k in keys):
            continue
        for label, url in items:
            if url in seen:
                continue
            seen.add(url)
            parts.append(str(link(url, label)))
    return " · ".join(parts)


def recs(items: list[dict]) -> str:
    if not items:
        return "<div class='cap'>No recommendations triggered.</div>"
    out = []
    for i, r in enumerate(items, 1):
        sev = (r.get("severity") or "low").lower()
        docs = doc_links(r)
        out.append(
            f"<div class='rec panel' style='border-left-color:{TONE.get(sev, '#69707D')}'>"
            f"<div><span class='t'>{i}. {esc(r.get('title'))}</span><span class='sev' style='color:{TONE.get(sev, '#69707D')}'>{esc(sev.upper())}</span></div>"
            f"<div class='e'>{esc(r.get('evidence'))}</div>"
            f"<div style='margin-top:4px'><b>Action.</b> {esc(r.get('action'))}</div>"
            + (f"<div class='cap'>Impact: {esc(r.get('impact'))}</div>" if r.get("impact") else "")
            + (f"<div class='docs'>Documentation: {docs}</div>" if docs else "")
            + "</div>"
        )
    return "".join(out)


# --------------------------------------------------------------------------- account analysis (§0–10)
def render_account_html(acc: dict) -> str:
    m, p, cl, e, d, st, so, ai, an = (acc.get(k) or {} for k in ("meta", "platform", "cloud", "ecu", "deployments", "storage", "solutions", "ai_ml", "analysis"))
    sz, wl = acc.get("sizing") or {}, acc.get("workload") or {}
    cs, org, lb = cl.get("summary") or {}, m.get("org") or {}, m.get("lookback") or {}
    bd = an.get("burndown") or {}
    con = e.get("contract") or {}
    sol = so.get("totals") or {}
    B: list[str] = []

    # header + snapshot
    contract = org.get("contract") or {}
    cterm = ""
    if contract.get("start"):
        cterm = f" · contract {str(contract['start'][0])[:10]} → {str((contract.get('end') or [''])[0])[:10]}"
    B.append("<header class='masthead'><div class='brand'>Elastic<i></i></div>"
             f"<h1>{esc(m.get('customer'))} — K360 account analysis</h1>"
             f"<div class='sub'>{esc(m.get('date'))} · lookback {esc(lb.get('start'))} → {esc(lb.get('end'))} ({esc(lb.get('days'))} d) · AE {esc(org.get('AE'))} · ARR {usd(org.get('ARR'))} · {esc(org.get('segment'))} {esc(org.get('geo'))}{esc(cterm)}</div></header>")
    sol_lbl = " / ".join(f"{lbl} {sol.get(k, {}).get('ecu_pct', 0):g}%" for k, lbl in (("observability", "O11y"), ("search", "Search"), ("security", "Sec")))
    B.append("<div class='stats' style='grid-template-columns:repeat(5,1fr)'>"
             + stat(p.get("label"), f"{p.get('ech_count')} ECH · {p.get('serverless_count')} serverless (ECH {p.get('ech_share_pct')}% of ECU)")
             + stat(n0(e.get("total_lookback")), f"ECU, {e.get('lookback_days')}-day lookback")
             + stat(f"{st.get('data_stored_tb')} TB", f"stored · as of {st.get('as_of')}")
             + stat(sol_lbl, "ECU by solution")
             + stat(bd.get("level"), f"rate {bd.get('consumption_rate')}× · runway {bd.get('runway_days')} d", "danger" if "OVERAGE" in str(bd.get("level")) else None)
             + "</div>")
    est = sz.get("estimators") or {}
    if sz.get("triggered"):
        o = est.get("observability") or {}
        rb = o.get("readback") or {}
        B.append(callout("Serverless sizing triggered (ECH > 50%)", f"Observability estimator: <b>{esc(rb.get('monthly_steady_state'))}/mo · {esc(rb.get('annual_year1'))} year 1</b> vs ≈{n0(e.get('monthly_avg_complete_months'))} ECU/mo on ECH. "
                         + (f"{link(o.get('url'), 'Open estimator')} · " if o.get("url") else "") + (link((sz.get("drive_folder") or {}).get("url"), "Drive folder") if sz.get("drive_folder") else ""), "warning"))

    # 1 platform
    b = [f"<p><b>{esc(p.get('label'))}</b> — {esc(p.get('ech_count'))} ECH deployments / {n0(p.get('ech_ecu'))} ECU; {esc(p.get('serverless_count'))} serverless / {n0(p.get('serverless_ecu'))} ECU. "
         f"{esc(cs.get('provider'))} {esc(cs.get('region'))}, plan {esc(cs.get('plan'))}, hot instance <b>{esc(cs.get('hot_instance'))}</b>, mostly {esc(cs.get('zones'))} AZ. Templates: "
         + esc(", ".join(f"{t['template']} ({t['deployments']})" for t in cl.get("templates") or [])) + "</p>"]
    tiers = [(t["tier"], t["ecu"]) for t in cl.get("tiers") or []]
    z1 = cl.get("single_zone") or []
    zones = cl.get("zones") or []
    right = "<h3>Availability zones</h3>" + table(
        ["Zones", "ECU", "%"],
        [[f"{z['zones']} AZ", n0(z["ecu"]), pct(z["pct"])] for z in zones],
        tones=["danger" if z.get("zones") == 1 else None for z in zones],
        boxed=True,
    )
    if z1:
        shown = ", ".join(r["name"] for r in z1[:4])
        more = f" +{len(z1) - 4} more" if len(z1) > 4 else ""
        right += caption(f"Warning: {len(z1)} deployment(s) in 1 AZ ({shown}{more}). A zone outage takes them offline.")
    right += "<h3>Non-capacity billing</h3>" + table(["Type", "ECU", "% of spend"], [[t["type"], n0(t["ecu"]), pct(t["pct"])] for t in cl.get("non_capacity") or [] if t.get("ecu")], boxed=True)
    b.append(grid(
        panel("<h3>Capacity ECU by tier</h3>" + hbar_chart(tiers, show_pct_of=cl.get("capacity_ecu")) + caption(f"Capacity ECU from Cloud-* SKUs · {n0(cl.get('capacity_ecu'))} total · Source: K360 billing")),
        panel(right),
        ratio="3fr 2fr",
    ))
    inst = cl.get("instances") or []
    shown = inst[:4]
    b.append(panel("<h3>Instance families" + (f" (top {len(shown)} of {len(inst)})" if len(inst) > len(shown) else "") + "</h3>" + table(["Instance", "Tier", "ECU", "% of capacity", "Deployments (hot)"], [[i["instance"], i["tier"], n0(i["ecu"]), pct(i["pct"]), i.get("deployments_hot") or "—"] for i in shown], ["left", "left", "right", "right", "right"], boxed=True)))
    projs = ((p.get("serverless") or {}).get("projects")) or []
    if projs:
        b.append(panel("<h3>Serverless projects</h3>" + table(["Project", "Solution", "ECU", "First bill", "Last bill"], [[x.get("project_id"), x.get("solution") or x.get("template"), n0(x.get("ecu")), x.get("first_bill"), x.get("last_bill")] for x in projs[:8]], ["left", "left", "right", "left", "left"], boxed=True)
                 + (caption(f"Top 8 of {len(projs)} projects by ECU") if len(projs) > 8 else "")))
    regions = [r for r in (cl.get("regions") or []) if r.get("region")]
    by_region: dict[str, int] = {}
    for dep in cl.get("by_deployment") or []:
        by_region[dep.get("region") or ""] = by_region.get(dep.get("region") or "", 0) + 1
    tallest = max(by_region.values()) if by_region else 0
    # A short fleet fits under the snapshot. A tall one keeps the table on its own page.
    share_cover = 0 < tallest <= 12
    arch = architecture_table(acc) if regions else ""
    if arch:
        as_of = st.get("as_of")
        B.append(section(
            "Current ECH architecture",
            caption(
                "Utilization is that deployment's share of lookback ECU. Stored size, documents, and average document size are the telemetry snapshot"
                + (f" as of {as_of}" if as_of else "")
                + ". A shaded row is one availability zone, with no zone failover."
            ) + arch,
            new_page=not share_cover,
        ))
        topo = regional_architecture_svg(acc) + deployment_topology(acc)
        if topo.strip():
            B.append(section("Topology", topo, new_page=True))
    B.append(section("1. Platform, cloud & instance footprint", "".join(b), new_page=True))

    # 2 + 3 share one page
    vs = d.get("version_summary") or {}
    byv = d.get("by_version") or []
    pie = [(v["version"], v["count"]) for v in byv[:6]] + ([("other", sum(v["count"] for v in byv[6:]))] if len(byv) > 6 else [])
    mo = e.get("monthly") or {}
    partial = set(mo.get("partial_months") or [])

    def _mlab(s: str) -> str:
        try:
            y, mm = s.split("-")
            return f"{int(mm)}/{y[2:]}" + ("*" if s in partial else "")
        except (ValueError, IndexError):
            return s

    cats = [_mlab(x) for x in mo.get("months") or []]
    pk, lo, dp = e.get("monthly_peak") or {}, e.get("monthly_low") or {}, e.get("daily_peak") or {}
    metrics = table(["Metric", "Value"], [
        ["Total (lookback)", n0(e.get("total_lookback"))],
        ["Monthly average", n0(e.get("monthly_avg_complete_months"))],
        ["Peak / low month", f"{pk.get('month')} {n0(pk.get('ecu'))} / {lo.get('month')} {n0(lo.get('ecu'))}"],
        ["Daily avg / peak", f"{n0(e.get('daily_avg'))} / {dp.get('day')} {n0(dp.get('ecu'))}"],
        ["Last 30 days", f"{n0(e.get('last_30d_daily_avg'))}/d · {(e.get('trend') or {}).get('last_30d_vs_lookback_avg_pct')}%"],
        ["Commit / required", f"{n0(con.get('commit_pace_ecu_per_day'))} / {n0(con.get('required_pace_ecu_per_day'))} ECU/d"],
    ])
    ot = ((d.get("over_time") or {}).get("month")) or {}
    tr = d.get("trend") or {}
    tone = "warning" if "significant" in str(tr.get("flag")) else "info"
    mom = tr.get("largest_mom_change") or {}
    trend_txt = (f"Average {tr.get('first_avg')} ({' – '.join(tr.get('first_period') or [])}) → {tr.get('last_avg')} ({' – '.join(tr.get('last_period') or [])}), Δ {tr.get('delta')} ({tr.get('delta_pct')}%). "
                 f"Peak {(tr.get('peak') or {}).get('count')} in {(tr.get('peak') or {}).get('month')}; largest change {mom.get('delta')} in {mom.get('month')}.")
    top = d.get("top") or []
    b = [grid(
        "<h3>Stack version</h3>" + donut(pie, size=132) + caption(f"{', '.join(f'{k}×{v}' for k, v in (vs.get('majors') or {}).items())} · reference {vs.get('reference_version')} · lagging {vs.get('lagging_count')} ({vs.get('lagging_ecu_pct')}% ECU)"),
        "<h3>Versions" + (f" (top 6 of {len(byv)})" if len(byv) > 6 else "") + "</h3>" + table(["Version", "Deps", "ECU", "%"], [[v["version"], v["count"], n0(v.get("ecu")), pct(v.get("ecu_pct"))] for v in byv[:6]]),
        "<h3>ECU metrics</h3>" + metrics,
        ratio="1.1fr 1fr 1.2fr")]
    b.append("<h3>Monthly ECU</h3>" + bar_chart(cats, [("ECU", mo.get("totals") or [])], height=148, ref=(float(e.get("monthly_avg_complete_months") or 0), "avg")) + caption("Month (* = partial) · Y: ECU · Source: K360 billing"))
    b.append("<h3>Top deployments by ECU" + (f" (6 of {d.get('count')})" if len(top) > 6 else "") + "</h3>" + table(
        ["Deployment", "Version", "Template", "ECU", "%", "ECU/mo", "Months"],
        [[t["name"], t.get("version"), t.get("template"), n0(t["ecu"]), pct(t.get("ecu_pct")), n0(t.get("ecu_monthly_avg")), t.get("months_active")] for t in top[:6]],
        ["left", "left", "left", "right", "right", "right", "right"]))
    B.append(section("2. Deployments, stack versions & ECU", "".join(b), new_page=True))

    # 3 + 4 share one page
    ser = bd.get("series") or {}
    sm = ser.get("months") or []
    burn = panel("<h3>3. Remaining ECU over time</h3>" + (
        line_chart(
            [_mlab(x) for x in sm],
            series=[("Implied balance", ser.get("implied_balance") or []), ("Commit-pace forecast", ser.get("commit_pace_balance") or [])],
            height=230, show_values=False, colors=[PALETTE[0], PALETTE[2]],
        ) + caption(ser.get("note") or "") if sm else "<div class='cap'>No balance series.</div>"
    ))
    flags = "".join(callout(f["flag"], esc(f.get("detail")), f.get("severity") or "info") for f in bd.get("flags") or []) or "<div class='cap'>No burndown flags.</div>"
    deps = panel(
        "<h3>4. Active deployments</h3>"
        + line_chart([_mlab(x) for x in ot.get("periods") or []], ot.get("count") or [], height=230, show_values=False)
        + caption("Deployments with billing ECU > 0 per month · Source: K360 billing")
    )
    metrics = panel("<h3>Balance</h3>" + table(["Metric", "Value"], [
        ["Level", bd.get("level")], ["Consumption rate", f"{bd.get('consumption_rate')}×"], ["Actual daily (30 d)", n0(bd.get("daily_30d"))],
        ["Commit / required pace", f"{n0(bd.get('commit_pace'))} / {n0(bd.get('required_pace'))} ECU/d"], ["ECU balance", n0(bd.get("ecu_balance"))],
        ["Runway", f"{bd.get('runway_days')} d → {bd.get('runout_date')}"], ["Contract end", bd.get("nearest_contract_end")], ["Annual projection", n0(bd.get("annual_projection"))],
    ] + [
        [f"Next {h.get('months')} months", _horizon_cell(h)] for h in ((an.get("projections") or {}).get("horizons") or [])
    ], boxed=True) + (f"<div class='cap'>{esc((an.get('projections') or {}).get('note'))}</div>" if (an.get("projections") or {}).get("note") else ""))
    B.append(section(
        "3–4. Burndown and deployments over time",
        grid(burn, deps, ratio="1.35fr 1fr", gap="28px")
        + grid(metrics, panel("<h3>What changed</h3>" + callout(str(tr.get("flag") or "trend").capitalize(), esc(trend_txt), tone) + flags), ratio="1.15fr 1fr", gap="28px"),
        new_page=True,
    ))

    # 5 storage and 6 ECU each get a page and a time chart. 7 is its own page.
    tm = st.get("tier_mix") or {}
    cc = so.get("cross_cutting") or {}
    fc = ai.get("feature_counts") or {}
    ai_deps = ai.get("by_deployment") or []
    sp, ig = (wl.get("search") or {}), (wl.get("ingest") or {})
    pk_, np_ = sp.get("peak") or {}, sp.get("nonpeak") or {}
    body46 = [grid(
        donut([(k.replace("data", ""), (v or {}).get("ecu")) for k, v in tm.items()], size=112),
        table(["Metric", "Value"], [
            ["Stored", f"{st.get('data_stored_tb')} TB"],
            ["Documents", n0(st.get("docs_count"))],
            ["Avg doc", f"{st.get('avg_doc_kb')} KB"],
            ["ECU / GB / mo", st.get("ecu_per_gb_mo_org")],
            ["Snapshot ECU", n0(st.get("snapshot_storage_ecu"))],
            ["Telemetry", f"{st.get('as_of')} ({st.get('telemetry_age_days')} d)"],
        ]),
        table(["Largest by data", "GB", "ECU/mo"], [[x["name"], n0(x.get("data_stored_gb")), n0(x.get("ecu_monthly_avg"))] for x in (st.get("by_deployment") or [])[:4]]),
        ratio="0.8fr 1fr 1.5fr")]
    hist = st.get("monthly") or {}
    hist_months = hist.get("months") or []
    hist_gb = hist.get("gb") or []
    if hist_months and hist_gb:
        body46.append(
            line_chart([_mlab(x) for x in hist_months], [float(v or 0) for v in hist_gb], height=260, show_values=False)
            + caption(hist.get("note") or "Monthly stored GB.")
            + caption(f"Y: GB · Source: {hist.get('source') or 'K360'}. The snapshot above is one day and can differ from the peak in that month.")
        )
    body6: list[str] = ["<h3 style='margin-top:0'>ECU by solution</h3>" + grid(
        donut([(k.title(), v.get("ecu")) for k, v in sol.items()], size=100),
        table(["Solution", "Deps", "ECU", "%", "ECU/mo", "GB"], [[k.title(), v.get("deployments"), n0(v.get("ecu")), pct(v.get("ecu_pct")), n0(v.get("ecu_monthly_avg")), n0(v.get("gb"))] for k, v in sol.items()])
        + caption(f"Of which ML nodes {n0(cc.get('ml_node_ecu'))} ECU, EIS {cc.get('genai_eis_inference_ecu')} ECU · " + ", ".join(f"{k} {v}" for k, v in (so.get("method_counts") or {}).items())),
        ratio="0.7fr 2fr")]
    commit = float((e.get("contract") or {}).get("commit_pace_ecu_per_day") or 0)
    rng = e.get("range") or {}
    start_s, end_s = str(rng.get("start") or "")[:10], str(rng.get("end") or "")[:10]
    util_months = mo.get("months") or []
    util_vals: list[float] = []
    for month, ecu_m in zip(util_months, mo.get("totals") or []):
        try:
            year, month_n = (int(x) for x in str(month).split("-"))
            first = date(year, month_n, 1)
            last = date(year, month_n, calendar.monthrange(year, month_n)[1])
            if start_s:
                first = max(first, date.fromisoformat(start_s))
            if end_s:
                last = min(last, date.fromisoformat(end_s))
            days = max((last - first).days + 1, 1)
        except ValueError:
            days = 30
        pace = commit * days
        util_vals.append(round(100 * float(ecu_m or 0) / pace, 1) if pace else float(ecu_m or 0))
    if util_months and commit:
        body6.append(
            line_chart([_mlab(x) for x in util_months], util_vals, height=280, show_values=False)
            + caption("Y: percent of the commit. Monthly billed ECU ÷ (commit ECU/day × days of that month inside the lookback). 100% matches the commit. Source: K360 billing.")
        )
    elif util_months:
        body6.append(
            line_chart([_mlab(x) for x in util_months], [float(v or 0) for v in (mo.get("totals") or [])], height=280, show_values=False)
            + caption("Y: ECU · No commit is on the contract, so this is monthly billed ECU. Source: K360 billing.")
        )
    body7: list[str] = []
    def _mtok(v) -> str:
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "—"
        return f"{v:,.2f}" if abs(v) >= 0.1 else f"{v:,.4f}"

    tm = ai.get("token_meter") or {}
    meter_rows = tm.get("rows") or []
    def _models(x) -> str:
        labs = x.get("model_labels") or x.get("models") or []
        if not labs:
            return "—"
        shown_m = ", ".join(labs[:2])
        if len(labs) > 2:
            shown_m += f" +{len(labs) - 2}"
        return shown_m

    ai_rows = [[x["name"], n0(x.get("ml_node_ecu")), n1(x.get("eis_inference_ecu")), pct(x.get("ai_pct_of_dep")), "yes" if x.get("elser") else "—", "yes" if x.get("vector_mappings") or x.get("vectors") else "—", _models(x)] for x in ai_deps[:5]]
    eis_label = "EIS ECU"
    if meter_rows:
        eis_label = f"EIS ECU · {_mtok(tm.get('million_tokens'))}M tokens"
    body7.append("<div class='stats' style='grid-template-columns:repeat(5,1fr)'>"
                  + stat(n0(ai.get("ml_node_ecu")), f"ML node ECU ({ai.get('ml_node_pct_of_org')}%)")
                  + stat(ai.get("eis_inference_ecu"), eis_label)
                  + stat(fc.get("vector_mappings"), "vector deployments")
                  + stat(fc.get("elser"), "ELSER")
                  + stat(fc.get("ai_assistant"), "AI Assistant") + "</div>")
    if meter_rows:
        shown = meter_rows[:5]
        meter_tbl = table(
            ["Workload", "M tokens", "ECU", "ECU/M"],
            [[f"{(r.get('model_label') or ('Chat' if str(r.get('kind') or '').startswith('chat') else '—'))} · {(r.get('kind_label') or '').replace(' tokens', '')}", _mtok(r.get("million_tokens")), f"{float(r.get('ecu') or 0):,.2f}", "—" if r.get("ecu_per_million") is None else f"{float(r['ecu_per_million']):.2f}"] for r in shown]
            + [["Total", _mtok(tm.get("million_tokens")), f"{float(tm.get('ecu') or 0):,.2f}", ""]],
            ["left", "right", "right", "right"],
            tones=[None] * len(shown) + ["info"],
        )
        extra = f" Top {len(shown)} of {len(meter_rows)}; the rest is in the report." if len(meter_rows) > len(shown) else ""
        meter_tbl += caption("M tokens are millions. ECU/M is the billed rate." + extra)
        right = meter_tbl
    else:
        right = table(["Model", "Deps"], [[x["model"], x["deployments"]] for x in ai.get("model_counts") or []])
    body7.append(grid(
        table(["Deployment", "ML ECU", "EIS", "AI %", "ELSER", "Vectors", "Models"], ai_rows, ["left", "right", "right", "right", "center", "center", "left"]),
        right,
        ratio="1.2fr 1fr" if meter_rows else "3fr 1fr"))
    if sp:
        body7.append(caption(f"Workload ({wl.get('window_days')} d, {wl.get('indices')} indices): peak {pk_.get('qps')} QPS @ {pk_.get('complexity_ms')} ms; non-peak {np_.get('qps')} QPS; ingest ≈ {n0(ig.get('docs_per_sec'))} docs/s."))
    cons_pdf = ((acc.get("exports") or {}).get("consolidation") or {}).get("drive_url")
    rec_body = recs(an.get("performance") or [])
    if cons_pdf:
        rec_body += caption("Cost recommendations are on the consolidation report.") + f"<div class='docs'>{link(cons_pdf, 'Open the consolidation PDF')}</div>"
    B.append(section("5. Data storage", "".join(body46), new_page=True))
    B.append(section("6. ECU by solution", "".join(body6), new_page=True))
    B.append(section("7. ML, vectors & AI", "".join(body7), new_page=True))
    B.append(section("8. Current State Analysis & Recommendations", rec_body, new_page=True))
    if not consolidation_report_exists(acc):
        B.extend(cost_sizing_sections(acc))
    return page(f"{m.get('customer')} — K360 account analysis", "".join(B))


def _p(text: str) -> str:
    return f"<p style='margin:4px 0 6px'>{esc(text)}</p>"


def consolidation_inputs_html(acc: dict) -> str:
    """Where each consolidation input came from, and the arithmetic that turns it into the headline."""
    e = acc.get("ecu") or {}
    st = acc.get("storage") or {}
    sol = (acc.get("solutions") or {}).get("totals") or {}
    wl = acc.get("workload") or {}
    c = acc.get("consolidation") or {}
    sv, cr, fr, b = (c.get(k) or {} for k in ("savings", "contract_rate", "fragmentation", "basis"))
    svl = c.get("serverless") or {}
    rng = e.get("range") or {}
    partial = (e.get("monthly") or {}).get("partial_months") or []
    notes = st.get("notes") or []
    search_wl = wl.get("search") or {}
    ingest = wl.get("ingest") or {}
    ret = b.get("retention_months") or {}
    ret_txt = ", ".join(f"{k} {v} mo" for k, v in ret.items() if v is not None) or "3 mo assumed"
    missing = (fr.get("n_deployments") or 0) - (fr.get("n_with_gb") if fr.get("n_with_gb") is not None else (fr.get("n_priced") or 0))
    by_sol = svl.get("by_solution") or {}
    split = " · ".join(
        f"{k}: {n1((sol.get(k) or {}).get('gb'))} GB, {(sol.get(k) or {}).get('deployments', 0)} deps, {(sol.get(k) or {}).get('ecu_pct', 0)}% of ECU"
        for k in ("search", "observability", "security")
    )
    avg_doc = ingest.get("avg_doc_bytes")
    from_diags = _from_diagnostics(acc)
    if from_diags:
        size_src = "Elasticsearch diagnostic bundle, local primary store. Searchable snapshots are left out. A deployment with no bundle keeps its K360 size."
        size_math = "Observability and Security GB/day is measured bulk index bytes per day. K360 has no raw ingest total; stored GB ÷ assumed retention is the --k360-ingest backup and can sit far below this bulk rate. Search disk is the diagnostic store size."
        ret_src = "Size-weighted ILM delete age, else data-stream retention, else the oldest index. Ingest-weighted across the solution. The estimator accepts 1–36 months."
        ret_math = "The GB/day written to the estimator is the measured bulk rate. The sensitivity table still shows stored GB ÷ retention and does not reprice the estimate."
    else:
        size_src = notes[0] if notes else "account storage block"
        size_math = "K360 does not provide a raw ingest total. Raw GB/day = stored GB ÷ (retention months × 30.416). This backup can sit far below measured bulk. Deployments with no row are absent from this total."
        ret_src = "run assumption (default 3 months), not measured from ILM or data streams"
        ret_math = "Changes the implied GB/day shown in the sensitivity table. The estimator was not re-run, so the quoted dollars stay put."
    rows = [
        ["Lookback ECU", f"{n0(e.get('total_lookback'))} ECU, {rng.get('start') or '—'} → {rng.get('end') or '—'} ({e.get('lookback_days')} d)", "cloud-tracker-billing", f"Complete-month average is {n0(sv.get('ech_list_mo'))} ECU/mo. Partial months left out: {', '.join(partial) if partial else 'none'}." + ("" if sv.get("ech_contract_mo") is not None else " List price is $1 per ECU.")],
        ["Stored size", f"{n1(st.get('data_stored_gb'))} GB · {n0(st.get('docs_count'))} docs · as of {st.get('as_of')} · {st.get('deployments_with_telemetry')} deployments with a row", size_src, size_math],
        ["Solution split", split, "use case, then features, billing family, and name; GB joined by deployment name", "One serverless project per solution. A solution with no bulk and no stored GB is not priced."],
        ["Retention", ret_txt, ret_src, ret_math],
        ["Workload", f"{wl.get('window_days')} d · {wl.get('indices')} indices · peak {n1((search_wl.get('peak') or {}).get('qps'))} QPS for {(search_wl.get('peak') or {}).get('pct_hours_mo')}% of hours · ingest {n1(ingest.get('docs_per_sec'))} docs/s · active {n0(ingest.get('active_hours_mo'))} h/mo · avg doc {n0(avg_doc) if avg_doc is not None else 'missing'} B", "cloud-tracker-index_telemetry, day averages. Average document size is dataset_size_bytes / docs_count.", "Hour mix is the Search Serverless estimator's active search hours. Ingest docs/s is scaled by the search share of stored GB. A missing document size leaves Search ingest at $0."],
        ["Contract", f"ARR {usd(cr.get('arr'))} · commit {n0(cr.get('commit_ecu_per_day'))} ECU/day · shown rate ${cr.get('implied_usd_per_ecu')}/ECU", "organization pivot", cr.get("note") or "list view only"],
        (["Deployments without a size row",
          f"{missing} of {fr.get('n_deployments')} have no size on {st.get('as_of')}. {fr.get('n_size_moved')} now report under a sibling name. {missing - (fr.get('n_size_moved') or 0)} have no current size.",
          "cloud-tracker-data_pivot, each name's last size day and the names that started after it",
          f"The renamed clusters' data is already in the stored-GB total under the new name. All {fr.get('n_deployments')} stay in the ECU totals."]
         if fr.get("size_gaps") else
         ["Deployments without a size row", f"{missing} of {fr.get('n_deployments')} have no stored-GB row", "same size snapshot as the stored-GB total", "They stay in the ECU totals and out of the stored-GB total. They are not given a serverless price."]),
    ]
    if sv.get("ech_contract_mo") is not None:
        lines = [
            f"ECH at the contract rate is {usd(sv.get('ech_contract_mo'))}/mo: {n0(sv.get('ech_list_mo'))} ECU × ARR ÷ (commit ECU/day × 365), shown as ${cr.get('implied_usd_per_ecu')}/ECU and applied unrounded.",
            f"The serverless estimate is {usd(sv.get('serverless_mo'))}/mo. Against the contract rate that is {usd(sv.get('cost_mo'))}/mo lower ({sv.get('cost_pct')}%), or {usd(sv.get('cost_yr'))}/yr. Serverless is not discounted.",
        ]
    else:
        lines = [f"At list, $1 per ECU: {n0(sv.get('ech_list_mo'))} billed − {usd(sv.get('serverless_mo'))} estimate = {usd(sv.get('list_mo'))}/mo ({sv.get('list_pct')}%) estimated savings, or {usd(sv.get('list_yr'))}/yr."]
    body = table(["Input", "Value used", "Where it came from", "How it enters the math"], rows, ["left", "left", "left", "left"])
    body += bullets([
        "Every serverless dollar is an estimate from the serverless estimators. It is not a quote or a bill.",
        "The component breakdown and the sizing inputs are on the previous page.",
        *lines,
    ])
    search = by_sol.get("search") or {}
    if search.get("peak_vcu") is not None and not str(search.get("pricing") or "").startswith("es3-estimator"):
        mix = search.get("hour_mix") or {}
        on_floor = float(search.get("peak_vcu") or 0) <= 28.05 and float(search.get("idle_vcu") or 0) <= 28.05
        floor = " Both sit on the 28 VCU floor at this stored size." if on_floor else ""
        ingest_note = " Average document size is missing in this run, so ingest is the 4 VCU floor." if avg_doc is None else ""
        body += _p(
            f"Search model. Peak VCU = max(28, 1,352 × stored GB / 139,760) = {search.get('peak_vcu')}. "
            f"Idle VCU = max(28, 1,204 × stored GB / 139,760) = {search.get('idle_vcu')}.{floor} "
            f"Hour mix is peak {mix.get('peak_pct')}%, non-peak {mix.get('nonpeak_pct')}%, idle {mix.get('idle_pct')}% of 730 h, from the workload window. "
            f"Non-peak hours are charged at the peak VCU. Search $ = those hours × VCU × $0.09 = {usd(search.get('search_mo'))}. "
            f"Retention $ = {n1(search.get('storage_gb') or search.get('stored_gb'))} GB × $0.047 = {usd(search.get('retention_mo'))}. "
            f"Ingest VCU = 4 when docs/s or average bytes is missing, otherwise max(4, MB/s × 4), × active hours × $0.14 = {usd(search.get('ingest_mo'))}.{ingest_note} "
            f"Support = 15% of those three = {usd(search.get('support_mo'))}. Sum = {usd(search.get('total_mo'))}/mo. This is a model, not a sheet."
        )
    return section("Data used, and how the headline is calculated", body, new_page=True)


def serverless_component_slide(acc: dict) -> str:
    """Component dollars and the sizing inputs, one page."""
    c = acc.get("consolidation") or {}
    svl = c.get("serverless") or {}
    by_sol = svl.get("by_solution") or {}
    sz = acc.get("sizing") or {}
    comp_rows = []
    for k, v in by_sol.items():
        if not ((v.get("stored_gb") or 0) > 0 or (v.get("total_mo") or 0) > 0):
            continue
        parts = []
        if v.get("search_mo") is not None:
            parts.append(f"search VCU {usd(v.get('search_mo'))}")
        if v.get("ingest_mo") is not None:
            parts.append(f"ingest {usd(v.get('ingest_mo'))}")
        if v.get("retention_mo") is not None:
            parts.append(f"retain {usd(v.get('retention_mo'))}")
        if v.get("eis_mo"):
            parts.append(f"EIS {usd(v.get('eis_mo'))}")
        if v.get("support_mo") is not None:
            parts.append(f"support 15% {usd(v.get('support_mo'))}")
        comp_rows.append([k.title(), " + ".join(parts) or "—", usd(v.get("total_mo")), estimator_label(v.get("pricing"))])
    left = "<h3>By component</h3>" + table(
        ["Project", "Components / mo", "Estimate / mo", "Estimator"], comp_rows, ["left", "left", "right", "left"],
    ) + bullets([
        "On a Search row, add the component dollars to get the monthly total. Support is 15% of the other Search components.",
        "The Observability row is one total. It is not split into ingest, retention, and support.",
    ])
    cons = sz.get("consolidated") or {}
    right = "<h3>Sizing inputs</h3>" + table(
        ["Solution", "Deployments", "ECU/mo", "Stored GB", "Kept (mo)", "GB/day"],
        [[k.title(), v.get("deployments"), n0(v.get("ecu_monthly_avg")), n0(v.get("stored_gb")), v.get("retention_months"), n1(v.get("daily_raw_gb"))] for k, v in cons.items()],
        ["left", "right", "right", "right", "right", "right"],
    )
    diag_cov = sz.get("diagnostics") or {}
    if diag_cov.get("active_month"):
        month = diag_cov.get("active_month")
        partial = " That month is partial." if diag_cov.get("active_month_partial") else ""
        roster_notes = [
            f"Deployments are those with a bill in {month}: {n0(diag_cov.get('active_deployments'))}.{partial}",
        ]
        if diag_cov.get("inactive_excluded"):
            roster_notes.append(
                f"{n0(diag_cov.get('inactive_excluded'))} deployments had no bill that month. Their last stored snapshot, {n0(diag_cov.get('inactive_snapshot_gb'))} GB, is not in this sizing."
            )
        if diag_cov.get("ecu_month") and diag_cov.get("active_month_partial"):
            roster_notes.append(f"ECU/mo is {diag_cov.get('ecu_month')} billing for the active deployments, the last complete month.")
        if diag_cov.get("k360_fallback"):
            n = int(float(diag_cov.get("k360_fallback") or 0))
            noun = "deployment" if n == 1 else "deployments"
            verb = "has" if n == 1 else "have"
            add = "adds" if n == 1 else "add"
            roster_notes.append(f"{n0(n)} active {noun} {verb} no diagnostic bundle and {add} no ingest.")
        right += bullets(roster_notes)
    est = sz.get("estimators") or {}
    project_rows = []
    for sol_name in ("search", "observability", "security"):
        for region_row in (by_sol.get(sol_name) or {}).get("by_region") or []:
            project_rows.append([
                f"{sol_name.title()} · {region_row.get('region')}",
                usd(region_row.get("sizing_monthly") if region_row.get("sizing_monthly") is not None else region_row.get("monthly")),
                usd(region_row.get("monthly")),
                link(region_row.get("url"), "Open sizing") if region_row.get("url") else "not priced",
            ])
    est_rows = []
    if not project_rows:
        for k in ("observability", "security", "search"):
            x = est.get(k) or {}
            if not x:
                continue
            rb = x.get("readback") or {}
            blk = by_sol.get(k) or {}
            name = {"search": "Search Serverless", "observability": "Observability Serverless", "security": "Security Serverless"}.get(k, k.title())
            if k == "search" and blk.get("by_region") and blk.get("total_mo") is not None:
                monthly = usd(blk.get("total_mo"))
                year1 = usd(float(blk["total_mo"]) * 12)
            elif rb:
                monthly = usd(rb.get("monthly_steady_state")) if isinstance(rb.get("monthly_steady_state"), (int, float)) else (rb.get("monthly_steady_state") or "—")
                year1 = usd(rb.get("annual_year1")) if isinstance(rb.get("annual_year1"), (int, float)) else (rb.get("annual_year1") or "—")
            else:
                monthly, year1 = "—", "—"
            est_rows.append([name, monthly, year1, link(x.get("url"), "Open estimator") if x.get("url") else "not priced"])
    body = left + right
    search = by_sol.get("search") or {}
    if project_rows:
        project_rows.append(["Total", "", usd(svl.get("total_mo")), ""])
        body += "<h3>One sizing per region</h3>" + table(["Project", "Sizing / mo", "In the estimate", ""], project_rows, ["left", "right", "right", "left"], tones=[None] * (len(project_rows) - 1) + ["info"])
    elif est_rows:
        body += "<h3>Estimator copies</h3>" + table(["Estimator", "Monthly", "Year 1", ""], est_rows, ["left", "right", "right", "left"])
    notes = []
    if project_rows:
        notes.append("Each link is that region's sizing. A Search sizing is the capacity estimate. The estimate column adds that region's EIS, and the total is the figure used in this report.")
    if any("skipped" in str((est.get(k) or {}).get("status") or "") for k in ("security", "observability")):
        notes.append("Security was not priced. There is no security workload.")
    body += bullets(notes)
    folder = (sz.get("drive_folder") or {}).get("url")
    if folder:
        body += f"<p style='margin:4px 0 0'>{link(folder, 'Drive folder')}</p>"
    return section("Estimated monthly serverless total, by component", body, new_page=True)


def _raw_cell(v: float) -> str:
    return "0" if v <= 0 else f"{v:,.3f}"


def _metered_cell(v: float) -> str:
    if v <= 0:
        return "0"
    if v < 0.01:
        return f"{v:.3f}"
    return f"{v:,.2f}"


def metered_rate_section(acc: dict) -> str:
    """Per-deployment Observability ingest rate. Not a D32 quote."""
    c = acc.get("consolidation") or {}
    rates = list(c.get("metered_rates") or [])
    if not rates and _from_diagnostics(acc):
        from consolidation import observability_metered_rates
        rates = observability_metered_rates((acc.get("solutions") or {}).get("by_deployment") or [])
    if not rates:
        return ""

    def row(r: dict) -> list:
        rate = r.get("usd_per_gb")
        return [
            r.get("name") or "",
            _raw_cell(float(r.get("raw_gb_day") or 0)),
            _metered_cell(float(r.get("metered_gb_day") or 0)),
            "—" if rate is None else f"${float(rate):.3f}",
        ]

    def at_top_rate(r: dict) -> bool:
        rate = r.get("usd_per_gb")
        return rate is not None and abs(float(rate) - 0.5) < 1e-6

    headers = ["Deployment", "Raw GB/day", "Metered GB/day", "Metered rate"]
    align = ["left", "right", "right", "right"]
    mid = (len(rates) + 1) // 2
    left, right = rates[:mid], rates[mid:]
    body = bullets([
        "Metered GB/day is raw bulk × 1.595. Logs are 90% and metrics are 10%. Each stream walks the Observability ingest bands on its own. The rate is that deployment's blended dollars per metered GB when it is its own project.",
        "The first 50 metered GB/day are $0.50 per GB. The bands then step to $0.325, $0.225, $0.15, $0.105, $0.10, $0.095, and $0.0925. A deployment inside the first band on both streams is $0.500, and that row is marked in red. A deployment with no bulk has no rate.",
        "This rate is not the monthly quote. The monthly quote remains the Observability estimator read-back, which also includes retention and support.",
    ])
    body += grid(
        table(headers, [row(r) for r in left], align, ["danger" if at_top_rate(r) else None for r in left]),
        table(headers, [row(r) for r in right], align, ["danger" if at_top_rate(r) else None for r in right]),
        ratio="1fr 1fr",
        gap="16px",
    )
    return section("Metered ingest rate by deployment", body, new_page=True)


def search_power_section(acc: dict) -> str:
    """Search-power projection. Not part of the Observability D32 quote."""
    profiles = ((acc.get("consolidation") or {}).get("search_power") or {}).get("profiles") or []
    if not profiles:
        return ""
    order = {"on-demand": 0, "performant": 1, "high availability": 2}
    profiles = sorted(profiles, key=lambda p: order.get(str(p.get("Search Power Setting") or "").lower(), 9))
    labels = [str(p.get("Search Power Setting") or "") for p in profiles]
    monthly = [float(p.get("Total $/month") or 0) for p in profiles]
    stored = ((acc.get("consolidation") or {}).get("basis") or {}).get("stored_gb")
    stored_bit = f"{float(stored):,.0f} GB" if stored else "the sized stored GB"
    body = "<h3>Monthly search-power estimate</h3>" + bar_chart(
        labels,
        [("Search power, $ / month", monthly)],
        width=700, height=180, show_values=True, prefix="$", exact_values=True,
    )
    body += table(
        ["Search power", "$ / month", "Steady annual", "Provisioned VCU", "Idle VCU", "Search $", "Ingest $", "Storage $", "Support $"],
        [[
            p.get("Search Power Setting"),
            usd(p.get("Total $/month")),
            usd(float(p.get("Total $/month") or 0) * 12),
            p.get("Provisioned Search VCU/h"),
            p.get("Idle Search VCU/h"),
            usd(p.get("Search $/month")),
            usd(p.get("Ingest $/month")),
            usd(p.get("Storage $/month")),
            usd(p.get("Support $/month")),
        ] for p in profiles],
        ["left"] + ["right"] * 8,
        ["success" if str(p.get("Search Power Setting")) == "on-demand" else None for p in profiles],
    )
    body += bullets([
        "On-demand, performant, and high availability are the three search-power settings. High availability is the high-demand option: it keeps the most search capacity ready.",
        f"The bars use {stored_bit}, the measured query rate, and Premium support. They are not added to the Observability ingest quote.",
        "Y: dollars per month. X: search power.",
    ])
    return section("Serverless search-power projection", body, new_page=True)


# --------------------------------------------------------------------------- consolidation (§11)
def render_consolidation_html(acc: dict) -> str:
    m, c = acc.get("meta") or {}, acc.get("consolidation") or {}
    sv, comp, svl, fr, cr, b0 = (c.get(k) or {} for k in ("savings", "ech_composition", "serverless", "fragmentation", "contract_rate", "basis"))
    dom_key = svl.get("dominant_solution") or "observability"
    dom = (svl.get("by_solution") or {}).get(dom_key) or {}
    cloud = (acc.get("cloud") or {}).get("summary") or {}
    B: list[str] = []
    diag = _from_diagnostics(acc)
    headline = f"{esc(m.get('customer'))} — estimated savings from consolidating on {esc(dom_key.title())} Serverless"
    if diag:
        headline += ", measured from cluster diagnostics"
    scenario = c.get("retention_scenario")
    if scenario == "30d":
        headline += " — retention 30 days"
    elif scenario == "rounded":
        headline += " — retention rounded up"
    source_bit = " · ingest and retention from Elasticsearch diagnostics" if diag else " · K360 backup: stored GB ÷ assumed retention"
    dep_n = n0(b0.get("deployments"))
    active_cov = ((acc.get("sizing") or {}).get("diagnostics") or {})
    if active_cov.get("inactive_excluded"):
        dep_label = f"{dep_n} active deployments"
        ecu_label = f"{n0(b0.get('ech_monthly_ecu'))} ECU/mo account bill (complete-month avg)"
    else:
        dep_label = f"{dep_n} ECH deployments"
        ecu_label = f"{n0(b0.get('ech_monthly_ecu'))} ECU/mo (complete-month avg)"
    B.append("<header class='masthead'><div class='brand'>Elastic<i></i></div>"
             f"<h1>{headline}</h1>"
             f"<div class='sub'>{dep_label} · {ecu_label} · {n0(b0.get('stored_gb'))} GB stored · {esc(cloud.get('provider'))} {esc(cloud.get('region'))} · {esc(m.get('date'))}{source_bit}</div></header>")
    rc = c.get("reconciliation") or {}
    if sv.get("cost_mo") is not None:
        stats = [
            stat(usd(sv.get("ech_contract_mo")), "Monthly ECH cost at the contract rate", tag="calculated"),
            stat(usd(sv.get("serverless_mo")), "Estimated monthly serverless cost", "success", tag="estimate", estimate=True),
            stat(f"{usd(sv.get('cost_mo'))} ({pct(sv.get('cost_pct'))})", "Estimated monthly reduction versus the contract rate", "success", tag="estimate", estimate=True),
        ]
        compare_cats = ["ECH at contract rate", "Serverless estimate"]
        ech_vals = [float(sv.get("ech_contract_mo") or 0), 0.0]
        svl_vals = [0.0, float(sv.get("serverless_mo") or 0)]
    else:
        stats = [
            stat(usd(sv.get("ech_list_mo")), "Monthly ECH cost at list price", tag="billed"),
            stat(usd(sv.get("serverless_mo")), "Estimated monthly serverless cost", "success", tag="estimate", estimate=True),
            stat(f"{usd(sv.get('list_mo'))} ({pct(sv.get('list_pct'))})", "Estimated monthly reduction versus list price", "success", tag="estimate", estimate=True),
        ]
        compare_cats = ["ECH at list", "Serverless estimate"]
        ech_vals = [float(sv.get("ech_list_mo") or 0), 0.0]
        svl_vals = [0.0, float(sv.get("serverless_mo") or 0)]
    B.append(f"<div class='stats' style='grid-template-columns:repeat({len(stats)},1fr)'>{''.join(stats)}</div>")
    B.append("<h3>ECH cost today vs. the serverless estimate</h3>" + bar_chart(
        compare_cats, [("ECH at the contract rate" if sv.get("cost_mo") is not None else "ECH, from billed ECU", ech_vals), ("Serverless, estimate", svl_vals)],
        stacked=True, width=700, height=140, show_values=True, prefix="$", exact_values=True, colors=[PALETTE[0], PALETTE[2]]))
    plan = c.get("deployment_plan") or []
    if plan:
        plan_ref = (float(sv.get("ech_contract_mo")), "ECH bill") if sv.get("ech_contract_mo") is not None else None
        B.append("<h3>Monthly estimate if the deployment count is cut</h3>" + bar_chart(
            [p.get("label") or "" for p in plan],
            [("Observability estimate", [float(p.get("monthly") or 0) for p in plan])],
            width=700, height=168, show_values=True, prefix="$", exact_values=True, ref=plan_ref,
        ))
        if c.get("retention_set_days") == 30:
            plan_ret = "Retention on every project is 30 days, written as 1 month."
        else:
            plan_ret = "Retention on each project is its measured months, rounded up."
        B.append(caption(
            "0% keeps one project per active deployment. 25, 50, and 75% pack the fleet into that many fewer projects. "
            "100% is a single project. Each bar is the sum of Observability estimator read-backs. "
            + plan_ret
        ))
    quote_bits = []
    for q in rc.get("quotes") or []:
        quote_bits.append(f"{estimator_label(q.get('source'))} {usd(q.get('monthly'))}/mo")
    if sv.get("cost_mo") is not None:
        cover_notes = [
            "Calculated: ECU at the contract rate, ARR ÷ (commit ECU per day × 365). Estimate: from the serverless estimators, not a quote or a bill. The contract rate is not applied to serverless.",
            ("The serverless estimate is " + " plus ".join(quote_bits) + ".") if quote_bits else "The serverless estimate is one project per solution.",
            f"The rate is ${cr.get('implied_usd_per_ecu')} per ECU, shown rounded and applied unrounded. That puts ECH at {usd(sv.get('ech_contract_mo'))}/mo. The serverless estimate is {usd(sv.get('cost_mo'))}/mo lower, a {pct(sv.get('cost_pct'))} reduction.",
        ]
    else:
        cover_notes = [
            "Billed: ECU from K360 billing at list, $1 per ECU. Estimate: from the serverless estimators, not a quote or a bill.",
            ("The serverless estimate is " + " plus ".join(quote_bits) + ".") if quote_bits else "The serverless estimate is one project per solution.",
        ]
    if diag:
        cover_notes.append("Ingest is bulk GB/day from the diagnostic bundles. K360 does not provide a raw ingest total. Stored GB ÷ assumed retention is the --k360-ingest backup and can sit far below this bulk rate.")
        thirty = c.get("thirty_day") or {}
        if c.get("retention_set_days") == 30:
            measured = ((((acc.get("sizing") or {}).get("consolidated") or {}).get("observability") or {}).get("retention_months_measured"))
            measured_bit = f" Measured retention is {measured} months." if measured else ""
            cover_notes.append(
                f"Retention is set to 30 days and written as 1 month.{measured_bit} The saved sheet is at that month."
            )
        elif thirty.get("monthly") is not None:
            cover_notes.append(
                f"The same bulk at 30 days is written as 1 month and reads {usd(thirty.get('monthly'))}/mo, year 1 {usd(thirty.get('annual_year1'))}. The saved sheet stays at the rounded measured month."
            )
    else:
        cover_notes.append("Ingest is the K360 backup: stored GB ÷ assumed retention. K360 does not provide a raw ingest total, so this can sit far below the bulk payload the cluster accepted.")
    B.append(bullets(cover_notes))
    B.append(serverless_component_slide(acc))
    rate_page = metered_rate_section(acc)
    if rate_page:
        B.append(rate_page)
    power_page = search_power_section(acc)
    if power_page:
        B.append(power_page)
    B.append(consolidation_inputs_html(acc))
    profiles = [] if (c.get("search_power") or {}).get("profiles") else ((((c.get("serverless") or {}).get("by_solution") or {}).get("search") or {}).get("regional_profiles") or (((acc.get("sizing") or {}).get("estimators") or {}).get("search") or {}).get("profiles") or [])
    regional_profiles = bool((((c.get("serverless") or {}).get("by_solution") or {}).get("search") or {}).get("regional_profiles"))
    if profiles:
        eis_profile = float((((c.get("serverless") or {}).get("by_solution") or {}).get("search") or {}).get("eis_mo") or 0)
        prof_headers = ["Profile", "$ / mo", "Provisioned VCU", "Idle VCU", "Search $", "Storage $", "Support $"]
        if eis_profile:
            prof_headers.insert(6, "EIS $")
        prof_rows = []
        for p in profiles:
            row = [p.get("Search Power Setting"), usd(p.get("Total $/month")), p.get("Provisioned Search VCU/h"), p.get("Idle Search VCU/h"), usd(p.get("Search $/month")), usd(p.get("Storage $/month")), usd(p.get("Support $/month"))]
            if eis_profile:
                row.insert(6, usd(eis_profile))
            prof_rows.append(row)
        prof_table = table(
            prof_headers,
            prof_rows,
            ["left"] + ["right"] * (len(prof_headers) - 1),
            tones=["success" if str(p.get("Search Power Setting")) == "on-demand" else None for p in profiles],
        )
        B.append(section("Search Serverless price by profile", "<h3>Monthly cost for the same stored data</h3>" + bar_chart(
            [str(p.get("Search Power Setting") or "profile") for p in profiles],
            [("$ / month", [float(p.get("Total $/month") or 0) for p in profiles])],
            width=640, height=200, show_values=True, prefix="$",
        ) + prof_table + bullets([
            "On-demand is the sum of the regional Search projects used in this report. Performant and high availability keep more search capacity ready, so they cost more for the same GB." if regional_profiles else "On-demand is the Search Serverless price used in this report. Performant and high availability keep more search capacity ready, so they cost more for the same GB.",
            "Y: dollars per month. X: Search Serverless power setting.",
            "Ingest is $0 on every profile when average document size is missing.",
            (f"EIS is {usd(eis_profile)} on every profile, and it is inside $ / mo. Search $ + storage $ + EIS $ + support $ = $ / mo." if eis_profile else ""),
        ]), new_page=True))

    # 1
    if dom.get("daily_metered_gb") or dom.get("sheet_metered_gb_day"):
        metered_v = dom.get("sheet_metered_gb_day")
        if metered_v is None:
            metered_v = dom.get("daily_metered_gb")
        volume = f"{n0(metered_v)} GB/d metered · {n0(dom.get('retained_gb'))} GB retained"
        rate_bits = []
        if svl.get("eff_ingest_usd_per_gb") is not None:
            rate_bits.append(f"${svl.get('eff_ingest_usd_per_gb')}/GB ingest")
        if svl.get("eff_retain_usd_per_gb_mo") is not None:
            rate_bits.append(f"${svl.get('eff_retain_usd_per_gb_mo')}/GB-mo retain")
        rates = " · ".join(rate_bits) if rate_bits else "One monthly total"
        billed = "Metered ingest GB + retained GB (+ support)"
    elif str(dom.get("pricing") or "").startswith("es3-estimator"):
        od = next((p for p in (dom.get("regional_profiles") or []) if str(p.get("Search Power Setting")) == "on-demand"), None)
        vcu = (od or {}).get("Provisioned Search VCU/h", dom.get("peak_vcu"))
        volume = f"{n1(dom.get('storage_gb') or dom.get('stored_gb'))} GB stored · {vcu} provisioned search VCU"
        rates = "Search Serverless, on-demand"
        billed = "Search VCU-hours + storage + support"
    elif dom.get("peak_vcu") is not None:
        volume = f"{n1(dom.get('storage_gb') or dom.get('stored_gb'))} GB stored · {dom.get('peak_vcu')} search VCU"
        rates = "Storage-floored VCU, not a per-GB ingest rate"
        billed = "Search VCU-hours + $0.047/GB retain + ingest VCU (+ 15% support)"
    else:
        volume, rates, billed = "—", "—", "Metered usage"
    unit = table(["", "ECH (monthly, billed)", "Serverless (monthly, estimate)"], [
        ["What is billed", "RAM-hours × tier × zones + snapshots + transfer", billed],
        ["Volume basis", f"{n0(sv.get('ech_list_mo'))} ECU", volume],
        ["Cost per stored GB-month", f"{svl.get('ech_ecu_per_stored_gb_mo')} ECU", f"${svl.get('eff_usd_per_stored_gb_mo')}"],
        ["Effective unit rates", "—", rates],
        ["Tiering", "Customer-managed, each tier billed", "Not applicable — one price"],
        ["Capacity headroom", "Paid for 24×7", "None provisioned"],
    ], ["left", "left", "left"])
    by_sol_bars = svl.get("by_solution") or {}
    contract_rate = (float(sv["ech_contract_mo"]) / float(sv["ech_list_mo"])) if sv.get("ech_contract_mo") and sv.get("ech_list_mo") else None
    def _ech(v):
        v = float(v or 0)
        return round(v * contract_rate, 2) if contract_rate else v
    ser = [("Data tiers", [_ech(comp.get("data_tiers_mo")), 0]), ("Fixed overhead", [_ech(comp.get("fixed_overhead_mo")), 0]), ("ML nodes", [_ech(comp.get("ml_nodes_mo")), 0]), ("Snapshots + transfer", [_ech(comp.get("non_capacity_mo")), 0]),
           ("Ingest", [0, sum((v or {}).get("ingest_mo") or 0 for v in by_sol_bars.values())]), ("Retention", [0, sum((v or {}).get("retention_mo") or 0 for v in by_sol_bars.values())]),
           ("Search VCU", [0, sum((v or {}).get("search_mo") or 0 for v in by_sol_bars.values())]),
           ("EIS", [0, sum((v or {}).get("eis_mo") or 0 for v in by_sol_bars.values())]),
           ("Support", [0, sum((v or {}).get("support_mo") or 0 for v in by_sol_bars.values())])]
    for sol_name, sol_blk in by_sol_bars.items():
        if "sheet" in str((sol_blk or {}).get("pricing") or "") and (sol_blk.get("total_mo") or 0) > 0:
            ser.append((estimator_label(sol_blk.get("pricing")), [0, sol_blk.get("total_mo") or 0]))
    other = (sv.get("serverless_mo") or 0) - sum(x[1][1] for x in ser)
    if other > 1:
        ser.append(("Other serverless", [0, other]))
    ser = [s for s in ser if any((v or 0) > 0.5 for v in s[1])]
    chart = bar_chart(["ECH today", "Serverless consolidated"], ser, stacked=True, width=440, height=250, show_values=True)
    projects = table(["Project", "Deps", "Stored GB", "Raw GB/d", "Metered GB/d", "Total $/mo", "ECH ECU/mo", "Pricing"],
                     [[k.title(), v.get("deployments"), n0(v.get("stored_gb")), n1(v.get("daily_raw_gb")), n0(v.get("daily_metered_gb")), usd(v.get("total_mo")), n0(v.get("ech_ecu_mo")), estimator_label(v.get("pricing"))] for k, v in (svl.get("by_solution") or {}).items() if (v.get("stored_gb") or 0) > 0 or (v.get("ech_ecu_mo") or 0) > 0],
                     ["left", "right", "right", "right", "right", "right", "right", "left"])
    B.append(section("1. What you pay for today, and what replaces it", grid(panel(unit), "<h3>Monthly build-up</h3>" + chart, ratio="1.05fr 1fr") + "<h3>Projects</h3>" + projects + bullets([
        f"Left bar: those ECU at the contract rate. It includes partial months, so it will not equal the {usd(sv.get('ech_contract_mo'))} complete-month contract cost in the headline." if contract_rate else f"Left bar: Cloud-* tier ECU ÷ ({b0.get('lookback_days')} / 30.416). It includes partial months, so it will not equal the {n0(sv.get('ech_list_mo'))} complete-month average in the headline.",
        "Right bar: dollars per month from the serverless build-up.",
        "Both bars are dollars." if contract_rate else "Y axis mixes ECU and dollars because list price treats 1 ECU as $1.",
    ]), new_page=True))

    search_blk = (svl.get("by_solution") or {}).get("search") or {}
    eis_rows = search_blk.get("eis_workloads") or []
    if eis_rows:
        def _tok(w: dict) -> str:
            v = w.get("million_tokens")
            return "—" if v is None else f"{float(v):,.4f}"
        eis_ecu_sum = sum(float(w.get("ecu") or 0) for w in eis_rows)
        eis_mo_sum = sum(float(w.get("monthly") or 0) for w in eis_rows)
        eis_table = table(
            ["EIS workload", "What moves", "Million tokens", "ECU, full year", "$ / mo"],
            [[w["model"], w["kind"], _tok(w), f"{float(w.get('ecu') or 0):,.2f}", f"${float(w.get('monthly') or 0):,.2f}"] for w in eis_rows]
            + [["Total", "", "", f"{eis_ecu_sum:,.2f}", f"${eis_mo_sum:,.2f}"]],
            ["left", "left", "right", "right", "right"],
            tones=[None] * len(eis_rows) + ["info"],
        )
        B.append(section(
            "Search machine learning, priced as EIS",
            callout(
                "This EIS load is inside the estimate",
                f"Deployments that billed jina also ran {n0(search_blk.get('jina_ml_ecu'))} ECU of ML nodes over the year "
                f"({n0(search_blk.get('jina_ml_ecu_mo'))} ECU/mo). That capacity is in this EIS line at list, together with the billed tokens, "
                f"${eis_mo_sum:,.2f}/mo before support. "
                f"It is in the Search total, in the regional projects, in every Search profile, and in every row of section 5. "
                f"Quieter and busier months scale the rest of the estimate and leave this line unchanged. "
                f"Another {n0(search_blk.get('other_ml_ecu'))} ECU of ML nodes have no jina SKU and are not in the line. "
                "The node ECU is the capacity on the same deployment, not a per-model meter.",
            ) + eis_table,
            new_page=True,
        ))

    # 2
    bk = c.get("overhead_buckets") or []
    t = c.get("tail") or {}
    hi = c.get("highest_overhead_deployments") or []
    under = ["ECU in the table is the full year, not a month."]
    if bk:
        worst = max(bk, key=lambda x: float(x.get("fixed_overhead_pct") or 0))
        under.append(f"Clusters in the {worst['bucket']} group spend the most of their bill on overhead ({pct(worst.get('fixed_overhead_pct'))}).")
    if hi:
        top = hi[0]
        under.append(f"{top['name']} is the highest single cluster, at {pct(top.get('fixed_overhead_pct'))}.")
    left = ("<h3>Overhead by cluster size</h3>" + table(
        ["Size", "Clusters", "ECU", "Overhead"],
        [[x["bucket"], x["deployments"], n0(x["ecu_lookback"]), pct(x.get("fixed_overhead_pct"))] for x in bk],
    ) + (bullets(under) if under else ""))
    tail_card = ""
    if t.get("n"):
        tail_card = (f"<div class='card'><div class='hd'>Smallest clusters<span>{t['n']} clusters</span></div><div class='stats' style='grid-template-columns:repeat(3,1fr)'>"
                     + stat(f"{n0(t.get('ech_ecu_mo'))} ECU", f"per month · {pct(t.get('ech_share_pct'))} of spend")
                     + stat(f"{n0(t.get('stored_gb'))} GB", "data stored")
                     + stat(f"{n0(t.get('fixed_overhead_ecu_mo'))} ECU", "overhead per month")
                     + f"</div><div class='e'>Merging them onto fewer clusters saves about {n0(t.get('ech_consolidation_saving_mo'))} ECU/mo, if a quarter of that overhead goes away.</div></div>")
    oh = comp.get("fixed_overhead_pct")
    title2 = f"2. {pct(oh)} of the ECH bill is cluster overhead" if oh is not None else "2. Cluster overhead"
    search_eis = (svl.get("by_solution") or {}).get("search") or {}
    if search_eis.get("ml_node_ecu_mo"):
        ml_line = f"Machine learning on Search is in the EIS line: {usd(search_eis.get('eis_mo'))}/mo. That is the billed tokens plus {n0(search_eis.get('jina_ml_ecu'))} ECU of ML nodes on deployments that billed jina ({n0(search_eis.get('jina_ml_ecu_mo'))} ECU/mo). Snapshots and transfer add {n0(comp.get('non_capacity_mo'))} ECU/mo."
    else:
        ml_line = f"Machine learning adds {n0(comp.get('ml_nodes_mo'))} ECU/mo. Snapshots and transfer add {n0(comp.get('non_capacity_mo'))} ECU/mo."
    B.append(section(title2,
                     bullets([
                         f"These are nodes that do not hold data: master, Kibana, coordinating, integrations, enterprise search, and APM. Together they are {n0(comp.get('fixed_overhead_mo'))} ECU/mo. Serverless does not charge for them.",
                         ml_line,
                     ]) + grid(left, tail_card), new_page=True))

    # 3
    per_all = c.get("by_deployment") or []
    ecu_bar = hbar_chart([(r["name"], float(r.get("ech_ecu_mo") or 0)) for r in per_all[:8]], width=460, label_w=170)
    with_gb = fr.get("n_with_gb")
    gaps = fr.get("size_gaps") or []
    size_line = (f"{with_gb} of {fr.get('n_deployments')} deployments have a size on the snapshot day ({n0(fr.get('stored_gb'))} GB). {fr.get('n_size_moved')} now report under a sibling name already in that total. {fr.get('n_size_never')} have never reported a size."
                 if gaps else f"{with_gb} of {fr.get('n_deployments')} deployments have a stored-GB row ({n0(fr.get('stored_gb'))} GB). The rest stay in the ECU total only.")
    left3 = bullets([
        f"The estimate is one serverless project per solution in each region, {usd(sv.get('serverless_mo'))}/mo. Deployments are not priced on their own.",
        size_line,
        f"Fixed overhead across the fleet is {n0(fr.get('fixed_overhead_ecu_mo'))} ECU/mo. A serverless project does not bill those roles.",
        "Keep a few projects (prod / non-prod, residency), not one per team.",
    ])
    arch = c.get("architecture") or {}
    arch_html = ""
    if arch.get("groups") and arch.get("projects"):
        arch_notes = ["Each row is one solution in one region, and the project it moves into. A serverless project stays in that region."]
        if arch.get("per_region"):
            arch_notes.append("Search is re-estimated in each region, with query load split by that region's share of stored GB. Observability is one sheet per region. Each extra project carries its own minimum capacity, so the regions together cost more than one combined project.")
            misses = [p for p in arch["projects"] if "provider_default" in str(p.get("region_match") or "")]
            if misses:
                arch_notes.append(" ".join(
                    (
                        f"Search {p['region']} is not in the Search price table, so that project is priced as {str(p.get('region_match')).split('(')[0].strip()}."
                        if p.get("solution") == "search"
                        else f"{str(p.get('solution') or '').title()} {p['region']} is not on the estimator region list, so that project is priced as {str(p.get('region_match')).split('(')[0].strip()}."
                    )
                    for p in misses
                ))
        arch_notes.append("Prod and non-prod can also be separate projects. Each extra project needs its own estimate.")
        arch_html = "<h3>How ECH deployments map to serverless projects</h3>" + architecture_svg(arch["groups"], arch["projects"]) + bullets(arch_notes)
    title3 = "3. One project per solution in each region" if arch.get("per_region") else "3. One project per solution, not one per deployment"
    B.append(section(title3, grid(left3, "<h3>ECH ECU per month, largest deployments</h3>" + ecu_bar) + arch_html, new_page=True))

    # 4
    per = per_all[:10]
    has_cost = any(r.get("ech_cost_mo") is not None for r in per)
    headers = ["Deployment", "Solution", "Stored GB", "ECU/mo", "Fixed ECU/mo", "Overhead %"]
    if has_cost:
        headers.insert(4, "Contract $/mo")
    rows4 = []
    tones4 = []
    for r in per:
        gb_cell = n0(r["stored_gb"]) if r.get("gb_known") else (f"on {r['size_now_on']}" if r.get("size_now_on") else "no GB")
        row = [r["name"], r["solution"], gb_cell, n0(r["ech_ecu_mo"]), n0(r.get("fixed_overhead_ecu_mo")), pct(r.get("overhead_pct"))]
        if has_cost:
            row.insert(4, usd(r.get("ech_cost_mo")))
        rows4.append(row)
        oh = r.get("overhead_pct")
        tones4.append("warning" if oh is not None and float(oh) > 50 else None)
    body = table(headers, rows4, ["left", "left"] + ["right"] * (len(headers) - 2), tones4)
    if any(tones4):
        body += caption("Shaded rows are fixed overhead above 50% of that deployment's ECU.")
    body += "".join(callout(x["title"], esc(x["detail"]), x.get("tone") or "info") for x in c.get("callouts") or [])
    dep_notes = [
        "ECU/mo is lookback ECU ÷ (lookback days / 30.416).",
        "Overhead % is fixed-role ECU ÷ that deployment’s ECU. Fixed roles are master, Kibana, coordinating, integrations server, enterprise search, and APM.",
        ("“on <name>” means this cluster’s size now reports under that name, and the GB is counted there. “no GB” means no current size."
         if fr.get("size_gaps") else "“no GB” means the size snapshot had no row. That deployment is not given a serverless price."),
    ]
    if has_cost:
        dep_notes.insert(1, f"Contract $/mo multiplies ECU/mo by ARR ÷ (commit × 365). The rate is shown rounded as ${cr.get('implied_usd_per_ecu')}.")
    body += bullets(dep_notes)
    B.append(section(f"4. Where ECH spend sits (top {len(per)} by ECU)", body, new_page=True))

    # 5
    sens = c.get("sensitivity") or {}
    ret = sens.get("retention") or []
    fleet = sens.get("fleet") or []
    rt = table(["Kept for", "GB stored", "GB per day"],
               [[f"{r['retention_months']:g} mo" + (" (estimate)" if r.get("used") else ""), n0(r.get("stored_gb")), n1(r.get("raw_gb_day"))] for r in ret],
               tones=["info" if r.get("used") else None for r in ret])
    used = next((r for r in ret if r.get("used")), None)
    if diag:
        ret_note = "This table divides stored GB by a month count. It is not the bulk rate written to the estimator, and it does not reprice the quote."
    elif used:
        ret_note = f"Search holds {n0(used.get('stored_gb'))} GB. The backup keeps it for {used['retention_months']:g} months, about {n1(used.get('raw_gb_day'))} GB per day. A shorter keep means the same stored data is treated as having arrived faster. The estimate was not recalculated."
    else:
        ret_note = "Stored GB stays the same. A shorter keep means the same stored data is treated as having arrived faster. The price was not recalculated."

    def _fleet_axis(scenario: str) -> str:
        low = scenario.lower()
        if "lookback" in low or "average" in low:
            return "Average"
        if "last 30" in low:
            return "Last 30 days"
        if "quiet" in low or "latest complete" in low:
            return "Quietest month"
        return scenario[:14]

    names = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

    def _fleet_row_label(scenario: str) -> str:
        low = scenario.lower()
        if "quiet" in low or "latest complete" in low:
            for part in scenario.split():
                if len(part) == 7 and part[4:5] == "-" and part[:4].isdigit():
                    return f"Quietest month ({names[int(part[5:7]) - 1]} {part[:4]})"
            return "Quietest month"
        if "last 30" in low:
            return "Last 30 days"
        if "lookback" in low or "average" in low:
            return "Average month"
        return scenario

    has_contract = any(f.get("ech_cost_mo") is not None for f in fleet)
    has_svl = any(f.get("serverless_mo") is not None for f in fleet)
    fleet_series = []
    colors5 = []
    if has_contract:
        fleet_series.append(("ECH at contract rate", [f.get("ech_cost_mo") or 0 for f in fleet]))
        colors5.append(PALETTE[0])
    else:
        fleet_series.append(("ECH at list", [f["ech_ecu_mo"] for f in fleet]))
        colors5.append(PALETTE[6])
    if has_svl:
        fleet_series.append(("Serverless, estimate", [f.get("serverless_mo") or 0 for f in fleet]))
        colors5.append(PALETTE[2])
    fc = bar_chart([_fleet_axis(f["scenario"]) for f in fleet], fleet_series, width=520, height=220, show_values=True, prefix="$", colors=colors5, exact_values=True)
    if has_contract:
        ft_headers = ["Usage", "ECH at contract rate", "Serverless, estimate", "Savings vs. contract rate"]
        ft_rows = [[_fleet_row_label(f["scenario"]), usd(f.get("ech_cost_mo")), usd(f.get("serverless_mo")), usd(f.get("save_vs_contract_mo"))] for f in fleet]
        ft_align = ["left", "right", "right", "right"]
    else:
        ft_headers = ["Usage", "ECH at list", "Serverless, estimate", "Savings vs. list"]
        ft_rows = [[_fleet_row_label(f["scenario"]), usd(f["ech_ecu_mo"]), usd(f.get("serverless_mo")), usd(f.get("save_vs_list_mo"))] for f in fleet]
        ft_align = ["left", "right", "right", "right"]
    ft = table(ft_headers, ft_rows, ft_align, tones=["info" if abs(float(f.get("scale") or 0) - 1) < 1e-9 else None for f in fleet])
    others = [f for f in fleet if abs(float(f.get("scale") or 0) - 1) > 1e-9]
    fleet_notes = []
    if has_svl:
        scales = " and ".join(f"×{float(f['scale']):.2f}" for f in others)
        if any(f.get("eis_mo") for f in fleet):
            fleet_notes.append(f"EIS stays at {usd(next(f.get('eis_mo') for f in fleet if f.get('eis_mo')))}/mo in every row. Quieter and busier months scale the rest of the estimate and leave that line unchanged.")
        fleet_notes.append(f"Average month uses the serverless estimate in this report. The other rows scale it by the same usage change ({scales}). The estimators were not re-run for them." if others else "The serverless figure is the estimate in this report.")
        busy = next((f for f in fleet if "last 30" in f["scenario"].lower()), None)
        if busy and busy.get("save_vs_contract_mo") is not None:
            fleet_notes.append(f"At the last-30-day pace, the estimated savings against the contract rate grow to {usd(busy['save_vs_contract_mo'])}/mo.")
    else:
        fleet_notes.append("The serverless price was not recalculated.")
    B.append(section("5. What if data is kept longer, or the fleet is busier",
                     grid("<h3>How long Search data is kept</h3>" + rt + bullets([ret_note]),
                          "<h3>ECH vs. the serverless estimate as usage changes</h3>" + fc)
                     + ft + bullets(fleet_notes),
                     new_page=True))

    # 6
    quotes = rc.get("quotes") or []
    if quotes:
        card = ("<div class='card'><div class='hd'>Serverless estimators<span>the prices in this report</span></div>"
                + table(["Solution", "Estimator", "$ / mo", "Year 1", "Steady annual"],
                        [[q.get("solution", "").title(), estimator_label(q.get("source")), usd(q.get("monthly")), usd(q.get("annual_year1")), usd(q.get("annual_steady_state"))] for q in quotes]
                        + [["Total", "both estimators", usd(rc.get("total_mo")), "—", "—"]])
                + "<br>".join(str(link(q.get("url"), f"Open the {estimator_label(q.get('source')).split(',')[0]} estimator")) for q in quotes if q.get("url"))
                + bullets(["These are the serverless estimates. They add up to the headline. Section 5 scales them for quieter and busier months."])
                + "</div>")
    elif rc:
        est = ((acc.get("sizing") or {}).get("estimators") or {})
        o11y_est = est.get("observability") or {}
        rb = o11y_est.get("readback") or {}
        cells = o11y_est.get("cells") or {}
        o11y_model = ((svl.get("by_solution") or {}).get("observability") or {}).get("total_mo")
        if rb:
            written = ", ".join(f"{k}={v}" for k, v in cells.items())
            card = ("<div class='card'><div class='hd'>Observability sheet<span>only quotable $</span></div>"
                    + table(["", "Sheet read-back", "Ladder model"], [
                        ["Raw GB/day written", rb.get("total_raw_gb_day"), n1(((svl.get("by_solution") or {}).get("observability") or {}).get("daily_raw_gb"))],
                        ["Metered GB/day", rb.get("metered_gb_day"), n1(((svl.get("by_solution") or {}).get("observability") or {}).get("daily_metered_gb"))],
                        ["Monthly", rb.get("monthly_steady_state"), usd(o11y_model)],
                        ["Year 1", rb.get("annual_year1"), "—"],
                        ["Steady annual", rb.get("annual_steady_state"), usd((o11y_model or 0) * 12)],
                    ])
                    + (str(link(o11y_est.get("url"), "Open Observability estimator")) if o11y_est.get("url") else "")
                    + bullets([
                        f"The headline was not matched to this sheet. The dominant solution is {dom_key}.",
                        "Quote the Observability sheet for that slice.",
                        "Quote the es3-estimator on-demand figure for Search, not the storage-floor model.",
                        "Cells written: " + written,
                    ]) + "</div>")
        else:
            card = "<div class='card'>No estimator read-back available (sizing step skipped or failed).</div>"
    else:
        card = "<div class='card'>No estimator read-back available (sizing step skipped or failed).</div>"
    side_items: list[str] = []
    if cr.get("implied_usd_per_ecu"):
        side_items.extend([
            f"ARR {usd(cr.get('arr'))} against {n0(cr.get('commit_ecu_per_day'))} ECU/day is ${cr.get('implied_usd_per_ecu')}/ECU.",
            f"The same {n0(sv.get('ech_list_mo'))} ECU/mo is {usd(sv.get('ech_contract_mo'))} at that rate.",
            "The rate is ARR ÷ (commit ECU/day × 365), applied unrounded and shown rounded.",
            "Serverless is the estimator price and is not discounted. Verify the contract with the AE.",
        ])
    side_items.extend([x for x in (c.get("caveats") or []) + (c.get("notes") or []) if x])
    side = "<h3>Before you quote</h3>" + bullets(side_items)
    foot_src = "Elasticsearch diagnostics joined to K360 billing" if _from_diagnostics(acc) else "K360 cloud-tracker billing + telemetry"
    B.append(section("6. Reconciliation and what to confirm before quoting", grid(card, side) + f"<div class='foot'>Source: {foot_src} · {esc(svl.get('pricing_source'))} · generated {esc(m.get('generated_at'))} · only estimator read-backs are quotable $</div>", new_page=True))
    B.extend(cost_sizing_sections(acc, sizing=False))
    return page(f"{m.get('customer')} — serverless consolidation savings", "".join(B))


# --------------------------------------------------------------------------- chrome
def find_chrome() -> str:
    for cand in (os.environ.get("CHROME_PATH"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium", "/usr/bin/google-chrome", "/usr/bin/chromium"):
        if cand and Path(cand).is_file():
            return cand
    raise FileNotFoundError("Chrome not found — set CHROME_PATH")


def chrome_pdf(html_path: Path, pdf_path: Path, chrome: str | None = None) -> None:
    chrome = chrome or find_chrome()
    tmp = pdf_path.with_suffix(".pdf.part")
    try:
        with tempfile.TemporaryDirectory(prefix="k360-chrome-") as prof:
            cmd = [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run", f"--user-data-dir={prof}", f"--print-to-pdf={tmp}", html_path.resolve().as_uri()]
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        if not tmp.is_file() or tmp.stat().st_size < 1000:
            raise RuntimeError(f"Chrome produced no PDF at {pdf_path}")
        tmp.replace(pdf_path)
    finally:
        tmp.unlink(missing_ok=True)


def export(acc: dict, folder: Path, *, skip_pdf: bool = False, only: str | None = None, consolidation_suffix: str | None = None, consolidation_kind: str = "consolidation") -> dict:
    key = acc["meta"]["run_key"]
    jobs = []
    if only in (None, "account"):
        jobs.append(("account", ACCOUNT_SUFFIX, render_account_html))
    if only in (None, "consolidation") and acc.get("consolidation") and not (acc["consolidation"] or {}).get("error"):
        jobs.append((consolidation_kind, consolidation_suffix or CONSOLIDATION_SUFFIX, render_consolidation_html))
    out: dict = {}
    pending: list[tuple[str, Path, Path]] = []
    for kind, suffix, fn in jobs:
        html_path = folder / f"{key}_{suffix}.html"
        pdf_path = folder / f"{key}_{suffix}.pdf"
        html_path.write_text(fn(acc))
        out[kind] = {"title": f"{key}_{suffix}", "html": str(html_path)}
        if not skip_pdf:
            pending.append((kind, html_path, pdf_path))
    if pending:
        chrome = find_chrome()
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            futs = {pool.submit(chrome_pdf, html_path, pdf_path, chrome): (kind, pdf_path) for kind, html_path, pdf_path in pending}
            for fut in as_completed(futs):
                kind, pdf_path = futs[fut]
                try:
                    fut.result()
                    out[kind]["pdf"] = str(pdf_path)
                except Exception as ex:  # HTML still useful
                    out[kind]["error"] = repr(ex)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-json", required=True)
    ap.add_argument("--out-dir", default=None, help="default: folder of the account JSON")
    ap.add_argument("--skip-pdf", action="store_true", help="HTML only")
    ap.add_argument("--only", choices=("account", "consolidation"), default=None)
    args = ap.parse_args()
    src = Path(args.account_json)
    acc = json.loads(src.read_text())
    res = export(acc, Path(args.out_dir) if args.out_dir else src.parent, skip_pdf=args.skip_pdf, only=args.only)
    acc.setdefault("exports", {}).update(res)
    src.write_text(json.dumps(acc, indent=2))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
