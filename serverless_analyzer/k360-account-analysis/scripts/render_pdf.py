#!/usr/bin/env python3
"""PDF export of both canvases (account analysis §0–10 + consolidation savings §11).

Canvases are live React apps and can't be printed headlessly, so this renders the same
sections, in the same order, from `<key>_account.json` into print-ready HTML (inline SVG
charts, stdlib only) and prints them with headless Chrome:

  <key>_Account_analysis.{html,pdf}
  <key>_full retention_bulk_serverless_consolidation_plan.{html,pdf}                 in full_retention/
  <key>_30d_bulk_serverless_consolidation_plan.{html,pdf}                            in 30d/
  <key>_full retention_compression-applied_serverless_consolidation_plan.{html,pdf} in full_retention/
  <key>_30d_compression-applied_serverless_consolidation_plan.{html,pdf}            in 30d/
  <key>_full retention_k360_serverless_consolidation_plan.{html,pdf}                in full_retention/ (--k360-ingest)

  python render_pdf.py --account-json serverless_analyzer/diag_artifacts/<key>/<key>_account.json [--skip-pdf] [--only account|consolidation]

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
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from analyze import component_summary, display_solution

# Elastic brand: blue, pink, teal, yellow, then ink and danger for extra series.
PALETTE = ["#0B64DD", "#00BFB3", "#F04E98", "#153385", "#F5A700", "#BD271E", "#69707D", "#48A0FF", "#007A73", "#C78A00"]
TONE = {"high": "#BD271E", "medium": "#8A6100", "low": "#0B64DD", "danger": "#BD271E", "warning": "#8A6100", "info": "#0B64DD", "success": "#007A73", "neutral": "#69707D"}

ACCOUNT_SUFFIX = "Account_analysis"
CONSOLIDATION_SUFFIX = "Serverless_consolidation_bulk_savings"
DIAGNOSTICS_SUFFIX = "Serverless_consolidation_from_diagnostics"
K360_BACKUP_SUFFIX = "Serverless_consolidation_from_k360"
FULL_RETENTION_FOLDER = "full_retention"
THIRTY_DAY_FOLDER = "30d"


def pdf_folder_name(kind: str) -> str | None:
    """Retention folder for a consolidation PDF. The account PDF stays in the run folder."""
    if not kind or kind == "account":
        return None
    if kind.endswith("_30d"):
        return THIRTY_DAY_FOLDER
    return FULL_RETENTION_FOLDER


def consolidation_pdf_stem(run_key: str, kind: str) -> str:
    """<customer>_<date>_<full retention|30d>_<bulk|compression-applied>_serverless_consolidation_plan.

    A K360 backup uses k360 in the basis slot so it does not replace the bulk PDF.
    """
    if kind.endswith("_30d"):
        retention = "30d"
        basis_src = kind[: -len("_30d")]
    else:
        retention = "full retention"
        basis_src = kind
    if "compression" in basis_src:
        basis = "compression-applied"
    elif "k360" in basis_src:
        basis = "k360"
    else:
        basis = "bulk"
    return f"{run_key}_{retention}_{basis}_serverless_consolidation_plan"


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


def _reduction(x) -> bool:
    """A positive reduction is a discount. Zero and a higher serverless price get no arrow."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0


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
    return f"<div class='tbl'><table{cls}><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


class Raw(str):
    """Pre-escaped HTML cell."""


def link(url: str | None, label: str) -> Raw:
    return Raw(f"<a href='{html.escape(url)}'>{esc(label)}</a>") if url else Raw(esc(label))


def stat(value, label, tone: str | None = None, tag: str | None = None, *, estimate: bool = False, down: bool = False) -> str:
    color = f" style='color:{TONE[tone]}'" if tone else ""
    tag_html = f"<span class='tag{'' if estimate else ' fact'}'>{esc(tag)}</span>" if tag else ""
    mark = "<span class='down' aria-hidden='true'></span>" if down else ""
    return f"<div class='stat'><div class='v'{color}>{mark}{esc(value)}{tag_html}</div><div class='l'>{esc(label)}</div></div>"


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


def bullets(items: list[str], *, columns: int = 1) -> str:
    lis = "".join(f"<li>{esc(x)}</li>" for x in items if x)
    if not lis:
        return ""
    cls = "notes cols2" if columns == 2 else "notes"
    return f"<ul class='{cls}'>{lis}</ul>"


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


def _rgb(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _tint(color: str, toward: str, amount: float) -> str:
    a, b = _rgb(color), _rgb(toward)
    mixed = tuple(max(0, min(255, round(a[i] + (b[i] - a[i]) * amount))) for i in range(3))
    return "#{:02X}{:02X}{:02X}".format(*mixed)


def _vbar3d(x: float, y: float, w: float, h: float, fill: str, *, dx: float, dy: float, cap: bool) -> str:
    """One column: darker right face, lighter top face, front face at the value."""
    if w <= 0 or h <= 0.4:
        return ""
    light, dark = _tint(fill, "#FFFFFF", 0.45), _tint(fill, "#12141A", 0.34)
    front = f"<polygon points='{x:.1f},{y:.1f} {x + w:.1f},{y:.1f} {x + w:.1f},{y + h:.1f} {x:.1f},{y + h:.1f}' fill='{fill}'/>"
    side = (
        f"<polygon points='{x + w:.1f},{y:.1f} {x + w + dx:.1f},{y - dy:.1f} "
        f"{x + w + dx:.1f},{y + h - dy:.1f} {x + w:.1f},{y + h:.1f}' fill='{dark}'/>"
    )
    top = ""
    if cap:
        top = (
            f"<polygon points='{x:.1f},{y:.1f} {x + dx:.1f},{y - dy:.1f} "
            f"{x + w + dx:.1f},{y - dy:.1f} {x + w:.1f},{y:.1f}' fill='{light}'/>"
        )
    return side + top + front


def _hbar3d(x: float, y: float, w: float, h: float, fill: str, *, dx: float, dy: float) -> str:
    """One horizontal bar with a top face and an end face."""
    if w <= 0 or h <= 0.4:
        return ""
    light, dark = _tint(fill, "#FFFFFF", 0.45), _tint(fill, "#12141A", 0.34)
    front = f"<polygon points='{x:.1f},{y:.1f} {x + w:.1f},{y:.1f} {x + w:.1f},{y + h:.1f} {x:.1f},{y + h:.1f}' fill='{fill}'/>"
    top = (
        f"<polygon points='{x:.1f},{y:.1f} {x + dx:.1f},{y - dy:.1f} "
        f"{x + w + dx:.1f},{y - dy:.1f} {x + w:.1f},{y:.1f}' fill='{light}'/>"
    )
    end = (
        f"<polygon points='{x + w:.1f},{y:.1f} {x + w + dx:.1f},{y - dy:.1f} "
        f"{x + w + dx:.1f},{y + h - dy:.1f} {x + w:.1f},{y + h:.1f}' fill='{dark}'/>"
    )
    return top + end + front


def bar_chart(categories: list[str], series: list[tuple[str, list[float]]], *, stacked: bool = False, width: int = 640, height: int = 230,
              ref: tuple[float, str] | None = None, show_values: bool = False, prefix: str = "", colors: list[str] | None = None, exact_values: bool = False,
              sublabels: list[str] | None = None) -> str:
    colors = colors or PALETTE
    dx, dy = 9.0, 6.0
    pl, pr, pt, pb = 58, 16 + dx, 18 + dy, (64 if sublabels else 48)
    w, h = width - pl - pr, height - pt - pb
    n = max(len(categories), 1)
    totals = [sum(s[1][i] or 0 for s in series) for i in range(n)] if stacked else [max((s[1][i] or 0) for s in series) for i in range(n)]
    vmax = _nice_max(max(totals + [ref[0] if ref else 0]))

    def lab(v: float) -> str:
        return f"{prefix}{n0(v) if exact_values else _short(v)}"

    out = [f"<svg viewBox='0 0 {width} {height}' width='100%' xmlns='http://www.w3.org/2000/svg' font-size='13' font-family='inherit'>"]
    for k in range(5):
        y = pt + h - h * k / 4
        out.append(f"<line x1='{pl}' x2='{pl + w}' y1='{y:.1f}' y2='{y:.1f}' stroke='#D3DAE6'/><text x='{pl - 8}' y='{y + 4:.1f}' text-anchor='end' fill='#69707D'>{lab(vmax * k / 4)}</text>")
    slot = w / n
    groups = 1 if stacked else max(len(series), 1)
    bw = slot * 0.72 / groups
    depth = min(dx, max(4.0, bw * 0.28))
    rise = depth * dy / dx
    for i, cat in enumerate(categories):
        x0 = pl + slot * i + slot * 0.12
        if stacked:
            face = max(slot * 0.58 - depth, 8)
            base = 0.0
            drawn: list[tuple[float, float, float, str, bool]] = []
            for j, (_, vals) in enumerate(series):
                v = float(vals[i] or 0)
                if v <= 0:
                    continue
                y = pt + h - h * (base + v) / vmax
                bh = h * v / vmax
                drawn.append((y, bh, v, colors[j % len(colors)], False))
                base += v
            if drawn:
                y, bh, v, fill, _ = drawn[-1]
                drawn[-1] = (y, bh, v, fill, True)
            for y, bh, _v, fill, cap in drawn:
                out.append(_vbar3d(x0, y, face, bh, fill, dx=depth, dy=rise, cap=cap))
            if show_values and base:
                out.append(f"<text x='{x0 + face / 2:.1f}' y='{pt + h - h * base / vmax - rise - 4:.1f}' text-anchor='middle' fill='#343741' font-weight='700'>{lab(base)}</text>")
            lx = x0 + face / 2
        else:
            face = max(bw - depth - 3, bw * 0.62)
            lx = x0 + (bw * max(groups - 1, 0) + face) / 2
            for j, (_, vals) in enumerate(series):
                v = float(vals[i] or 0)
                bh = h * v / vmax
                x = x0 + bw * j
                y = pt + h - bh
                out.append(_vbar3d(x, y, face, bh, colors[j % len(colors)], dx=depth, dy=rise, cap=True))
                if show_values and v:
                    out.append(f"<text x='{x + face / 2:.1f}' y='{y - rise - 4:.1f}' text-anchor='middle' fill='#343741' font-weight='700'>{lab(v)}</text>")
        anchor = "end" if n > 8 else "middle"
        rot = " transform='rotate(-35 {:.1f} {:.1f})'".format(lx, pt + h + 16) if n > 8 and max((len(c) for c in categories), default=0) > 6 else ""
        out.append(f"<text x='{lx:.1f}' y='{pt + h + 16}' text-anchor='{anchor}' fill='#343741' font-size='13'{rot}>{esc(cat)}</text>")
        if sublabels and i < len(sublabels) and sublabels[i]:
            sy = pt + h + 32
            srot = " transform='rotate(-35 {:.1f} {:.1f})'".format(lx, sy) if rot else ""
            out.append(f"<text x='{lx:.1f}' y='{sy:.1f}' text-anchor='{anchor}' fill='#69707D' font-size='12'{srot}>{esc(sublabels[i])}</text>")
    if ref:
        y = pt + h - h * ref[0] / vmax
        out.append(f"<line x1='{pl}' x2='{pl + w}' y1='{y:.1f}' y2='{y:.1f}' stroke='#69707D' stroke-dasharray='4 3'/><text x='{pl + w}' y='{y - 4:.1f}' text-anchor='end' fill='#343741'>{esc(ref[1])}</text>")
    out.append("</svg>")
    legend = _legend([s[0] for s in series], colors) if len(series) > 1 else ""
    return "".join(out) + legend


def plan_hbar(rows: list[tuple[str, str, float]], *, ref: tuple[float, str] | None = None, width: int = 440) -> str:
    """Horizontal bars for a tall column. Each row is (cut, project count, monthly dollars).

    The dollar amount sits on the bar. The project count is a column on the
    right, with one arrow pointing down beside that column.
    """
    n = max(len(rows), 1)
    dx, dy = 8.0, 5.0
    label_w, count_w, arrow_w = 48, 96, 16
    plot_x = label_w
    plot_w = max(width - plot_x - count_w - arrow_w, 40)
    row_h, bar_h, top = 52, 22, 22
    height = top + n * row_h + 22
    vals = [float(v or 0) for _, _, v in rows]
    vmax = _nice_max(max(vals + ([ref[0]] if ref else []) + [1]))
    out = [f"<svg viewBox='0 0 {width} {height}' width='100%' xmlns='http://www.w3.org/2000/svg' font-family='inherit'>"]
    if ref:
        rx = plot_x + plot_w * min(float(ref[0]), vmax) / vmax
        anchor = "end" if rx > plot_x + plot_w * 0.55 else "start"
        lx = min(rx - 4, plot_x + plot_w - 4) if anchor == "end" else rx + 4
        out.append(
            f"<line x1='{rx:.1f}' x2='{rx:.1f}' y1='16' y2='{top + n * row_h - 8:.1f}' stroke='#69707D' stroke-dasharray='4 3'/>"
            f"<text x='{lx:.1f}' y='13' text-anchor='{anchor}' fill='#343741' font-size='12'>{esc(ref[1])}</text>"
        )
    count_x = plot_x + plot_w + 10
    for i, (title, sub, v) in enumerate(rows):
        mid = top + i * row_h + row_h / 2
        bw = plot_w * float(v or 0) / vmax
        by = mid - bar_h / 2
        out.append(f"<rect x='{plot_x}' y='{by:.1f}' width='{plot_w:.1f}' height='{bar_h}' rx='3' fill='#EEF2F7'/>")
        face = max(bw - dx, 0)
        if bw > 0:
            out.append(_hbar3d(plot_x, by, max(face, 2), bar_h, PALETTE[0], dx=dx, dy=dy))
        label = usd(v)
        text_y = by + bar_h * 0.72
        if face >= 78:
            out.append(
                f"<text x='{plot_x + face - 8:.1f}' y='{text_y:.1f}' text-anchor='end' fill='#ffffff' font-size='12' font-weight='700'>{label}</text>"
            )
        else:
            out.append(
                f"<text x='{plot_x + max(face, 0) + dx + 6:.1f}' y='{text_y:.1f}' text-anchor='start' fill='#343741' font-size='12' font-weight='700'>{label}</text>"
            )
        out.append(f"<text x='4' y='{mid + 4:.1f}' fill='#343741' font-size='13' font-weight='700'>{esc(title)}</text>")
        if sub:
            out.append(f"<text x='{count_x:.1f}' y='{mid + 4:.1f}' fill='#343741' font-size='12' font-weight='600'>{esc(sub)}</text>")
    if n >= 2 and any(sub for _, sub, _ in rows):
        ax = width - 8
        y_top = top + row_h / 2
        y_bot = top + (n - 1) * row_h + row_h / 2
        out.append(
            f"<line x1='{ax:.1f}' y1='{y_top:.1f}' x2='{ax:.1f}' y2='{y_bot - 8:.1f}' stroke='#69707D' stroke-width='1.6'/>"
            f"<polygon points='{ax - 4.5:.1f},{y_bot - 8:.1f} {ax + 4.5:.1f},{y_bot - 8:.1f} {ax:.1f},{y_bot + 1:.1f}' fill='#69707D'/>"
        )
    axis_y = top + n * row_h - 2
    out.append(f"<line x1='{plot_x}' x2='{plot_x + plot_w:.1f}' y1='{axis_y:.1f}' y2='{axis_y:.1f}' stroke='#D3DAE6'/>")
    out.append(f"<text x='{plot_x}' y='{axis_y + 14:.1f}' fill='#69707D' font-size='11'>$0</text>")
    out.append(f"<text x='{plot_x + plot_w:.1f}' y='{axis_y + 14:.1f}' text-anchor='end' fill='#69707D' font-size='11'>{usd(vmax)}</text>")
    out.append("</svg>")
    return "".join(out)


def hbar_chart(items: list[tuple[str, float]], *, width: int = 520, label_w: int = 150, show_pct_of: float | None = None) -> str:
    bh, gap, dx, dy = 18, 12, 6.0, 4.0
    height = len(items) * (bh + gap) + 10
    vmax = _nice_max(max([v for _, v in items] + [1]))
    w = width - label_w - 78 - dx
    out = [f"<svg viewBox='0 0 {width} {height}' width='100%' xmlns='http://www.w3.org/2000/svg' font-size='13' font-family='inherit'>"]
    for i, (lab, v) in enumerate(items):
        y = 8 + i * (bh + gap)
        bw = w * (v or 0) / vmax
        out.append(f"<text x='{label_w - 8}' y='{y + 14}' text-anchor='end' fill='#343741'>{esc(lab)}</text>")
        out.append(_hbar3d(label_w, y, max(bw, 0), bh, PALETTE[0], dx=dx, dy=dy))
        tail = f" ({100 * v / show_pct_of:.1f}%)" if show_pct_of else ""
        out.append(f"<text x='{label_w + bw + dx + 6:.1f}' y='{y + 14}' fill='#343741'>{n0(v)}{tail}</text>")
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
ul.notes.cols2 { columns: 2; column-gap: 22px; font-size: 11px; line-height: 1.28; margin-bottom: 4px; }
ul.notes.cols2 li { break-inside: avoid; margin: 1px 0; }
.sec { margin: 0 0 2px; } .newpage { break-before: page; }
.page-break { break-before: page; }
.stats { display: grid; gap: 12px; margin: 0 0 12px; align-items: stretch; }
.stat { min-width: 0; display: flex; flex-direction: column; justify-content: flex-start; background: #fff; border: 1px solid #D3DAE6; border-top: 3px solid #0B64DD; border-radius: 6px; padding: 8px 10px 10px; break-inside: avoid; }
.stat .l { order: -1; color: #69707D; font-size: 10px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; line-height: 1.3; min-height: 2.6em; }
.stat .v { font-size: 16px; font-weight: 700; line-height: 1.25; color: #153385; margin-top: 2px; }
.stat .down { display: inline-block; width: 0; height: 0; margin: 0 5px 1px 0; border-left: 5px solid transparent; border-right: 5px solid transparent; border-top: 7px solid currentColor; vertical-align: middle; }
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
body.consolidation h3 { margin: 12px 0 8px; break-after: avoid; }
body.consolidation h2 + h3,
body.consolidation .panel > h3:first-child,
body.consolidation .card > h3:first-child,
body.consolidation .grid > div > h3:first-child { margin-top: 0; }
body.consolidation table { margin: 0; }
body.consolidation th, body.consolidation td { padding: 4px 8px; }
body.consolidation .tbl { margin: 0 0 12px; }
body.consolidation .tbl:last-child,
body.consolidation .tbl:has(+ .cap),
body.consolidation .panel .tbl:last-child,
body.consolidation .card .tbl:last-child { margin-bottom: 0; }
body.consolidation .cap { margin: 4px 0 12px; }
body.consolidation .panel,
body.consolidation .card { padding: 10px 12px; }
body.consolidation .grid { gap: 12px; margin: 0 0 12px; }
body.consolidation .grid > div:not(:has(.panel)):not(:has(.card)):not(:has(.grid)) { border: none; padding: 0; background: transparent; }
body.consolidation ul.notes { margin: 8px 0 12px 18px; }
body.consolidation .sec { margin: 0 0 6px; }
body.consolidation svg { break-inside: avoid; }
body.consolidation tr { break-inside: auto; }
body.consolidation { font-size: 14px; line-height: 1.45; }
body.consolidation .masthead { padding: 16px 18px 14px; margin-bottom: 18px; }
body.consolidation .masthead h1 { font-size: 24px; line-height: 1.25; }
body.consolidation .masthead .sub { font-size: 14px; line-height: 1.4; }
body.consolidation h2 { font-size: 18px; margin: 0 0 14px; padding-bottom: 6px; }
body.consolidation h3 { font-size: 15.5px; margin: 16px 0 10px; }
body.consolidation .cap { font-size: 13px; line-height: 1.4; margin: 8px 0 14px; }
body.consolidation ul.notes { font-size: 14px; line-height: 1.45; margin: 12px 0 16px 20px; }
body.consolidation ul.notes li { margin: 4px 0; }
body.consolidation ul.notes.cols2 { font-size: 13.5px; line-height: 1.45; column-gap: 28px; }
body.consolidation th { font-size: 12px; }
body.consolidation td { font-size: 13.5px; line-height: 1.4; }
body.consolidation th, body.consolidation td { padding: 7px 10px; }
body.consolidation .tbl { margin: 0 0 16px; }
body.consolidation .stat { padding: 12px 14px 14px; }
body.consolidation .stat .l { font-size: 11.5px; letter-spacing: .03em; }
body.consolidation .stat .v { font-size: 20px; }
body.consolidation .stats { gap: 14px; margin-bottom: 16px; }
body.consolidation .panel,
body.consolidation .card { padding: 14px 16px; }
body.consolidation .grid { gap: 18px; margin: 0 0 16px; }
body.consolidation .legend { font-size: 13px; gap: 6px 16px; margin-top: 8px; }
body.consolidation .callout { padding: 10px 14px; margin-bottom: 14px; }
body.consolidation .foot { font-size: 12.5px; margin-top: 18px; }
body.consolidation .sec { margin: 0 0 18px; }
body.consolidation .keep { break-inside: avoid; }
"""


def page(title: str, body: str, *, kind: str = "") -> str:
    cls = f" class='{kind}'" if kind else ""
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(title)}</title><style>{CSS}</style></head><body{cls}>{body}</body></html>"


def consolidation_report_exists(acc: dict) -> bool:
    """Cost (§9) and sizing (§10) live on the consolidation PDF when that report ran."""
    c = acc.get("consolidation") or {}
    return bool((acc.get("sizing") or {}).get("triggered") and c and not c.get("error"))


def cost_sizing_sections(acc: dict, *, sizing: bool = True, new_page: bool = True, numbered: bool = False) -> list[str]:
    m = acc.get("meta") or {}
    an = acc.get("analysis") or {}
    sz = acc.get("sizing") or {}
    est = sz.get("estimators") or {}
    del numbered
    cost_title = "Cost-savings analysis"
    sizing_title = "Serverless sizing"
    out = [section(cost_title, recs(an.get("cost") or []), new_page=new_page)]
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
        out.append(section(sizing_title, body, new_page=new_page))
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
    for r in items:
        sev = (r.get("severity") or "low").lower()
        docs = doc_links(r)
        out.append(
            f"<div class='rec panel' style='border-left-color:{TONE.get(sev, '#69707D')}'>"
            f"<div><span class='t'>{esc(r.get('title'))}</span><span class='sev' style='color:{TONE.get(sev, '#69707D')}'>{esc(sev.upper())}</span></div>"
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
    B.append(section("Platform, cloud and instance footprint", "".join(b), new_page=True))

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
    B.append(section("Deployments, stack versions and ECU", "".join(b), new_page=True))

    # 3 + 4 share one page
    ser = bd.get("series") or {}
    sm = ser.get("months") or []
    burn = panel("<h3>Remaining ECU over time</h3>" + (
        line_chart(
            [_mlab(x) for x in sm],
            series=[("Implied balance", ser.get("implied_balance") or []), ("Commit-pace forecast", ser.get("commit_pace_balance") or [])],
            height=230, show_values=False, colors=[PALETTE[0], PALETTE[2]],
        ) + caption(ser.get("note") or "") if sm else "<div class='cap'>No balance series.</div>"
    ))
    flags = "".join(callout(f["flag"], esc(f.get("detail")), f.get("severity") or "info") for f in bd.get("flags") or []) or "<div class='cap'>No burndown flags.</div>"
    deps = panel(
        "<h3>Active deployments</h3>"
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
        "Burndown and deployments over time",
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
    B.append(section("Data storage", "".join(body46), new_page=True))
    B.append(section("ECU by solution", "".join(body6), new_page=True))
    B.append(section("ML, vectors and AI", "".join(body7), new_page=True))
    B.append(section("Current state analysis and recommendations", rec_body, new_page=True))
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
        if e.get("quote_lookback_days"):
            ret_math = (
                f"The GB/day written to the estimator is diagnostic bulk × {e.get('quote_usage_factor')}, "
                f"the {int(e['quote_lookback_days'])}-day pace divided by the complete-month pace. "
                "The sensitivity table still shows stored GB ÷ retention and does not reprice the estimate."
            )
        else:
            ret_math = "The GB/day written to the estimator is the measured bulk rate. The sensitivity table still shows stored GB ÷ retention and does not reprice the estimate."
    else:
        size_src = notes[0] if notes else "account storage block"
        size_math = "K360 does not provide a raw ingest total. Raw GB/day = stored GB ÷ (retention months × 30.416). This backup can sit far below measured bulk. Deployments with no row are absent from this total."
        ret_src = "run assumption (default 3 months), not measured from ILM or data streams"
        ret_math = "Changes the implied GB/day shown in the sensitivity table. The estimator was not re-run, so the quoted dollars stay put."
    if e.get("quote_lookback_days"):
        ecu_math = (
            f"{int(e['quote_lookback_days'])}-day lookback is {n0(sv.get('ech_list_mo'))} ECU/mo. "
            f"Complete-month average is {n0(e.get('monthly_avg_complete_months'))} ECU/mo. "
            f"Partial months left out of that average: {', '.join(partial) if partial else 'none'}."
        )
    else:
        ecu_math = f"Complete-month average is {n0(sv.get('ech_list_mo'))} ECU/mo. Partial months left out: {', '.join(partial) if partial else 'none'}."
    if sv.get("ech_contract_mo") is None:
        ecu_math += " List price is $1 per ECU."
    rows = [
        ["Lookback ECU", f"{n0(e.get('total_lookback'))} ECU, {rng.get('start') or '—'} → {rng.get('end') or '—'} ({e.get('lookback_days')} d)", "cloud-tracker-billing", ecu_math],
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
        "The component breakdown and the sizing inputs are in the estimate above.",
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
    return "<h3>Data used, and how the headline is calculated</h3>" + body


def serverless_component_slide(acc: dict, *, new_page: bool = True, bare: bool = False) -> str:
    """Component dollars and the sizing inputs. Bare HTML sits beside the deployment-count chart."""
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
            names = [str(name) for name in (diag_cov.get("fallback_names") or []) if name]
            named = f" ({', '.join(names[:8])})" if names else ""
            roster_notes.append(f"{n0(n)} active {noun} {verb} no diagnostic bundle{named} and {add} no ingest.")
        if c.get("retention_set_days") == 30 and (cons.get("search") or {}).get("retention_months") is not None:
            roster_notes.append(
                f"Search disk is the measured store scaled from {cons['search'].get('retention_months')} months to 1 month. "
                "Observability and Security on this page are written as 1 month."
            )
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
    sec_status = str((est.get("security") or {}).get("status") or "")
    if "no security workload" in sec_status:
        notes.append("Security was not priced. There is no security workload.")
    elif "retention was not measured" in sec_status:
        notes.append("Security was not priced. It has bulk and no measured retention.")
    body += bullets(notes)
    folder = (sz.get("drive_folder") or {}).get("url")
    if folder:
        body += f"<p style='margin:4px 0 0'>{link(folder, 'Drive folder')}</p>"
    if bare:
        return body
    return section("Estimated monthly serverless total, by component", body, new_page=new_page)


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
        from skill_paths import find_skill, scripts_dir

        diag_scripts = scripts_dir(find_skill("diag-serverless-consolidation", Path(__file__).resolve().parent))
        if str(diag_scripts) not in sys.path:
            sys.path.insert(0, str(diag_scripts))
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

    accounted = c.get("ingest_basis") == "compressioncalc_accounted_for"
    headers = ["Deployment", "Accounted GB/day" if accounted else "Raw GB/day", "Metered GB/day", "Metered rate"]
    align = ["left", "right", "right", "right"]
    mid = (len(rates) + 1) // 2
    left, right = rates[:mid], rates[mid:]
    if accounted:
        rate_lead = "The GB/day in this table is compressioncalc_accounted_for, bulk multiplied by 0.60. Metered GB/day is that figure × 1.595. Logs are 90% and metrics are 10%. Each stream walks the Observability ingest bands on its own. The rate is that deployment's blended dollars per metered GB when it is its own project."
    else:
        rate_lead = "Metered GB/day is raw bulk × 1.595. Logs are 90% and metrics are 10%. Each stream walks the Observability ingest bands on its own. The rate is that deployment's blended dollars per metered GB when it is its own project."
    body = bullets([
        rate_lead,
        "The first 50 metered GB/day are $0.50 per GB. The bands then step to $0.325, $0.225, $0.15, $0.105, $0.10, $0.095, and $0.0925. A deployment inside the first band on both streams is $0.500, and that row is marked in red. A deployment with no bulk has no rate.",
        "This rate is not the monthly quote. The monthly quote remains the Observability estimator read-back, which also includes retention and support.",
    ])
    body += grid(
        table(headers, [row(r) for r in left], align, ["danger" if at_top_rate(r) else None for r in left]),
        table(headers, [row(r) for r in right], align, ["danger" if at_top_rate(r) else None for r in right]),
        ratio="1fr 1fr",
    )
    return "<h3>Deployment consolidation analysis across metered ingest rates</h3>" + body


def _integrity_block(acc: dict) -> str:
    """Check, score, and pass for the Appendix. Not a serverless bill."""
    block = (acc.get("consolidation") or {}).get("integrity") or {}
    checks = block.get("checks") or []
    if not checks:
        return ""
    score = block.get("score")
    passed = block.get("passed")
    if score is None:
        headline = "Quote integrity was not scored."
    else:
        flagged = [row.get("check") for row in checks if row.get("score") is not None and row.get("passed") is False]
        mark = "Pass" if passed else "Fail"
        headline = f"Quote integrity {float(score):.3f} — {mark}."
        if passed and flagged:
            headline += " Flagged: " + ", ".join(str(name) for name in flagged) + "."
    rows = []
    tones = []
    for row in checks:
        value = row.get("score")
        flag = row.get("passed")
        if value is None:
            shown, word, tone = "—", "n/a", None
        else:
            shown = f"{float(value):.3f}"
            word = "Pass" if flag else "Fail"
            tone = "success" if flag else "danger"
        rows.append([row.get("check") or "", shown, word, row.get("detail") or ""])
        tones.append(tone)
    return (
        "<h3>Quote integrity</h3>"
        + bullets([
            headline,
            "The score is the mean of the checks that apply. A check that does not apply is n/a and stays out of the mean.",
            "A score of 0.90 or higher passes. A check that misses its own bar stays flagged. A G26 gap of 0.51 scores 0 on that component and still passes that component. E73 must be 0.",
            "This scores whether the written inputs, the sheet echo, and the published totals agree. It is not a serverless bill.",
        ])
        + table(["Check", "Score", "Pass", "Measured"], rows, ["left", "right", "left", "left"], tones)
    )


def _search_estimated(acc: dict) -> bool:
    """True when Search itself was priced. A skipped Search estimator is not an estimate."""
    c = acc.get("consolidation") or {}
    search = ((c.get("serverless") or {}).get("by_solution") or {}).get("search") or {}
    est = ((acc.get("sizing") or {}).get("estimators") or {}).get("search") or {}
    status = str(est.get("status") or "")
    if status.startswith("skipped") or "no search workload" in status.lower():
        return False
    if search.get("regional_profiles"):
        return True
    if "quotable" in str(search.get("pricing") or ""):
        return True
    return bool(est.get("profiles"))


def _search_profile_fallback(acc: dict) -> str:
    """Estimator profiles when the search-power block was not built. Empty when Search was not estimated."""
    if not _search_estimated(acc):
        return ""
    c = acc.get("consolidation") or {}
    if (c.get("search_power") or {}).get("profiles"):
        return ""
    profiles = (
        (((c.get("serverless") or {}).get("by_solution") or {}).get("search") or {}).get("regional_profiles")
        or (((acc.get("sizing") or {}).get("estimators") or {}).get("search") or {}).get("profiles")
        or []
    )
    if not profiles:
        return ""
    regional_profiles = bool((((c.get("serverless") or {}).get("by_solution") or {}).get("search") or {}).get("regional_profiles"))
    eis_profile = float((((c.get("serverless") or {}).get("by_solution") or {}).get("search") or {}).get("eis_mo") or 0)
    headers = ["Profile", "$ / mo", "Provisioned VCU", "Idle VCU", "Search $", "Storage $", "Support $"]
    if eis_profile:
        headers.insert(6, "EIS $")
    rows = []
    for p in profiles:
        row = [p.get("Search Power Setting"), usd(p.get("Total $/month")), p.get("Provisioned Search VCU/h"), p.get("Idle Search VCU/h"), usd(p.get("Search $/month")), usd(p.get("Storage $/month")), usd(p.get("Support $/month"))]
        if eis_profile:
            row.insert(6, usd(eis_profile))
        rows.append(row)
    prof_table = table(
        headers,
        rows,
        ["left"] + ["right"] * (len(headers) - 1),
        tones=["success" if str(p.get("Search Power Setting")) == "on-demand" else None for p in profiles],
    )
    return (
        "<h3>Search Serverless price by profile</h3>"
        + bar_chart(
            [str(p.get("Search Power Setting") or "profile") for p in profiles],
            [("$ / month", [float(p.get("Total $/month") or 0) for p in profiles])],
            width=640, height=200, show_values=True, prefix="$",
        )
        + prof_table
        + bullets([
            "On-demand is the sum of the regional Search projects used in this report. Performant and high availability keep more search capacity ready, so they cost more for the same GB." if regional_profiles else "On-demand is the Search Serverless price used in this report. Performant and high availability keep more search capacity ready, so they cost more for the same GB.",
            "Y: dollars per month. X: Search Serverless power setting.",
            "Ingest is $0 on every profile when average document size is missing.",
            (f"EIS is {usd(eis_profile)} on every profile, and it is inside $ / mo. Search $ + storage $ + EIS $ + support $ = $ / mo." if eis_profile else ""),
        ])
    )


def search_power_section(acc: dict) -> str:
    """Appendix A: inputs, metered rates, search power, then the quote integrity score."""
    profiles = ((acc.get("consolidation") or {}).get("search_power") or {}).get("profiles") or []
    if not _search_estimated(acc):
        profiles = []
    parts = [part for part in (consolidation_inputs_html(acc), metered_rate_section(acc), _search_profile_fallback(acc)) if part]
    if profiles:
        order = {"on-demand": 0, "performant": 1, "high availability": 2}
        profiles = sorted(profiles, key=lambda p: order.get(str(p.get("Search Power Setting") or "").lower(), 9))
        labels = [str(p.get("Search Power Setting") or "") for p in profiles]
        monthly = [float(p.get("Total $/month") or 0) for p in profiles]
        stored = ((acc.get("consolidation") or {}).get("basis") or {}).get("stored_gb")
        stored_bit = f"{float(stored):,.0f} GB" if stored else "the sized stored GB"
        body = "<h3>Monthly search-power estimate</h3>" + bar_chart(
            labels,
            [("Search power, $ / month", monthly)],
            width=700, height=230, show_values=True, prefix="$", exact_values=True,
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
            "On-demand, performant, and high availability are the three search-power settings. High availability is the highly-performant option: it keeps the most search capacity ready.",
            f"The bars use {stored_bit}, the measured query rate, and Premium support. They are not added to the Observability ingest quote.",
            "Y: dollars per month. X: search power.",
        ])
        parts.append(body)
    integrity = _integrity_block(acc)
    if integrity:
        parts.append(integrity)
    if not parts:
        return ""
    return section("Appendix A", "".join(parts), new_page=True)


_INTEGRITY_FORMULA = {
    "Sheet echo": "Mean of the G26 component and the E73 component, for each solution that has a workload and a sheet. G26 component = 1 − min(|G26 − written bulk| / 0.51, 1). A blank G26 scores 0. E73 scores 1 when the parsed amount is 0, and 0 when it is blank or the amount is above 0.005.",
    "Bundle coverage": "Matched deployments ÷ active deployments. When the active count is absent, the denominator is matched bundles plus bundles that fell back.",
    "Measured retention": "1 − (bulk with no measured age ÷ Σ bulk).",
    "Direct read-back": "Solutions on their own read-back ÷ solutions with a workload. One region counts. A store split does not.",
    "Line agreement": "1 − min(|sum of quote lines − headline| / headline, 1). Both $0 scores 1.",
    "Year agreement": "For each quote line, 1 − min(|steady annual − 12 × unrounded monthly| / |steady annual|, 1), then the mean. Both sides are rounded to cents first.",
}


def _formula_num(x, places: int = 3) -> str | None:
    if x is None or x == "":
        return None
    try:
        value = float(x)
    except (TypeError, ValueError):
        return None
    if abs(value - round(value)) < 1e-9:
        return f"{value:,.0f}"
    text = f"{value:,.{places}f}".rstrip("0").rstrip(".")
    return text


def _formula_money(x) -> str | None:
    if x is None or x == "":
        return None
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return None


def _keep(row: list | None) -> list | None:
    if not row or row[-1] is None:
        return None
    return row


def _solution_measured(acc: dict) -> list[tuple[str, dict, dict]]:
    """Solutions that have a store, a bulk rate, or a written month on this run."""
    cons = (acc.get("sizing") or {}).get("consolidated") or {}
    priced = ((acc.get("consolidation") or {}).get("serverless") or {}).get("by_solution") or {}
    found = []
    for name in ("search", "observability", "security"):
        blk = cons.get(name) or {}
        if not isinstance(blk, dict):
            continue
        stored = float(blk.get("stored_gb") or 0)
        daily = float(blk.get("daily_raw_gb") or 0)
        if stored <= 0 and daily == 0 and blk.get("retention_months") is None:
            continue
        found.append((name, blk, priced.get(name) or {}))
    return found


def _compression_on(acc: dict) -> bool:
    c = acc.get("consolidation") or {}
    sz = acc.get("sizing") or {}
    return c.get("ingest_basis") == "compressioncalc_accounted_for" or sz.get("ingest_basis") == "compressioncalc_accounted_for"


def _quote_window_rows(acc: dict) -> list[list]:
    e = acc.get("ecu") or {}
    c = acc.get("consolidation") or {}
    b = c.get("basis") or {}
    sv = c.get("savings") or {}
    cr = c.get("contract_rate") or {}
    rows: list[list] = []
    days = e.get("quote_lookback_days")
    ech = b.get("ech_monthly_ecu")
    if ech is None:
        ech = e.get("quote_monthly_ecu")
    if days and _formula_num(ech, 2):
        daily = _formula_num(e.get("quote_daily_avg"), 2)
        inputs = f"{int(days)} billed days"
        if daily:
            inputs += f", daily average {daily} ECU"
        rows.append([
            "ECH ECU/month",
            "Billed ECU over the quote window ÷ days × 30.416.",
            inputs,
            _formula_num(ech, 2),
        ])
    factor = e.get("quote_usage_factor")
    if factor is not None:
        complete = _formula_num(e.get("monthly_avg_complete_months"), 2)
        inputs = f"pace factor {_formula_num(factor, 6)}"
        if complete:
            inputs += f". Complete-month average {complete} ECU/month"
        rows.append([
            "Pace factor",
            "Quote-window daily average ÷ complete-month daily average.",
            inputs,
            _formula_num(factor, 6),
        ])
    by_sol = (c.get("serverless") or {}).get("by_solution") or {}
    for name, quote in by_sol.items():
        if "quotable" not in str((quote or {}).get("pricing") or ""):
            continue
        if name == "observability":
            formula = "Observability monthly is the D32 read-back. Quoted only when G26 is within 0.51 GB/day of the written bulk."
        elif name == "security":
            formula = "Security monthly is the E74 read-back. Quoted only when parsed E73 is 0."
        elif name == "search":
            formula = "Search monthly is the es3 on-demand read-back."
        else:
            formula = "Estimator read-back."
        rows.append([f"{name.title()} monthly", formula, "Sheet read-back. Not recomputed here.", _formula_money(quote.get("total_mo"))])
    if cr.get("implied_usd_per_ecu") is not None:
        rows.append([
            "Contract rate",
            "ARR ÷ (commit ECU/day × 365), applied unrounded.",
            f"ARR {_formula_money(cr.get('arr'))} · commit {_formula_num(cr.get('commit_ecu_per_day'), 2)} ECU/day",
            f"${cr.get('implied_usd_per_ecu')}/ECU",
        ])
    if sv.get("ech_contract_mo") is not None:
        rows.append([
            "ECH $/month",
            "ECU/month × the contract rate.",
            f"{_formula_num(sv.get('ech_list_mo'), 2)} ECU/month × ${cr.get('implied_usd_per_ecu')}/ECU",
            _formula_money(sv.get("ech_contract_mo")),
        ])
        if sv.get("serverless_mo") is not None:
            rows.append([
                "Savings $/month",
                "ECH at the contract rate − the serverless read-back.",
                f"{_formula_money(sv.get('ech_contract_mo'))} − {_formula_money(sv.get('serverless_mo'))}",
                _formula_money(sv.get("cost_mo")),
            ])
            rows.append([
                "Savings %",
                "Savings $/month ÷ ECH at the contract rate.",
                f"{_formula_money(sv.get('cost_mo'))} ÷ {_formula_money(sv.get('ech_contract_mo'))}",
                None if sv.get("cost_pct") is None else f"{sv.get('cost_pct')}%",
            ])
            rows.append([
                "Savings $/year",
                "Savings $/month × 12.",
                _formula_money(sv.get("cost_mo")),
                _formula_money(sv.get("cost_yr")),
            ])
    elif sv.get("serverless_mo") is not None and sv.get("list_mo") is not None:
        rows.append([
            "Savings $/month",
            "ECH list ECU/month − the serverless read-back, at $1 per ECU.",
            f"{_formula_num(sv.get('ech_list_mo'), 2)} ECU/month − {_formula_money(sv.get('serverless_mo'))}",
            _formula_money(sv.get("list_mo")),
        ])
        rows.append([
            "Savings %",
            "Savings $/month ÷ ECH list ECU/month.",
            f"{_formula_money(sv.get('list_mo'))} ÷ {_formula_num(sv.get('ech_list_mo'), 2)} ECU/month",
            None if sv.get("list_pct") is None else f"{sv.get('list_pct')}%",
        ])
        rows.append([
            "Savings $/year",
            "Savings $/month × 12.",
            _formula_money(sv.get("list_mo")),
            _formula_money(sv.get("list_yr")),
        ])
    return [row for row in rows if _keep(row)]


def _bulk_rows(acc: dict) -> list[list]:
    c = acc.get("consolidation") or {}
    factor = (acc.get("ecu") or {}).get("quote_usage_factor")
    thirty = c.get("retention_set_days") == 30
    rows: list[list] = []
    for name, blk, _priced in _solution_measured(acc):
        label = name.title()
        daily = blk.get("daily_raw_gb")
        stored = blk.get("stored_gb")
        if name == "search" and float(stored or 0) > 0:
            rows.append([
                "Search disk",
                "Σ local primary store. Searchable snapshots stay out. This is not bulk GB/day.",
                "Diagnostic local store",
                f"{_formula_num(stored, 3)} GB",
            ])
        elif name != "search" and _compression_on(acc):
            pass
        elif daily not in (None, "") and float(daily or 0) != 0:
            if factor is not None:
                formula = (
                    "Σ write-index index.primaries.bulk.est_bytes_per_day ÷ 1e9, then × the pace factor. "
                    "est_bytes_per_day = primaries.bulk.total_size_in_bytes × 86,400 ÷ write_phase_sec, truncated to a whole byte."
                )
                inputs = (
                    f"pace factor {_formula_num(factor, 6)}. The result is the GB/day written after that factor. "
                    "write_phase_sec is creation until rollover, in whole seconds, or the index age while the ILM action is rollover. "
                    "A missing or zero write phase is 0. The indexing rate uses primaries.store.size_in_bytes and is not this figure."
                )
            else:
                formula = (
                    "Σ write-index index.primaries.bulk.est_bytes_per_day ÷ 1e9. "
                    "est_bytes_per_day = primaries.bulk.total_size_in_bytes × 86,400 ÷ write_phase_sec, truncated to a whole byte."
                )
                inputs = (
                    "Write indices only: the last index of a data stream, or an alias marked is_write_index. "
                    "write_phase_sec is creation until rollover, in whole seconds, or the index age while the ILM action is rollover. "
                    "A missing or zero write phase is 0. The indexing rate uses primaries.store.size_in_bytes and is not this figure."
                )
            rows.append([f"{label} bulk GB/day", formula, inputs, f"{_formula_num(daily, 3)} GB/day"])
        measured = blk.get("retention_months_measured")
        written = blk.get("retention_months")
        if measured is not None:
            rows.append([
                f"{label} measured months",
                "Σ (bulk × retention_days) ÷ Σ bulk ÷ 30.416. Only a retention age above 0 enters the weight.",
                "Size-weighted ILM delete age, else data-stream retention, else the oldest index.",
                f"{_formula_num(measured, 2)} months",
            ])
        if written is not None:
            if thirty and name != "search":
                formula = "The 30-day sheet writes 1 month. Bulk does not change."
            else:
                formula = "Snap a remainder under 0.001 month down, round up, then clamp to 1–36."
            rows.append([f"{label} month written", formula, "The estimator prices retention only on the integers 1–36.", f"{_formula_num(written, 2)} months"])
        if blk.get("retention_unmeasured"):
            rows.append([
                f"{label} retention",
                "A solution with bulk and no measured age is not written.",
                "No retention_days above 0 on the rows that have bulk.",
                "not written",
            ])
    return [row for row in rows if _keep(row)]


def _scale_rows(acc: dict) -> list[list]:
    c = acc.get("consolidation") or {}
    sz = acc.get("sizing") or {}
    est = (sz.get("estimators") or {}).get("search") or {}
    search = ((sz.get("consolidated") or {}).get("search") or {})
    rows: list[list] = []
    compressed = _compression_on(acc)
    thirty = c.get("retention_set_days") == 30
    if compressed:
        ratio = c.get("compression_ratio")
        if ratio is None:
            ratio = 0.60
        bulk = c.get("bulk_gb_day")
        accounted = c.get("compressioncalc_accounted_for")
        inputs = (
            "The priced ratio is 0.60 whenever a solution has a workload. "
            "It is not esdiag bulk.compression_ratio (bulk bytes ÷ store bytes) or bulk.storage_ratio (store bytes ÷ bulk bytes). "
            "The indexing rate uses primaries.store.size_in_bytes and is not this figure."
        )
        if _formula_num(bulk, 3):
            inputs = f"Bulk {_formula_num(bulk, 3)} GB/day. " + inputs
        if accounted not in (None, ""):
            label, result = "Compression-applied GB/day", f"{_formula_num(accounted, 3)} GB/day"
        else:
            label, result = "Compression ratio", _formula_num(ratio, 2)
        rows.append([
            label,
            "compressioncalc_accounted_for = bulk GB/day × 0.60. That bulk is Σ write-index primaries.bulk.est_bytes_per_day ÷ 1e9, and est_bytes_per_day = primaries.bulk.total_size_in_bytes × 86,400 ÷ write_phase_sec. On-disk bytes do not change the 0.60.",
            inputs,
            result,
        ])
        for name, blk, _priced in _solution_measured(acc):
            if name == "search":
                continue
            daily = blk.get("daily_raw_gb")
            if daily in (None, "") or float(daily or 0) == 0:
                continue
            cells = ((sz.get("estimators") or {}).get(name) or {}).get("cells") or {}
            if name == "observability":
                bits = []
                if cells.get("G21") is not None:
                    bits.append(f"logs G21 {_formula_num(cells.get('G21'), 2)} GB/day = 90% of the written GB/day")
                if cells.get("G23") is not None:
                    bits.append(f"metrics G23 {_formula_num(cells.get('G23'), 2)} GB/day = 10%")
                inputs = ". ".join(bits) or "Logs are 90% and metrics are 10% of the written GB/day."
                formula = "Observability writes the accounted GB/day. Logs = 0.90 × that rate. Metrics = 0.10 × that rate."
            else:
                g4 = cells.get("G4")
                inputs = f"G4 {_formula_num(g4, 2)} GB/day" if g4 is not None else "Security takes all of the accounted GB/day on G4."
                formula = "Security writes the accounted GB/day. G4 is all of it."
            rows.append([f"{name.title()} accounted GB/day", formula, inputs, f"{_formula_num(daily, 3)} GB/day"])
    scale = est.get("store_scale")
    written = search.get("retention_months")
    if scale is not None and (float(search.get("stored_gb") or 0) > 0 or written is not None):
        parts = ["Full-retention bulk leaves the measured store unchanged."]
        if thirty:
            parts.append("A 30-day sheet multiplies disk by 1 ÷ written months.")
        if compressed:
            parts.append("A compression-applied sheet multiplies disk by 0.60.")
        parts.append("Org store uses the same scale, so the ingest share stays put. Query rate does not scale.")
        inputs = f"Written month {_formula_num(written, 2)}." if written is not None else "Measured store."
        if thirty:
            inputs += " Forced month 1."
        rows.append([
            "Search disk scale",
            " ".join(parts),
            inputs,
            _formula_num(scale, 4),
        ])
    return [row for row in rows if _keep(row)]


def _workload_rows(acc: dict) -> list[list]:
    if not _search_estimated(acc):
        return []
    wl = acc.get("workload") or {}
    search = wl.get("search") or {}
    peak = search.get("peak") or {}
    nonpeak = search.get("nonpeak") or {}
    ingest = wl.get("ingest") or {}
    if peak.get("qps") is None and nonpeak.get("qps") is None and ingest.get("docs_per_sec") is None:
        return []
    rows: list[list] = []
    window = wl.get("window_days")
    indices = wl.get("indices")
    window_bit = []
    if window is not None:
        window_bit.append(f"{window} days")
    if indices is not None:
        window_bit.append(f"{indices} indices")
    where = ", ".join(window_bit) or "cloud-tracker-index_telemetry"
    if peak.get("qps") is not None:
        rows.append([
            "Peak QPS",
            "A day QPS is Σ Δsearch_total ÷ 86,400. Peak QPS is the highest cleaned day. A day above max(4 × the median, median + 50) is dropped unless that drops more than half the days.",
            where + ". Day averages, not peak-second QPS.",
            f"{_formula_num(peak.get('qps'), 2)} QPS",
        ])
    if peak.get("pct_hours_mo") is not None:
        rows.append([
            "Peak % of the month",
            "100 × (days with QPS at or above the 75th percentile) ÷ days. The percentile index is round((p ÷ 100) × (len − 1)). Hours = that percent ÷ 100 × 730.",
            where,
            f"{_formula_num(peak.get('pct_hours_mo'), 1)}%",
        ])
    if nonpeak.get("qps") is not None:
        rows.append([
            "Non-peak QPS",
            "The cleaned day closest to the median daily QPS.",
            where,
            f"{_formula_num(nonpeak.get('qps'), 2)} QPS",
        ])
    if nonpeak.get("pct_hours_mo") is not None:
        rows.append([
            "Non-peak % of the month",
            "100 × (days − peak days − idle days) ÷ days. Idle days are counted only when some day is at or below 1 QPS, and then the days at or below the 10th percentile. Hours = that percent ÷ 100 × 730.",
            where,
            f"{_formula_num(nonpeak.get('pct_hours_mo'), 1)}%",
        ])
    if search.get("idle_pct_hours_mo") is not None:
        rows.append([
            "Idle % of the month",
            "100 × idle days ÷ days. Hours = that percent ÷ 100 × 730.",
            "Idle only when a day at or below 1 QPS exists.",
            f"{_formula_num(search.get('idle_pct_hours_mo'), 1)}%",
        ])
    inputs = ((acc.get("sizing") or {}).get("estimators") or {}).get("search") or {}
    written = (inputs.get("inputs") or {}).get("search_workloads") or []
    labels = ["Peak", "Non-peak"]
    for band, row in zip(labels, written):
        hours = row.get("active_hours_per_month") if isinstance(row, dict) else None
        if hours is None:
            continue
        rows.append([
            f"{band} hours",
            "Hours = percent ÷ 100 × 730. Shares that sum past 730 are scaled down to the month.",
            "730-hour month.",
            f"{_formula_num(hours, 2)} h",
        ])
    regions = (((acc.get("consolidation") or {}).get("serverless") or {}).get("by_solution") or {}).get("search") or {}
    region_rows = [r for r in (regions.get("by_region") or []) if isinstance(r, dict)]
    if len(region_rows) > 1 and peak.get("qps") is not None:
        bits = [f"{r.get('region')} {_formula_num(r.get('stored_gb'), 1)} GB" for r in region_rows]
        rows.append([
            "Regional query rate",
            "Peak QPS and non-peak QPS × that region's stored GB ÷ search stored GB. Hour percentages and query time are not scaled.",
            f"Org peak {_formula_num(peak.get('qps'), 2)} QPS. " + "; ".join(bits),
            "applied on each regional sheet",
        ])
    if ingest.get("docs_per_sec") is not None:
        rows.append([
            "Ingest docs/s",
            "Median daily Σ Δdocs_count ÷ 86,400. On the Search sheet this rate is × search store ÷ org store. The quote-window pace factor scales it first when that factor is set.",
            where,
            f"{_formula_num(ingest.get('docs_per_sec'), 3)} docs/s",
        ])
    if ingest.get("active_hours_mo") is not None:
        rows.append([
            "Ingest active hours",
            "(days with Δdocs above 1,000 ÷ days) × 730.",
            where,
            f"{_formula_num(ingest.get('active_hours_mo'), 1)} h/month",
        ])
    return [row for row in rows if _keep(row)]


def _metered_rows(acc: dict) -> list[list]:
    rates = list((acc.get("consolidation") or {}).get("metered_rates") or [])
    if not rates:
        return []
    priced = [r for r in rates if r.get("usd_per_gb") is not None]
    half = [r for r in priced if abs(float(r.get("usd_per_gb") or 0) - 0.5) < 1e-6]
    return [[
        "Metered ingest rate",
        "Metered GB/day = bulk × 1.595. Logs = 0.90 × metered. Metrics = 0.10 × metered. Rate = (tier(logs) + tier(metrics)) ÷ metered GB/day. Bands per metered GB: 0–50 at $0.50, then $0.325, $0.225, $0.15, $0.105, $0.10, $0.095, and $0.0925.",
        f"{len(priced)} deployments with a rate. {len(half)} sit at $0.50 because both streams are inside the first 50 GB/day.",
        "not the monthly quote",
    ]]


def _plan_n(plan: list) -> int | None:
    """Active Observability deployments. The 0% cut keeps one project per deployment."""
    for row in plan:
        if str(row.get("label") or "") != "0%":
            continue
        try:
            return int(row.get("projects"))
        except (TypeError, ValueError):
            return None
    return None


def _cut_pct(label: str) -> int | None:
    text = str(label or "").strip().rstrip("%")
    try:
        return int(text)
    except ValueError:
        return None


def _plain_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _plan_count_line(n: int, pct: int) -> str:
    """The project-count arithmetic for one cut, with N filled in."""
    if pct <= 0:
        return f"N = {n}. 0% keeps every deployment, so the count stays {n}."
    if pct >= 100:
        return f"N = {n}. N × 0 = 0, so this cut does not use round. It is held at 1 project."
    share = (100 - pct) / 100
    product = n * share
    kept = int(round(product))
    line = f"N = {n}. {n} × {share:.2f} = {_plain_number(product)}, which rounds to {kept}."
    if abs(product - int(product) - 0.5) < 1e-9:
        line += f" Half-to-even sends .5 to the even integer, so the count is {kept}."
    return line


def _plan_dollar_line(acc: dict, label: str, monthly) -> str:
    """Where that cut's dollar came from: the sheets in the cut, or the headline."""
    bars = ((acc.get("sizing") or {}).get("deployment_plan") or {}).get("bars") or []
    bar = next((b for b in bars if str(b.get("label") or "") == label), None) or {}
    if bar.get("reused_headline"):
        o11y = (((acc.get("consolidation") or {}).get("serverless") or {}).get("by_solution") or {}).get("observability") or {}
        regions = [r for r in (o11y.get("by_region") or []) if isinstance(r, dict) and r.get("monthly") is not None]
        if len(regions) > 1:
            bits = [f"{r.get('region')} {_formula_money(r.get('monthly'))}" for r in regions]
            return "Observability headline, one read-back per region: " + " + ".join(bits) + f" = {_formula_money(monthly)}. No combined sheet is filled."
        return f"Observability headline {_formula_money(monthly)}. No combined sheet is filled."
    groups = [g for g in (bar.get("groups") or []) if isinstance(g, dict)]
    bits = []
    monies = []
    for group in groups:
        names = ", ".join(group.get("names") or []) or "project"
        money = _formula_money(group.get("monthly"))
        bulk = group.get("bulk_gb_day")
        if money:
            monies.append(money)
        if bulk is not None and money:
            bits.append(f"{names}, {float(bulk):.3f} GB/day, D32 {money}")
        elif money:
            bits.append(f"{names}, D32 {money}")
    if len(monies) > 1:
        return "; ".join(bits) + ". " + " + ".join(monies) + f" = {_formula_money(monthly)}."
    if bits:
        return bits[0] + "."
    if monthly is not None and float(monthly or 0) == 0:
        return "No project is filled, so there is no D32 to add."
    return "Sum of the Observability D32 read-backs at this cut."


def _plan_rows(acc: dict) -> list[list]:
    plan = (acc.get("consolidation") or {}).get("deployment_plan") or []
    if not plan:
        return []
    thirty = (acc.get("consolidation") or {}).get("retention_set_days") == 30
    month = "Every project is written as 1 month." if thirty else "Retention on a project is that group's measured months, rounded up."
    n = _plan_n(plan)
    n_bit = f" N = {n}, the active Observability deployments." if n is not None else ""
    rows = [[
        "Deployment-count cuts",
        "0% keeps N projects, one per Observability deployment. 25%, 50%, and 75% keep round(N × the share that remains). round is half-to-even, so .5 goes to the even integer and a cut can be 0 projects. 100% is held at 1 project. Its dollar is the Observability headline, not the combined sheet from the 50% cut.",
        month + " Bulk does not change." + n_bit,
        f"{len(plan)} cuts",
    ]]
    for row in plan:
        label = str(row.get("label") or "")
        pct = _cut_pct(label)
        if pct is None or n is None:
            formula = "Sum of the Observability D32 read-backs at this cut."
            count_line = _count_label(row.get("projects"), "project")
        elif pct >= 100:
            formula = "Held at 1 project. The dollar is the Observability headline."
            count_line = _plan_count_line(n, pct)
        elif pct <= 0:
            formula = "One project per Observability deployment."
            count_line = _plan_count_line(n, pct)
        else:
            formula = f"Projects kept = round(N × {(100 - pct) / 100:.2f})."
            count_line = _plan_count_line(n, pct)
        count = _count_label(row.get("projects"), "project")
        money = _formula_money(row.get("monthly"))
        rows.append([
            f"{label} of deployments",
            formula,
            count_line + " " + _plan_dollar_line(acc, label, row.get("monthly")),
            f"{count}, {money}" if count and money else money,
        ])
    return [row for row in rows if _keep(row)]


def _fleet_rows(acc: dict) -> list[list]:
    fleet = (((acc.get("consolidation") or {}).get("sensitivity") or {}).get("fleet")) or []
    rows = []
    for row in fleet:
        label = str(row.get("scenario") or "")
        if "estimator basis" in label.lower():
            continue
        if row.get("serverless_mo") is None:
            continue
        rows.append([
            label,
            "Quote-window read-back × (that scenario's ECU ÷ the quote-window ECU). Not re-estimated. EIS stays at its own figure.",
            f"Scale {_formula_num(row.get('scale'), 3)}. ECH {_formula_num(row.get('ech_ecu_mo'), 2)} ECU/month.",
            _formula_money(row.get("serverless_mo")),
        ])
    return rows


def _integrity_rows(acc: dict) -> list[list]:
    block = (acc.get("consolidation") or {}).get("integrity") or {}
    rows = []
    for row in block.get("checks") or []:
        name = row.get("check") or ""
        formula = _INTEGRITY_FORMULA.get(name)
        if not formula:
            continue
        score = row.get("score")
        if score is None:
            result = "n/a"
        else:
            result = f"{float(score):.3f}"
            if row.get("passed") is True:
                result += " pass"
            elif row.get("passed") is False:
                result += " fail"
        rows.append([name, formula, row.get("detail") or "This check does not apply.", result])
    if block.get("score") is not None:
        passed = "Pass is 0.90 or higher, and also when every scored check passes."
        rows.append([
            "Quote integrity",
            "The score is the mean of the checks that apply. A check with no inputs is n/a and stays out of the mean. " + passed,
            "This scores the written inputs, the sheet echo, and the published totals. It is not a serverless bill.",
            f"{float(block['score']):.3f}" + (" pass" if block.get("passed") else " fail"),
        ])
    return rows


def _formula_group(title: str, rows: list[list]) -> str:
    if not rows:
        return ""
    return f"<h3>{esc(title)}</h3>" + table(
        ["Value", "Formula", "Inputs used", "Result"],
        rows,
        ["left", "left", "left", "left"],
    )


def appendix_b_section(acc: dict) -> str:
    """Appendix B: each calculated value, its formula, the inputs, and the result already on this PDF."""
    groups = [
        _formula_group("Quote window", _quote_window_rows(acc)),
        _formula_group("Bulk and retention", _bulk_rows(acc)),
        _formula_group("30-day and compression scales", _scale_rows(acc)),
        _formula_group("Search workload", _workload_rows(acc)),
        _formula_group("Metered ingest rate", _metered_rows(acc)),
        _formula_group("Deployment count", _plan_rows(acc)),
        _formula_group("Fleet scales", _fleet_rows(acc)),
        _formula_group("Quote integrity", _integrity_rows(acc)),
    ]
    body = "".join(part for part in groups if part)
    if not body:
        return ""
    lead = bullets([
        "Each result is the figure already used in this report. Sheet dollars are read-backs. This page does not reprice them.",
        "A value that was not calculated on this PDF is left out.",
    ])
    return section("Appendix B", lead + body, new_page=True)


def _count_label(n, singular: str) -> str:
    if n is None:
        return ""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    word = singular if n == 1 else singular + "s"
    return f"{n:,} {word}"


def _priced_project_count(svl: dict) -> int | None:
    """Serverless projects in the headline estimate. One per priced solution, or one per priced region."""
    n = 0
    for v in (svl.get("by_solution") or {}).values():
        if not isinstance(v, dict):
            continue
        if not ((v.get("stored_gb") or 0) > 0 or (v.get("total_mo") or 0) > 0 or (v.get("ech_ecu_mo") or 0) > 0):
            continue
        regions = [
            r for r in (v.get("by_region") or [])
            if isinstance(r, dict) and ((r.get("monthly") or 0) > 0 or (r.get("sizing_monthly") or 0) > 0)
        ]
        n += len(regions) or 1
    return n or None


def deployment_spend_block(c: dict) -> str:
    """Top deployments by ECU. First page of consolidation section 1."""
    per_all = c.get("by_deployment") or []
    per = per_all[:10]
    if not per:
        return ""
    has_cost = any(r.get("ech_cost_mo") is not None for r in per)
    headers = ["Deployment", "Solution", "Stored GB", "ECU/mo", "Fixed ECU/mo", "Overhead %"]
    if has_cost:
        headers.insert(4, "Contract $/mo")
    rows, tones = [], []
    for r in per:
        gb_cell = n0(r["stored_gb"]) if r.get("gb_known") else (f"on {r['size_now_on']}" if r.get("size_now_on") else "no GB")
        row = [r["name"], r["solution"], gb_cell, n0(r["ech_ecu_mo"]), n0(r.get("fixed_overhead_ecu_mo")), pct(r.get("overhead_pct"))]
        if has_cost:
            row.insert(4, usd(r.get("ech_cost_mo")))
        rows.append(row)
        oh = r.get("overhead_pct")
        tones.append("warning" if oh is not None and float(oh) > 50 else None)
    title = "Where ECH spend sits"
    if len(per_all) > len(per):
        title += f" (top {len(per)} by ECU)"
    body = f"<h3>{title}</h3>" + table(headers, rows, ["left", "left"] + ["right"] * (len(headers) - 2), tones)
    if any(tones):
        body += caption("Shaded rows are fixed overhead above 50% of that deployment's ECU.")
    return body


def deployment_plan_section(c: dict, sv: dict, *, alongside: str = "") -> str:
    """Observability monthly estimate at 0/25/50/75/100% fewer deployments.

    When `alongside` is set, horizontal bars fill the left column and that
    HTML is the right column of the same page.
    """
    plan = c.get("deployment_plan") or []
    if not plan:
        return ""
    rows = [
        (str(p.get("label") or ""), _count_label(p.get("projects"), "project"), float(p.get("monthly") or 0))
        for p in plan
    ]
    if sv.get("ech_contract_mo") is not None:
        plan_ref = (float(sv["ech_contract_mo"]), f"ECH bill {usd(sv['ech_contract_mo'])}")
    else:
        plan_ref = None
    if c.get("retention_set_days") == 30:
        plan_ret = "Observability on every bar is 30 days, written as 1 month. Search is not on this chart."
    else:
        plan_ret = "Retention on each project is its measured months, rounded up."
    n = _plan_n(plan)
    n_bit = f" N = {n}." if n is not None else ""
    chart = plan_hbar(rows, ref=plan_ref, width=460)
    chart += caption(
        "The project count is on the right. The arrow beside it points down the column."
        + n_bit
        + " 0% keeps one project per Observability deployment. 25%, 50%, and 75% keep round(N × the share that remains), half-to-even, so a .5 can become 0 projects. "
        "100% stays 1 project and uses the Observability headline. That headline can differ from the 50% bar, which is one combined sheet. "
        + plan_ret
    )
    if alongside:
        chart = grid(
            chart,
            "<h3>Estimated monthly serverless total, by component</h3>" + alongside,
            ratio="0.86fr 1.14fr",
        )
    return section("Monthly estimate if the deployment count is cut", f"<div class='keep'>{chart}</div>", new_page=False)


def _contract_usd_per_ecu(cr: dict) -> float | None:
    """ARR ÷ (commit ECU/day × 365). The stored rate is rounded; the dollars use this one."""
    try:
        arr, commit = cr.get("arr"), cr.get("commit_ecu_per_day")
        if arr and commit:
            return float(arr) / (float(commit) * 365.0)
        if cr.get("implied_usd_per_ecu"):
            return float(cr["implied_usd_per_ecu"])
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None


def _serverless_data_out_price(solutions: set[str]) -> str:
    bits = []
    o11y, sec = "observability" in solutions, "security" in solutions
    if o11y and sec:
        bits.append("Observability and Security list data out at 50 GB free, then $0.05 per GB")
    elif o11y:
        bits.append("Observability lists data out at 50 GB free, then $0.05 per GB")
    elif sec:
        bits.append("Security lists data out at 50 GB free, then $0.05 per GB")
    elif not solutions:
        bits.append("Observability and Security list data out at 50 GB free, then $0.05 per GB")
    if "search" in solutions:
        bits.append("Elasticsearch Serverless lists data out as low as $0.05 per GB, with 50 GB free on vector profiles")
    if not bits:
        bits.append("Data out is a separate list meter, not inside the estimate")
    return ". ".join(bits) + "."


def egress_section(acc: dict) -> str:
    """Hosted data-transfer lines, and which of them are billed again on serverless."""
    cloud = acc.get("cloud") or {}
    nc = {str(t.get("type")): t for t in (cloud.get("non_capacity") or [])}
    c = acc.get("consolidation") or {}
    sv = c.get("savings") or {}
    days = float((acc.get("ecu") or {}).get("lookback_days") or (c.get("basis") or {}).get("lookback_days") or 0)
    months = days / 30.416 if days else 0
    rate = _contract_usd_per_ecu(c.get("contract_rate") or {})
    provider = str(((cloud.get("summary") or {}).get("provider")) or "").upper()
    solutions: set[str] = set()
    for key, val in ((c.get("serverless") or {}).get("by_solution") or {}).items():
        if isinstance(val, dict) and (float(val.get("total_mo") or 0) > 0 or float(val.get("deployments") or 0) > 0):
            solutions.add(str(key).lower())

    def month_ecu(key: str) -> float:
        ecu = float((nc.get(key) or {}).get("ecu") or 0)
        return ecu / months if months else 0.0

    def dollars(ecu_mo: float) -> str:
        return usd(ecu_mo * rate) if rate is not None else usd(ecu_mo)

    out_mo = month_ecu("data-transfer-out")
    out_pct = (nc.get("data-transfer-out") or {}).get("pct")
    money_hdr = "Contract $/mo" if rate is not None else "List $/mo"
    share = f", {pct(out_pct)} of the hosted bill" if out_pct is not None else ""
    priced = f"{dollars(out_mo)} at the contract rate" if rate is not None else f"{dollars(out_mo)} at list"
    intro = (
        f"Data out is {n1(out_mo)} ECU/mo ({priced}){share}. "
        f"The {usd(sv.get('serverless_mo'))} estimate does not include it. "
        f"The reduction on the cover counts this hosted charge as going away."
    )
    rows = []
    for key, label in (
        ("data-transfer-in", "Data in"),
        ("data-transfer-out", "Data out"),
        ("data-transfer-inter-node", "Inter-node"),
    ):
        rec = nc.get(key) or {}
        ecu_mo = month_ecu(key)
        rows.append([label, n0(rec.get("ecu")), n1(ecu_mo), pct(rec.get("pct")), dollars(ecu_mo)])
    billed = table(
        ["Meter", "Lookback ECU", "ECU/mo", "Share", money_hdr],
        rows,
        ["left", "right", "right", "right", "right"],
    )
    if provider == "AZURE":
        inter_ech = "Waived on Azure. This account billed nothing for inter-node."
    elif month_ecu("data-transfer-inter-node"):
        inter_ech = "Billed. Traffic between the nodes of a deployment, including shard copies and Kibana."
    else:
        inter_ech = "This account billed nothing for inter-node."
    def meter(label: str) -> Raw:
        return Raw(f"<span style='white-space:nowrap'>{esc(label)}</span>")

    carry = table(
        ["Meter", "On ECH", "On serverless"],
        [
            [
                meter("Data in"),
                "Free. Index requests and queries into the deployment.",
                "Free. Ingest GB is a different meter, and it is already in the estimate.",
            ],
            [
                meter("Data out"),
                "Billed after 100 GB a month, shared with inter-node, across every deployment. Search results, monitoring, and Private Link count, to any destination.",
                "Carries over, outside the estimate. " + _serverless_data_out_price(solutions) + " Private Link counts the same way.",
            ],
            [
                meter("Inter-node"),
                inter_ech,
                "Does not carry over. Serverless does not bill traffic between the nodes Elastic runs.",
            ],
        ],
        ["left", "left", "left"],
    )
    rate_note = (
        "Contract $ is that monthly ECU × ARR ÷ (commit ECU/day × 365)."
        if rate is not None
        else "List $ is $1 per ECU."
    )
    notes = bullets([
        f"ECU/mo is lookback ECU ÷ ({n0(days)} / 30.416). {rate_note}",
        "Snapshot storage and snapshot API calls are separate from these three meters. They stay in the non-node total on the overhead page.",
        "The hosted bill records ECU, not GB. This page does not convert that ECU into a serverless dollar.",
    ])
    body = f"<p style='margin:0 0 8px'>{esc(intro)}</p><h3>What the hosted bill charged</h3>" + billed + "<h3>What changes on serverless</h3>" + carry + notes
    return section("Data egress, and what carries over to serverless", body, new_page=False)


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
    search_mo = float(((svl.get("by_solution") or {}).get("search") or {}).get("total_mo") or 0)
    o11y_mo = float(((svl.get("by_solution") or {}).get("observability") or {}).get("total_mo") or 0)
    sec_mo = float(((svl.get("by_solution") or {}).get("security") or {}).get("total_mo") or 0)
    if scenario == "30d":
        named = [label for label, amount in (("Observability", o11y_mo), ("Security", sec_mo)) if amount]
        bits = []
        if named:
            bits.append(" and ".join(named) + " at 30 days")
        if search_mo:
            bits.append("Search at full retention")
        headline += " — " + (", ".join(bits) if bits else "retention 30 days")
    elif scenario == "rounded":
        headline += " — retention rounded up"
    if c.get("ingest_basis") == "compressioncalc_accounted_for":
        headline += " — compressioncalc_accounted_for"
    source_bit = " · ingest and retention from Elasticsearch diagnostics" if diag else " · K360 backup: stored GB ÷ assumed retention"
    dep_n = n0(b0.get("deployments"))
    active_cov = ((acc.get("sizing") or {}).get("diagnostics") or {})
    quote_days = b0.get("quote_lookback_days")
    ecu_basis = f"{int(quote_days)}-day lookback" if quote_days else "complete-month avg"
    ecu_src = b0.get("ech_monthly_ecu")
    if active_cov.get("inactive_excluded"):
        dep_label = f"{dep_n} active deployments"
        ecu_label = f"{n0(ecu_src)} ECU/mo account bill ({ecu_basis})"
    else:
        dep_label = f"{dep_n} ECH deployments"
        ecu_label = f"{n0(ecu_src)} ECU/mo ({ecu_basis})"
    B.append("<header class='masthead'><div class='brand'>Elastic<i></i></div>"
             f"<h1>{headline}</h1>"
             f"<div class='sub'>{dep_label} · {ecu_label} · {n0(b0.get('stored_gb'))} GB stored · {esc(cloud.get('provider'))} {esc(cloud.get('region'))} · {esc(m.get('date'))}{source_bit}</div></header>")
    rc = c.get("reconciliation") or {}
    project_n = _priced_project_count(svl)
    compare_subs = [
        _count_label(b0.get("deployments"), "deployment"),
        _count_label(project_n, "project"),
    ]
    if not any(compare_subs):
        compare_subs = None
    if quote_days:
        window = f"last {int(quote_days)} billed days"
        chart_title = f"Last {int(quote_days)} days of billed usage"
        ech_cat = f"ECH, {window}"
        svl_cat = f"Serverless, {window}"
    else:
        window = ""
        chart_title = "ECH cost today vs. the serverless estimate"
        ech_cat = "ECH at contract rate" if sv.get("cost_mo") is not None else "ECH at list"
        svl_cat = "Serverless estimate"
    if sv.get("cost_mo") is not None:
        ech_stat = "Monthly ECH cost at the contract rate" + (f", {window}" if window else "")
        svl_stat = "Estimated monthly serverless cost" + (f", {window}" if window else "")
        save_stat = "Estimated monthly reduction versus the contract rate" + (f", {window}" if window else "")
        pct_stat = "Estimated reduction versus the contract rate" + (f", {window}" if window else "")
        stats = [
            stat(usd(sv.get("ech_contract_mo")), ech_stat, tag="calculated"),
            stat(usd(sv.get("serverless_mo")), svl_stat, "success", tag="estimate", estimate=True),
            stat(usd(sv.get("cost_mo")), save_stat, "success", tag="estimate", estimate=True),
            stat(pct(sv.get("cost_pct")), pct_stat, "success", tag="estimate", estimate=True, down=_reduction(sv.get("cost_pct"))),
        ]
        ech_vals = [float(sv.get("ech_contract_mo") or 0), 0.0]
        svl_vals = [0.0, float(sv.get("serverless_mo") or 0)]
    else:
        ech_stat = "Monthly ECH cost at list price" + (f", {window}" if window else "")
        svl_stat = "Estimated monthly serverless cost" + (f", {window}" if window else "")
        save_stat = "Estimated monthly reduction versus list price" + (f", {window}" if window else "")
        pct_stat = "Estimated reduction versus list price" + (f", {window}" if window else "")
        stats = [
            stat(usd(sv.get("ech_list_mo")), ech_stat, tag="billed"),
            stat(usd(sv.get("serverless_mo")), svl_stat, "success", tag="estimate", estimate=True),
            stat(usd(sv.get("list_mo")), save_stat, "success", tag="estimate", estimate=True),
            stat(pct(sv.get("list_pct")), pct_stat, "success", tag="estimate", estimate=True, down=_reduction(sv.get("list_pct"))),
        ]
        ech_vals = [float(sv.get("ech_list_mo") or 0), 0.0]
        svl_vals = [0.0, float(sv.get("serverless_mo") or 0)]
    B.append(f"<div class='stats' style='grid-template-columns:repeat({len(stats)},1fr)'>{''.join(stats)}</div>")
    B.append(f"<h3>{esc(chart_title)}</h3>" + bar_chart(
        [ech_cat, svl_cat], [(ech_cat, ech_vals), (svl_cat, svl_vals)],
        stacked=True, width=700, height=230, show_values=True, prefix="$", exact_values=True, colors=[PALETTE[0], PALETTE[2]],
        sublabels=compare_subs))
    quote_bits = []
    for q in rc.get("quotes") or []:
        quote_bits.append(f"{estimator_label(q.get('source'))} {usd(q.get('monthly'))}/mo")
    if sv.get("cost_mo") is not None and quote_days:
        cover_notes = [
            f"Calculated: the last {int(quote_days)} days of billed ECU, as a month (that total ÷ {int(quote_days)} × 30.416), at the contract rate, ARR ÷ (commit ECU per day × 365). Estimate: the serverless estimators filled at that same pace. The contract rate is not applied to serverless.",
            ("The serverless estimate is " + " plus ".join(quote_bits) + ".") if quote_bits else "The serverless estimate is one project per solution.",
            f"The rate is ${cr.get('implied_usd_per_ecu')} per ECU, shown rounded and applied unrounded. Those {int(quote_days)} days put ECH at {usd(sv.get('ech_contract_mo'))}/mo. The serverless estimate is {usd(sv.get('cost_mo'))}/mo lower, a {pct(sv.get('cost_pct'))} reduction.",
        ]
    elif sv.get("cost_mo") is not None:
        cover_notes = [
            "Calculated: ECU at the contract rate, ARR ÷ (commit ECU per day × 365). Estimate: from the serverless estimators, not a quote or a bill. The contract rate is not applied to serverless.",
            ("The serverless estimate is " + " plus ".join(quote_bits) + ".") if quote_bits else "The serverless estimate is one project per solution.",
            f"The rate is ${cr.get('implied_usd_per_ecu')} per ECU, shown rounded and applied unrounded. That puts ECH at {usd(sv.get('ech_contract_mo'))}/mo. The serverless estimate is {usd(sv.get('cost_mo'))}/mo lower, a {pct(sv.get('cost_pct'))} reduction.",
        ]
    elif quote_days:
        cover_notes = [
            f"Billed: the last {int(quote_days)} days of ECU at list, $1 per ECU, as a month (that total ÷ {int(quote_days)} × 30.416). Estimate: the serverless estimators filled at that same pace.",
            ("The serverless estimate is " + " plus ".join(quote_bits) + ".") if quote_bits else "The serverless estimate is one project per solution.",
        ]
    else:
        cover_notes = [
            "Billed: ECU from K360 billing at list, $1 per ECU. Estimate: from the serverless estimators, not a quote or a bill.",
            ("The serverless estimate is " + " plus ".join(quote_bits) + ".") if quote_bits else "The serverless estimate is one project per solution.",
        ]
    if diag and c.get("ingest_basis") == "compressioncalc_accounted_for":
        ratio = c.get("compression_ratio")
        ratio_bit = "unset" if ratio is None else str(ratio)
        cover_notes.append(
            f"Ingest is compressioncalc_accounted_for: {c.get('compressioncalc_accounted_for')} GB/day. "
            f"That is bulk {c.get('bulk_gb_day')} GB/day multiplied by 0.60. "
            f"The ratio is {ratio_bit}. On-disk bytes are not used. "
            "The diagnostic bulk quote is a separate estimate. Search disk on this set is measured store × 0.60."
        )
        thirty = c.get("thirty_day") or {}
        if c.get("retention_set_days") == 30:
            cover_notes.append(
                "Observability and Security are written as 1 month. Search disk is the compressed store scaled to 1 month, on its own 30-day sheet."
            )
        elif thirty.get("monthly") is not None:
            cover_notes.append(
                f"The same compression-accounted ingest at 30 days is written as 1 month and reads {usd(thirty.get('monthly'))}/mo, year 1 {usd(thirty.get('annual_year1'))}. The saved sheet stays at the rounded measured month."
            )
    elif diag:
        cover_notes.append("Ingest is bulk GB/day from the diagnostic bundles. K360 does not provide a raw ingest total. Stored GB ÷ assumed retention is the --k360-ingest backup and can sit far below this bulk rate.")
        thirty = c.get("thirty_day") or {}
        if c.get("retention_set_days") == 30:
            cover_notes.append(
                "Observability and Security are written as 1 month. Search disk is the measured store scaled to 1 month, on its own 30-day sheet."
            )
        elif thirty.get("monthly") is not None:
            cover_notes.append(
                f"The same bulk at 30 days is written as 1 month and reads {usd(thirty.get('monthly'))}/mo, year 1 {usd(thirty.get('annual_year1'))}. The saved sheet stays at the rounded measured month."
            )
    else:
        cover_notes.append("Ingest is the K360 backup: stored GB ÷ assumed retention. K360 does not provide a raw ingest total, so this can sit far below the bulk payload the cluster accepted.")
    B.append(bullets(cover_notes))
    detail = serverless_component_slide(acc, bare=True)
    plan_page = deployment_plan_section(c, sv, alongside=detail)
    if plan_page:
        B.append(plan_page)
    else:
        B.append(section("Estimated monthly serverless total, by component", detail, new_page=False))

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
    lookback_total = None
    if quote_days:
        raw_total = sv.get("ech_contract_mo") if contract_rate else sv.get("ech_list_mo")
        if raw_total is not None:
            lookback_total = float(raw_total)
    slots = 3 if lookback_total is not None else 2

    def _bars(ech: float, serverless: float) -> list[float]:
        vals = [0.0] * slots
        vals[0] = ech
        vals[-1] = serverless
        return vals

    pieces = [
        ("Data tiers", _ech(comp.get("data_tiers_mo")), 0.0),
        ("Fixed overhead", _ech(comp.get("fixed_overhead_mo")), 0.0),
        ("ML nodes", _ech(comp.get("ml_nodes_mo")), 0.0),
        ("Snapshots + transfer", _ech(comp.get("non_capacity_mo")), 0.0),
        ("Ingest", 0.0, sum((v or {}).get("ingest_mo") or 0 for v in by_sol_bars.values())),
        ("Retention", 0.0, sum((v or {}).get("retention_mo") or 0 for v in by_sol_bars.values())),
        ("Search VCU", 0.0, sum((v or {}).get("search_mo") or 0 for v in by_sol_bars.values())),
        ("EIS", 0.0, sum((v or {}).get("eis_mo") or 0 for v in by_sol_bars.values())),
        ("Support", 0.0, sum((v or {}).get("support_mo") or 0 for v in by_sol_bars.values())),
    ]
    for _sol_name, sol_blk in by_sol_bars.items():
        if "sheet" in str((sol_blk or {}).get("pricing") or "") and (sol_blk.get("total_mo") or 0) > 0:
            pieces.append((estimator_label(sol_blk.get("pricing")), 0.0, float(sol_blk.get("total_mo") or 0)))
    ser = [(name, _bars(ech, serverless)) for name, ech, serverless in pieces]
    other = (sv.get("serverless_mo") or 0) - sum(vals[-1] for _name, vals in ser)
    if other > 1:
        ser.append(("Other serverless", _bars(0.0, other)))
    if lookback_total is not None:
        usage = [0.0, lookback_total, 0.0]
        ser.append((f"{int(quote_days)}-day usage", usage))
    ser = [s for s in ser if any((v or 0) > 0.5 for v in s[1])]
    categories = ["Full lookback", f"{int(quote_days)}-day lookback", "Serverless"] if lookback_total is not None else ["Full lookback", "Serverless"]
    chart = bar_chart(categories, ser, stacked=True, width=440, height=230, show_values=True)
    projects = table(["Project", "Deps", "Stored GB", "Raw GB/d", "Metered GB/d", "Total $/mo", "ECH ECU/mo", "Pricing"],
                     [[k.title(), v.get("deployments"), n0(v.get("stored_gb")), n1(v.get("daily_raw_gb")), n0(v.get("daily_metered_gb")), usd(v.get("total_mo")), n0(v.get("ech_ecu_mo")), estimator_label(v.get("pricing"))] for k, v in (svl.get("by_solution") or {}).items() if (v.get("stored_gb") or 0) > 0 or (v.get("ech_ecu_mo") or 0) > 0],
                     ["left", "right", "right", "right", "right", "right", "right", "left"])
    per_spend = c.get("by_deployment") or []
    callouts_html = "".join(callout(x["title"], esc(x["detail"]), x.get("tone") or "info") for x in c.get("callouts") or [])
    if quote_days:
        spend_notes = [f"ECU/mo and Fixed ECU/mo are the last {int(quote_days)} billed days, the same pace as the cover."]
    else:
        spend_notes = ["ECU/mo is lookback ECU ÷ (lookback days / 30.416)."]
    spend_notes.append("Overhead % is fixed-role ECU ÷ that deployment’s ECU. Fixed roles are master, Kibana, coordinating, integrations server, enterprise search, and APM.")
    if any((r.get("ech_cost_mo") is not None) for r in per_spend[:10]):
        spend_notes.append(f"Contract $/mo multiplies ECU/mo by ARR ÷ (commit × 365). The rate is shown rounded as ${cr.get('implied_usd_per_ecu')}.")
    if fr.get("size_gaps"):
        spend_notes.append("“on <name>” means this cluster’s size now reports under that name, and the GB is counted there. “no GB” means no current size.")
    elif per_spend:
        spend_notes.append("“no GB” means the size snapshot had no row. That deployment is not given a serverless price.")
    spend_table = deployment_spend_block(c)
    spend_detail = callouts_html + bullets(spend_notes)
    spend_body = spend_table + spend_detail
    if spend_body:
        B.append(section("Where ECH spend sits", spend_body, new_page=False))
    if lookback_total is not None and contract_rate:
        build_notes = [
            f"Full lookback: ECU over {n0(b0.get('lookback_days'))} days, divided into a month and split by tier, at the contract rate.",
            f"{int(quote_days)}-day lookback: the usage total on the cover, {usd(lookback_total)}. It is not split by tier.",
            "Serverless: dollars per month from the estimator at that lookback.",
            "All three bars are dollars.",
        ]
    elif lookback_total is not None:
        build_notes = [
            f"Full lookback: ECU over {n0(b0.get('lookback_days'))} days, divided into a month and split by tier.",
            f"{int(quote_days)}-day lookback: {n0(lookback_total)} ECU/mo, the usage total on the cover. It is not split by tier.",
            "Serverless: dollars per month from the estimator at that lookback.",
            "The ECH bars are ECU and the serverless bar is dollars, because list price treats 1 ECU as $1.",
        ]
    elif contract_rate:
        build_notes = [
            f"Full lookback: those ECU at the contract rate. It includes partial months, so it will not equal the {usd(sv.get('ech_contract_mo'))} complete-month contract cost in the headline.",
            "Serverless: dollars per month from the serverless build-up.",
            "Both bars are dollars.",
        ]
    else:
        build_notes = [
            f"Full lookback: Cloud-* tier ECU ÷ ({b0.get('lookback_days')} / 30.416). It includes partial months, so it will not equal the {n0(sv.get('ech_list_mo'))} complete-month average in the headline.",
            "Serverless: dollars per month from the serverless build-up.",
            "Y axis mixes ECU and dollars because list price treats 1 ECU as $1.",
        ]
    B.append(section("What you pay for today, and what replaces it", grid(unit, "<h3>Monthly build-up</h3>" + chart, ratio="1.05fr 1fr") + "<h3>Projects</h3>" + projects + bullets(build_notes), new_page=False))

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
                f"It is in the Search total, in the regional projects, in every Search profile, and in every row of the fleet sensitivity. "
                f"Quieter and busier months scale the rest of the estimate and leave this line unchanged. "
                f"Another {n0(search_blk.get('other_ml_ecu'))} ECU of ML nodes have no jina SKU and are not in the line. "
                "The node ECU is the capacity on the same deployment, not a per-model meter.",
            ) + eis_table,
            new_page=False,
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
                     + stat(f"{n0(t.get('ech_ecu_mo'))} ECU", f"their bill · {pct(t.get('ech_share_pct'))} of ECU")
                     + stat(f"{n0(t.get('stored_gb'))} GB", "data stored")
                     + stat(f"{n0(t.get('fixed_overhead_ecu_mo'))} ECU", "fixed overhead per month")
                     + f"</div><div class='e'>Their bill is not the fleet overhead above. Merging them onto fewer clusters saves about {n0(t.get('ech_consolidation_saving_mo'))} ECU/mo, if a quarter of their bill goes away.</div></div>")
    active_pct = fr.get("fixed_overhead_pct")
    if active_pct is None and fr.get("fixed_overhead_ecu_mo") and fr.get("ech_ecu_mo"):
        active_pct = round(100 * float(fr["fixed_overhead_ecu_mo"]) / float(fr["ech_ecu_mo"]), 1)
    oh = active_pct if active_pct is not None else comp.get("fixed_overhead_pct")
    title2 = f"{pct(oh)} of the active deployments' ECU is cluster overhead" if oh is not None else "Cluster overhead"
    search_eis = (svl.get("by_solution") or {}).get("search") or {}
    if search_eis.get("ml_node_ecu_mo"):
        ml_line = f"Machine learning on Search is in the EIS line: {usd(search_eis.get('eis_mo'))}/mo. That is the billed tokens plus {n0(search_eis.get('jina_ml_ecu'))} ECU of ML nodes on deployments that billed jina ({n0(search_eis.get('jina_ml_ecu_mo'))} ECU/mo). Snapshot storage, snapshot API calls, and data transfer over the full lookback add {n0(comp.get('non_capacity_mo'))} ECU/mo. The egress section follows."
    else:
        ml_line = f"Machine learning over the full lookback adds {n0(comp.get('ml_nodes_mo'))} ECU/mo. Snapshot storage, snapshot API calls, and data transfer add {n0(comp.get('non_capacity_mo'))} ECU/mo. The egress section follows."
    shown_fixed = fr.get("fixed_overhead_ecu_mo") if fr.get("fixed_overhead_ecu_mo") else comp.get("fixed_overhead_mo")
    year_fixed = comp.get("fixed_overhead_mo")
    year_bit = ""
    if year_fixed and shown_fixed and abs(float(year_fixed) - float(shown_fixed)) > 1:
        inactive_n = ((acc.get("sizing") or {}).get("diagnostics") or {}).get("inactive_excluded")
        year_bit = f" The full lookback is {n0(year_fixed)} ECU/mo, {pct(comp.get('fixed_overhead_pct'))} of that year"
        year_bit += (
            f", including {int(inactive_n)} {'deployment' if int(inactive_n) == 1 else 'deployments'} with no bill in the latest month."
            if inactive_n else "."
        )
    pace_bit = f" on the last {int(quote_days)} billed days" if quote_days else ""
    B.append(section(title2,
                     bullets([
                         f"These are nodes that do not hold data: master, Kibana, coordinating, integrations, enterprise search, and APM. Together they are {n0(shown_fixed)} ECU/mo{pace_bit}.{year_bit} Serverless does not charge for them.",
                         ml_line,
                     ]) + grid(left, tail_card), new_page=False))
    B.append(egress_section(acc))

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
    title3 = "One project per solution in each region" if arch.get("per_region") else "One project per solution, not one per deployment"
    B.append(section(title3, grid(left3, "<h3>ECH ECU per month, largest deployments</h3>" + ecu_bar) + arch_html, new_page=False))

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
        if "day lookback" in low:
            return scenario.split("(")[0].strip()
        if "complete-month" in low:
            return "Complete month"
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
        if "day lookback" in low:
            return scenario.split("(")[0].strip()
        if "complete-month" in low:
            return "Complete-month average"
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
    fc = bar_chart([_fleet_axis(f["scenario"]) for f in fleet], fleet_series, width=920, height=300, show_values=True, prefix="$", colors=colors5, exact_values=True)
    if has_contract:
        ft_headers = ["Usage", "ECH at contract rate", "Serverless, estimate", "Savings vs. contract rate"]
        ft_rows = [[_fleet_row_label(f["scenario"]), usd(f.get("ech_cost_mo")), usd(f.get("serverless_mo")), usd(f.get("save_vs_contract_mo"))] for f in fleet]
        ft_align = ["left", "right", "right", "right"]
    else:
        ft_headers = ["Usage", "ECH at list", "Serverless, estimate", "Savings vs. list"]
        ft_rows = [[_fleet_row_label(f["scenario"]), usd(f["ech_ecu_mo"]), usd(f.get("serverless_mo")), usd(f.get("save_vs_list_mo"))] for f in fleet]
        ft_align = ["left", "right", "right", "right"]
    def _cover_row(f: dict) -> bool:
        return abs(float(f.get("scale") or 0) - 1) < 1e-9

    ft = table(ft_headers, ft_rows, ft_align, tones=["info" if _cover_row(f) else None for f in fleet])
    others = [f for f in fleet if abs(float(f.get("scale") or 0) - 1) > 1e-9]
    fleet_notes = []
    if has_svl:
        scales = " and ".join(f"×{float(f['scale']):.2f}" for f in others)
        if any(f.get("eis_mo") for f in fleet):
            fleet_notes.append(f"EIS stays at {usd(next(f.get('eis_mo') for f in fleet if f.get('eis_mo')))}/mo in every row. Quieter and busier months scale the rest of the estimate and leave that line unchanged.")
        basis_label = f"The {int(quote_days)}-day lookback" if quote_days else "Average month"
        fleet_notes.append(f"{basis_label} is the serverless estimate in this report. The other rows scale it by the same usage change ({scales}). The estimators were not re-run for them." if others else "The serverless figure is the estimate in this report.")
        busy = next((f for f in fleet if "last 30" in f["scenario"].lower() and abs(float(f.get("scale") or 0) - 1) > 1e-9), None)
        if busy and busy.get("save_vs_contract_mo") is not None:
            fleet_notes.append(f"At the last-30-day pace, the estimated savings against the contract rate are {usd(busy['save_vs_contract_mo'])}/mo.")
    else:
        fleet_notes.append("The serverless price was not recalculated.")
    B.append(section("What if data is kept longer, or the fleet is busier",
                     "<h3>How long Search data is kept</h3>" + rt + bullets([ret_note])
                     + "<h3>ECH vs. the serverless estimate as usage changes</h3>" + fc
                     + ft + bullets(fleet_notes),
                     new_page=False))

    # 6
    quotes = rc.get("quotes") or []
    if quotes:
        card = ("<div class='card'><div class='hd'>Serverless estimators<span>the prices in this report</span></div>"
                + table(["Solution", "Estimator", "$ / mo", "Year 1", "Steady annual"],
                        [[q.get("solution", "").title(), estimator_label(q.get("source")), usd(q.get("monthly")), usd(q.get("annual_year1")), usd(q.get("annual_steady_state"))] for q in quotes]
                        + [["Total", "both estimators", usd(rc.get("total_mo")), "—", "—"]])
                + "<br>".join(str(link(q.get("url"), f"Open the {estimator_label(q.get('source')).split(',')[0]} estimator")) for q in quotes if q.get("url"))
                + bullets(["These are the serverless estimates. They add up to the headline. The fleet sensitivity scales them for quieter and busier months."])
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
    contract_items: list[str] = []
    if cr.get("implied_usd_per_ecu"):
        contract_items.extend([
            f"ARR {usd(cr.get('arr'))} against {n0(cr.get('commit_ecu_per_day'))} ECU/day is ${cr.get('implied_usd_per_ecu')}/ECU.",
            f"The same {n0(sv.get('ech_list_mo'))} ECU/mo is {usd(sv.get('ech_contract_mo'))} at that rate.",
            "The rate is ARR ÷ (commit ECU/day × 365), applied unrounded and shown rounded.",
            "Serverless is the estimator price and is not discounted. Verify the contract with the AE.",
        ])
    note_items = [x for x in (c.get("caveats") or []) + (c.get("notes") or []) if x]
    if contract_items:
        recon = grid(card, "<h3>Before you quote</h3>" + bullets(contract_items), ratio="1.15fr 1fr")
    else:
        recon = card
    if note_items:
        recon += bullets(note_items, columns=2)
    foot_src = "Elasticsearch diagnostics joined to K360 billing" if _from_diagnostics(acc) else "K360 cloud-tracker billing + telemetry"
    B.extend(cost_sizing_sections(acc, sizing=False, new_page=False, numbered=False))
    B.append(section("Reconciliation and what to confirm before quoting", recon + f"<div class='foot'>Source: {foot_src} · {esc(svl.get('pricing_source'))} · generated {esc(m.get('generated_at'))} · only estimator read-backs are quotable $</div>", new_page=False))
    appendix = search_power_section(acc)
    if appendix:
        B.append(appendix)
    formulas = appendix_b_section(acc)
    if formulas:
        B.append(formulas)
    return page(f"{m.get('customer')} — serverless consolidation savings", "".join(B), kind="consolidation")


# --------------------------------------------------------------------------- chrome
def find_chrome() -> str | None:
    for cand in (os.environ.get("CHROME_PATH"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium", "/usr/bin/google-chrome", "/usr/bin/chromium"):
        if cand and Path(cand).is_file():
            return cand
    return None


def chrome_pdf(html_path: Path, pdf_path: Path, chrome: str | None = None) -> None:
    chrome = chrome or find_chrome()
    if not chrome:
        raise FileNotFoundError("Chrome not found — set CHROME_PATH. The HTML file is the report.")
    tmp = pdf_path.with_suffix(".pdf.part")
    try:
        with tempfile.TemporaryDirectory(prefix="k360-chrome-") as prof:
            cmd = [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run", f"--user-data-dir={prof}", f"--print-to-pdf={tmp}", html_path.resolve().as_uri()]
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        if not tmp.is_file() or tmp.stat().st_size < 1000:
            raise RuntimeError(f"Chrome produced no PDF at {pdf_path}")
        tmp.replace(pdf_path)
        _seal_file(pdf_path)
    finally:
        tmp.unlink(missing_ok=True)


def _seal_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        return


def _seal_dir(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        return


def export_documents(docs: list[tuple[dict, str]], folder: Path, *, skip_pdf: bool = False) -> dict:
    """Render every document, then print the PDFs on one Chrome pool.

    Each item is (account, kind). kind is `account` or a consolidation kind such as
    `consolidation`, `consolidation_30d`, `consolidation_compressioncalc`, or
    `consolidation_compressioncalc_30d`.
    """
    out: dict = {}
    pending: list[tuple[str, Path, Path]] = []
    for acc, kind in docs:
        if kind != "account" and (not acc.get("consolidation") or (acc.get("consolidation") or {}).get("error")):
            continue
        key = acc["meta"]["run_key"]
        fn = render_account_html if kind == "account" else render_consolidation_html
        sub = pdf_folder_name(kind)
        out_dir = folder / sub if sub else folder
        out_dir.mkdir(parents=True, exist_ok=True)
        _seal_dir(out_dir)
        stem = f"{key}_{ACCOUNT_SUFFIX}" if kind == "account" else consolidation_pdf_stem(key, kind)
        html_path = out_dir / f"{stem}.html"
        pdf_path = out_dir / f"{stem}.pdf"
        try:
            html_path.write_text(fn(acc))
            _seal_file(html_path)
        except Exception as ex:  # one document does not drop the rest of the batch
            out[kind] = {"title": stem, "error": repr(ex)}
            continue
        out[kind] = {"title": stem, "html": str(html_path)}
        if not skip_pdf:
            pending.append((kind, html_path, pdf_path))
    if pending:
        chrome = find_chrome()
        if not chrome:
            for kind, _html_path, _pdf_path in pending:
                out[kind]["error"] = "Chrome not found — set CHROME_PATH. The HTML file is the report."
            return out
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


def export(acc: dict, folder: Path, *, skip_pdf: bool = False, only: str | None = None, consolidation_suffix: str | None = None, consolidation_kind: str = "consolidation") -> dict:
    docs: list[tuple[dict, str]] = []
    if only in (None, "account"):
        docs.append((acc, "account"))
    if only in (None, "consolidation") and acc.get("consolidation") and not (acc["consolidation"] or {}).get("error"):
        docs.append((acc, consolidation_kind))
    del consolidation_suffix
    return export_documents(docs, folder, skip_pdf=skip_pdf)


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
    _seal_file(src)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
