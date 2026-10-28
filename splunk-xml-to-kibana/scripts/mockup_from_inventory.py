#!/usr/bin/env python3
"""Build Splunk-faithful mockup/index.html from inventory.json.

Usage:
  python mockup_from_inventory.py --inventory PACKAGE_ROOT/inventory.json --out PACKAGE_ROOT/mockup
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

CSS = """
:root {
  --bg: #1a1c20; --bg2: #22252b; --bg3: #2b3038; --border: #3a414c;
  --text: #e8eaed; --muted: #9aa0a6; --accent: #5ccfe6; --bar: #5ccfe6;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); font: 13px/1.4 var(--font); }
.topbar {
  display: flex; align-items: center; gap: 16px; padding: 10px 16px;
  background: #111318; border-bottom: 1px solid var(--border);
}
.brand { font-weight: 700; color: var(--accent); }
.title { font-size: 15px; font-weight: 600; }
.meta { margin-left: auto; color: var(--muted); font-size: 12px; }
.inputs {
  display: flex; flex-wrap: wrap; gap: 10px; padding: 10px 16px;
  background: var(--bg2); border-bottom: 1px solid var(--border);
}
.input { display: flex; flex-direction: column; gap: 4px; min-width: 140px; }
.input label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
.input .ctl {
  background: var(--bg3); border: 1px solid var(--border); color: var(--text);
  border-radius: 3px; padding: 6px 8px; min-height: 28px;
}
.page { padding: 14px 16px 40px; }
.stack { display: flex; flex-direction: column; gap: 12px; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(4, 1fr); }
@media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
.panel {
  background: var(--bg2); border: 1px solid var(--border); border-radius: 4px;
  min-height: 110px; overflow: hidden;
}
.panel.kpi { min-height: 100px; }
.panel.wide { grid-column: 1 / -1; }
.panel h3 {
  margin: 0; padding: 8px 12px; font-size: 12px; font-weight: 600;
  border-bottom: 1px solid var(--border); background: var(--bg3);
}
.panel .body { padding: 12px; color: var(--muted); }
.kpi-val { font-size: 28px; font-weight: 700; color: var(--accent); }
.bars { display: flex; align-items: flex-end; gap: 4px; height: 72px; }
.bars span { flex: 1; background: linear-gradient(180deg, var(--bar), #2a6f7c); border-radius: 2px 2px 0 0; }
.footer { margin-top: 24px; color: var(--muted); font-size: 11px; border-top: 1px solid var(--border); padding-top: 12px; }
"""


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _control_strip(inputs: list[dict]) -> str:
    if not inputs:
        return (
            '<div class="inputs">'
            '<div class="input"><label>Time Range</label><div class="ctl">Last 24 hours</div></div>'
            "</div>"
        )
    parts = ['<div class="inputs">']
    for inp in inputs:
        label = inp.get("label") or inp.get("token") or "input"
        default = str(inp.get("default") or "")[:80] or "—"
        itype = inp.get("type") or "text"
        if itype == "time":
            default = default or "Last 24 hours"
        parts.append(
            f'<div class="input"><label>{_esc(label)}</label>'
            f'<div class="ctl">{_esc(default)}</div></div>'
        )
    parts.append("</div>")
    return "\n".join(parts)


def _panel_block(p: dict) -> str:
    title = p.get("title") or p.get("id") or "panel"
    viz = (p.get("viz") or "table").lower()
    chart = (p.get("chart_type") or "").lower()
    custom = (p.get("custom_viz") or "").lower()
    if viz == "single" or viz == "metric":
        return (
            f'<section class="panel kpi"><h3>{_esc(title)}</h3>'
            f'<div class="body"><div class="kpi-val">—</div></div></section>'
        )
    if viz in ("chart", "xy") or chart in ("bar", "column", "line", "area", "pie"):
        bars = "".join(f'<span style="height:{h}%"></span>' for h in (40, 70, 55, 90, 60))
        return (
            f'<section class="panel wide"><h3>{_esc(title)}</h3>'
            f'<div class="body"><div class="bars">{bars}</div></div></section>'
        )
    if "force" in custom or "sankey" in custom or viz == "custom":
        return (
            f'<section class="panel wide"><h3>{_esc(title)}</h3>'
            f'<div class="body">Custom viz placeholder ({_esc(custom or viz)})</div></section>'
        )
    return (
        f'<section class="panel wide"><h3>{_esc(title)}</h3>'
        f'<div class="body">Table placeholder</div></section>'
    )


def render(inv: dict) -> str:
    title = inv.get("title") or "Splunk Dashboard Mockup"
    theme = inv.get("theme") or "dark"
    kind = inv.get("kind") or ""
    panels = inv.get("panels") or []
    inputs = inv.get("inputs") or []
    kpis = [p for p in panels if (p.get("viz") or "").lower() in ("single", "metric")]
    rest = [p for p in panels if p not in kpis]
    kpi_html = "\n".join(_panel_block(p) for p in kpis) if kpis else ""
    rest_html = "\n".join(_panel_block(p) for p in rest)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<header class="topbar">
  <div class="brand">SPLUNK</div>
  <div class="title">{_esc(title)}</div>
  <div class="meta">{_esc(kind)} · {_esc(theme)} · {len(panels)} panels · generated mockup</div>
</header>
{_control_strip(inputs)}
<main class="page">
{f'<div class="grid">{kpi_html}</div>' if kpi_html else ''}
<div class="stack" style="margin-top:12px">
{rest_html}
</div>
<footer class="footer">Generated by mockup_from_inventory.py. Open via <code>open index.html</code>.</footer>
</main>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inventory", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="mockup/ directory")
    args = ap.parse_args()
    inv = json.loads(args.inventory.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    html_path = args.out / "index.html"
    html_path.write_text(render(inv), encoding="utf-8")
    readme = args.out / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Splunk mockup\n\n```bash\nopen index.html\n```\n",
            encoding="utf-8",
        )
    n = len(inv.get("panels") or [])
    ni = len(inv.get("inputs") or [])
    print(f"Wrote {html_path} panels={n} inputs={ni}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
