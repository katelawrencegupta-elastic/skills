#!/usr/bin/env python3
"""Draft MAPPING.md from inventory + panels catalog (+ optional gaps.yaml).

Usage:
  python render_mapping.py \\
    --inventory PACKAGE_ROOT/inventory.json \\
    --catalog PACKAGE_ROOT/esql/panels.json \\
    --out PACKAGE_ROOT/MAPPING.md \\
    [--gaps PACKAGE_ROOT/gaps.yaml] \\
    [--queries PACKAGE_ROOT/splunk_queries.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load_gaps(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    # minimal YAML subset: list of {item, reason} as JSON or simple lines
    if text.strip().startswith("["):
        return json.loads(text)
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if isinstance(data, list):
            return data
        return data.get("gaps") or []
    except Exception:
        gaps = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- ") and ":" in line:
                item, _, reason = line[2:].partition(":")
                gaps.append({"item": item.strip(), "reason": reason.strip()})
        return gaps


def render(
    inv: dict,
    catalog: dict,
    queries: dict | None,
    gaps: list[dict],
    package_path: str,
) -> str:
    title = inv.get("title") or catalog.get("dashboard", {}).get("title") or "Dashboard"
    dash_id = catalog.get("dashboard", {}).get("id") or inv.get("dashboard_id") or "unknown"
    inputs = inv.get("inputs") or []
    panels_inv = inv.get("panels") or []
    panels_cat = {p["id"]: p for p in (catalog.get("panels") or []) if p.get("id")}
    pinned = catalog.get("pinned_panels") or []

    lines: list[str] = []
    lines.append(f"# {title} — Splunk → Elastic mapping")
    lines.append("")
    lines.append(f"**Splunk source:** `{inv.get('kind', '')}` / `{inv.get('dashboard_id', '')}`")
    lines.append(f"**Elastic target:** `{dash_id}`")
    lines.append(f"**Package:** `{package_path}`")
    lines.append(f"**Splunk mockup:** `{package_path}/mockup/index.html`")
    lines.append("")
    lines.append("## 0. Audit / DIMA JSON used")
    lines.append("")
    lines.append("| JSON / XML file | Used? | How |")
    lines.append("|-----------------|:-----:|-----|")
    lines.append("| `source.xml` / inventory | Yes | Parsed offline |")
    lines.append("")
    lines.append("## 0b. Data factories")
    lines.append("")
    lines.append("| Script | Role |")
    lines.append("|--------|------|")
    lines.append("| `data_factories/generate_*.py` | Fill from conversion |")
    lines.append("")
    lines.append("## 1. Inputs → Controls matrix (required)")
    lines.append("")
    lines.append("| Splunk input / token | Elastic Control / time / GAP | Drives (`?param`) | Why |")
    lines.append("|----------------------|------------------------------|-------------------|-----|")
    ctl_by_var = {}
    for pp in pinned:
        cfg = (pp.get("config") or {})
        ctl_by_var[cfg.get("variable_name") or ""] = cfg.get("title") or pp.get("id")
    for inp in inputs:
        token = inp.get("token") or ""
        itype = inp.get("type") or ""
        if itype == "time":
            lines.append(
                f"| `{token}` (time) | Dashboard time picker | `?_tstart` / `?_tend` | Native time |"
            )
        elif token in ctl_by_var:
            lines.append(
                f"| `{token}` | Control `{ctl_by_var[token]}` | `?{token}` | Catalog pinned_panels |"
            )
        else:
            lines.append(f"| `{token}` | **GAP** | — | Not wired in catalog yet |")
    if not inputs:
        lines.append("| _(none)_ | time picker | `?_tstart` / `?_tend` | |")
    lines.append("")
    lines.append("## 2. Knowledge objects")
    lines.append("")
    lines.append("_(Fill lookups / macros / datamodels as needed.)_")
    lines.append("")
    lines.append("## 3. Panel-by-panel mapping")
    lines.append("")

    # Prefer catalog order; fall back to inventory titles
    ordered = catalog.get("panels") or []
    if not ordered:
        ordered = [{"id": p.get("id"), "title": p.get("title"), "viz": p.get("viz")} for p in panels_inv]

    for p in ordered:
        pid = p.get("id") or "panel"
        title_p = p.get("title") or pid
        viz = p.get("viz") or ""
        esql = p.get("esql") or ""
        qkey = pid
        spl = ""
        if queries:
            spl = queries.get(qkey) or queries.get(str(pid)) or ""
            if not spl:
                # try inventory id match
                for inv_p in panels_inv:
                    if inv_p.get("title") == title_p:
                        spl = queries.get(inv_p.get("id") or "", "")
                        break
        lines.append(f"### {title_p} — `{pid}`")
        lines.append("")
        lines.append("**SPL**")
        lines.append("")
        lines.append("```spl")
        lines.append((spl[:2000] + ("…" if len(spl) > 2000 else "")) if spl else "# (see splunk_queries.json)")
        lines.append("```")
        lines.append("")
        lines.append("**ES|QL**")
        lines.append("")
        lines.append("```esql")
        lines.append(esql or "# (none — markdown/links/vega_ref)")
        lines.append("```")
        lines.append("")
        lines.append(f"**Viz:** → `{viz}`")
        lines.append("**Why:** _(agent: fill non-obvious rationale)_")
        lines.append("")

    lines.append("## 4. Data layout")
    lines.append("")
    lines.append("| Elastic resource | Feeds panels |")
    lines.append("|------------------|--------------|")
    lines.append("| _(stream)_ | _(panels)_ |")
    lines.append("")
    lines.append("## 5. Gaps / non-goals")
    lines.append("")
    lines.append("| Item | Reason |")
    lines.append("|------|--------|")
    if gaps:
        for g in gaps:
            lines.append(f"| {g.get('item', '')} | {g.get('reason', '')} |")
    else:
        lines.append("| _(none listed)_ | |")
    lines.append("")
    lines.append("## 6. Links")
    lines.append("")
    lines.append(f"- Mockup: `{package_path}/mockup/index.html`")
    lines.append(f"- Catalog: `{package_path}/esql/panels.json`")
    lines.append(f"- Queries: `{package_path}/splunk_queries.json`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inventory", type=Path, required=True)
    ap.add_argument("--catalog", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--gaps", type=Path, default=None)
    ap.add_argument("--queries", type=Path, default=None)
    ap.add_argument("--package-path", default="")
    args = ap.parse_args()

    inv = json.loads(args.inventory.read_text(encoding="utf-8"))
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    queries = None
    if args.queries and args.queries.exists():
        queries = json.loads(args.queries.read_text(encoding="utf-8"))
    gaps = _load_gaps(args.gaps)
    pkg = args.package_path or str(args.out.parent)
    text = render(inv, catalog, queries, gaps, pkg)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
