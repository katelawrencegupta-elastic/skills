#!/usr/bin/env python3
"""Build Kibana dashboard JSON from esql/panels.json catalog.

Usage:
  python build_from_catalog.py --catalog PACKAGE_ROOT/esql/panels.json --out PACKAGE_ROOT/kibana
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _grid(p: dict, default_y: int) -> dict:
    g = p.get("grid") or {}
    return {
        "x": int(g.get("x", 0)),
        "y": int(g.get("y", default_y)),
        "w": int(g.get("w", 48)),
        "h": int(g.get("h", 8)),
    }


def panel_to_api(p: dict, auto_y: int) -> dict:
    viz = p.get("viz") or "data_table"
    pid = p["id"]
    title = p.get("title") or pid
    grid = _grid(p, auto_y)

    if viz == "markdown":
        return {
            "type": "markdown",
            "id": pid,
            "grid": grid,
            "config": {"content": p.get("content") or f"### {title}"},
        }

    if viz == "links":
        return {
            "type": "links",
            "id": pid,
            "grid": grid,
            "config": {
                "title": title,
                "links": p.get("links") or [],
            },
        }

    if viz == "vega_ref":
        return {
            "type": "vis",
            "id": pid,
            "grid": grid,
            "config": {
                "title": title,
                "ref_id": p.get("ref_id") or pid,
            },
        }

    esql = p.get("esql") or ""
    if not esql and viz not in ("markdown", "links"):
        raise SystemExit(f"panel {pid}: missing esql")

    if viz == "metric":
        cols = p.get("metrics") or ["count"]
        return {
            "type": "vis",
            "id": pid,
            "grid": grid,
            "config": {
                "title": title,
                "type": "metric",
                "data_source": {"type": "esql", "query": esql},
                "metrics": [{"type": "primary", "column": cols[0]}],
            },
        }

    if viz == "gauge":
        col = (p.get("metrics") or ["count"])[0]
        return {
            "type": "vis",
            "id": pid,
            "grid": grid,
            "config": {
                "title": title,
                "type": "gauge",
                "data_source": {"type": "esql", "query": esql},
                "metric": {"column": col},
            },
        }

    if viz == "heatmap":
        return {
            "type": "vis",
            "id": pid,
            "grid": grid,
            "config": {
                "title": title,
                "type": "heatmap",
                "data_source": {"type": "esql", "query": esql},
                "x": {"column": p.get("x") or "x"},
                "y": {"column": p.get("y") or "y"},
                "metric": {"column": (p.get("metrics") or ["count"])[0]},
            },
        }

    if viz == "xy":
        layer = p.get("layer") or "bar"
        layer_cfg: dict = {
            "type": layer,
            "data_source": {"type": "esql", "query": esql},
            "x": {"column": p.get("x") or "x"},
            "y": [{"column": p.get("y") or (p.get("metrics") or ["count"])[0]}],
        }
        if p.get("breakdown_by"):
            layer_cfg["breakdown_by"] = {"column": p["breakdown_by"]}
        cfg: dict = {
            "title": title,
            "type": "xy",
            "layers": [layer_cfg],
        }
        if p.get("axis"):
            cfg["axis"] = p["axis"]
        return {
            "type": "vis",
            "id": pid,
            "grid": grid,
            "config": cfg,
        }

    # data_table default — omit empty metrics (Kibana 400: reading 'type' of undefined)
    rows = p.get("rows") or []
    metrics = p.get("metrics") or []
    cfg: dict = {
        "title": title,
        "type": "data_table",
        "data_source": {"type": "esql", "query": esql},
        "rows": [{"column": c} for c in rows],
    }
    if metrics:
        cfg["metrics"] = [{"column": c} for c in metrics]
    return {
        "type": "vis",
        "id": pid,
        "grid": grid,
        "config": cfg,
    }


def build(catalog: dict) -> dict:
    dash = catalog["dashboard"]
    panels_in = catalog.get("panels") or []
    api_panels = []
    y = 0
    for p in panels_in:
        g = p.get("grid")
        if not g:
            p = {**p, "grid": {"x": 0, "y": y, "w": 48, "h": 8}}
        panel = panel_to_api(p, y)
        api_panels.append(panel)
        gg = panel["grid"]
        y = max(y, gg["y"] + gg["h"])

    out = {
        "id": dash["id"],
        "title": dash["title"],
        "description": dash.get("description") or "",
        "time_range": dash.get("time_range") or {"from": "now-24h", "to": "now"},
        "panels": api_panels,
    }
    if catalog.get("pinned_panels"):
        out["pinned_panels"] = catalog["pinned_panels"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="kibana/ directory")
    ap.add_argument("--bundle-name", default="", help="optional bundle filename stem")
    args = ap.parse_args()

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    dash = build(catalog)
    args.out.mkdir(parents=True, exist_ok=True)
    single = args.out / f"{dash['id']}.json"
    single.write_text(json.dumps(dash, indent=2) + "\n", encoding="utf-8")
    stem = args.bundle_name or f"{dash['id'].replace('-', '_')}_dashboards"
    bundle = args.out / f"{stem}.json"
    bundle.write_text(
        json.dumps({"dashboards": [dash], "source": "build_from_catalog"}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {single}")
    print(f"Wrote {bundle} panels={len(dash['panels'])} controls={len(dash.get('pinned_panels') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
