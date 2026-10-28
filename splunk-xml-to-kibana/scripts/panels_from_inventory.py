#!/usr/bin/env python3
"""Stub esql/panels.json (+ fields.json skeleton) from inventory.

Agent fills real ES|QL; stubs are valid for build_from_catalog.

Usage:
  python panels_from_inventory.py \\
    --inventory PACKAGE_ROOT/inventory.json \\
    --out PACKAGE_ROOT/esql/panels.json \\
    --dash-id bx-example --stream 'logs-bx.example-*'
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def _sid(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_").lower()
    return s or "panel"


def _viz_map(panel: dict) -> tuple[str, str]:
    """Return (catalog_viz, layer_or_empty)."""
    chart = (panel.get("chart_type") or "").lower()
    viz = (panel.get("viz") or "").lower()
    custom = (panel.get("custom_viz") or "").lower()
    blob = f"{chart} {viz} {custom}"

    if "sankey" in blob:
        return "vega_ref", ""
    if "image" in blob or "html" in blob:
        return "markdown", ""
    if "single" in blob or chart == "singlevalue":
        return "metric", ""
    if "gauge" in blob:
        return "gauge", ""
    if "punch" in blob or "heatmap" in blob:
        return "heatmap", ""
    if "area" in blob:
        return "xy", "area"
    if "line" in blob:
        return "xy", "line"
    if "bar" in blob or "column" in blob:
        return "xy", "bar"
    if "table" in blob or not chart:
        return "data_table", ""
    return "data_table", ""


class GridPacker:
    def __init__(self) -> None:
        self.y = 0
        self.x = 0
        self.row_h = 0

    def place(self, w: int, h: int) -> dict:
        if self.x + w > 48:
            self.y += self.row_h
            self.x = 0
            self.row_h = 0
        g = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        if self.x >= 48:
            self.y += self.row_h
            self.x = 0
            self.row_h = 0
        return g


def stub_panel(panel: dict, stream: str, dash_id: str, packer: GridPacker) -> dict:
    pid = _sid(panel.get("id") or panel.get("title") or "panel")
    title = panel.get("title") or pid
    viz, layer = _viz_map(panel)

    if viz == "markdown":
        return {
            "id": pid,
            "title": title,
            "viz": "markdown",
            "content": f"**GAP:** Splunk `{panel.get('viz') or panel.get('chart_type')}` asset not ported.",
            "grid": packer.place(48, 3),
        }

    if viz == "vega_ref":
        ref = f"{dash_id}-vega-sankey" if dash_id else f"{pid}-vega-sankey"
        return {
            "id": pid,
            "title": title,
            "viz": "vega_ref",
            "ref_id": ref,
            "grid": packer.place(28, 14),
        }

    if viz == "metric":
        return {
            "id": pid,
            "title": title,
            "viz": "metric",
            "esql": f"FROM {stream}\n| STATS count = COUNT(*)",
            "metrics": ["count"],
            "grid": packer.place(8, 5),
        }

    if viz == "gauge":
        return {
            "id": pid,
            "title": title,
            "viz": "gauge",
            "esql": f"FROM {stream}\n| STATS count = COUNT(*)",
            "metrics": ["count"],
            "grid": packer.place(8, 6),
        }

    if viz == "heatmap":
        return {
            "id": pid,
            "title": title,
            "viz": "heatmap",
            "esql": (
                f"FROM {stream}\n"
                f'| STATS count = COUNT(*) BY x = TO_STRING(@timestamp), y = "stub"\n'
                f"| LIMIT 20"
            ),
            "x": "x",
            "y": "y",
            "metrics": ["count"],
            "grid": packer.place(24, 12),
        }

    if viz == "xy":
        bucket = "BUCKET(@timestamp, 75, ?_tstart, ?_tend)"
        return {
            "id": pid,
            "title": title,
            "viz": "xy",
            "layer": layer or "bar",
            "esql": (
                f"FROM {stream}\n"
                f"| WHERE @timestamp <= ?_tend AND @timestamp > ?_tstart\n"
                f"| STATS count = COUNT(*) BY {bucket}\n"
                f"| SORT `{bucket}` ASC"
            ),
            "x": bucket,
            "y": "count",
            "axis": {
                "x": {"scale": "temporal", "domain": {"type": "fit", "rounding": False}}
            },
            "grid": packer.place(24, 10),
        }

    # data_table
    return {
        "id": pid,
        "title": title,
        "viz": "data_table",
        "esql": (
            f"FROM {stream}\n"
            f"| WHERE @timestamp <= ?_tend AND @timestamp > ?_tstart\n"
            f"| KEEP @timestamp\n"
            f"| SORT @timestamp DESC\n"
            f"| LIMIT 25"
        ),
        "rows": ["@timestamp"],
        "metrics": [],
        "grid": packer.place(24, 10),
    }


def stub_controls(inputs: list[dict]) -> list[dict]:
    pinned = []
    for inp in inputs:
        itype = (inp.get("type") or "").lower()
        token = inp.get("token") or inp.get("id") or "token"
        if "time" in itype and "bucket" not in token.lower():
            # dashboard time_range covers timerange inputs
            continue
        label = inp.get("label") or token
        opts = ["*"]
        # dropdown-ish
        if "dropdown" in itype or "multiselect" in itype or "text" in itype:
            pinned.append(
                {
                    "type": "esql_control",
                    "id": f"ctl_{_sid(token)}",
                    "width": "medium",
                    "grow": False,
                    "config": {
                        "title": label,
                        "variable_name": _sid(token).replace("-", "_"),
                        "variable_type": "values",
                        "control_type": "STATIC_VALUES",
                        "available_options": opts,
                        "selected_options": opts[:1],
                        "single_select": True,
                    },
                }
            )
    return pinned


def fields_skeleton(inv: dict, stream: str) -> dict:
    """Heuristic field list from query heads — agent edits values."""
    guessed: dict[str, dict] = {}
    for p in inv.get("panels") or []:
        head = (p.get("query_head") or "") + " " + (p.get("query_tail") or "")
        for m in re.finditer(
            r"\b(rule\.name|user\.name|user\.email|source\.ip|destination\.ip|"
            r"event\.action|event\.outcome|host\.name|closure\.code|closure\.label|"
            r"signal_to_noise|search_name|status)\b",
            head,
        ):
            key = m.group(1)
            if key not in guessed:
                if key.endswith(".ip"):
                    guessed[key] = {
                        "type": "enum",
                        "values": ["10.0.0.10", "10.0.0.20"],
                    }
                elif "noise" in key or "score" in key:
                    guessed[key] = {"type": "float", "min": 1.0, "max": 10.0}
                else:
                    guessed[key] = {
                        "type": "enum",
                        "values": [f"demo_{key.split('.')[-1]}_a", f"demo_{key.split('.')[-1]}_b"],
                    }
    if not guessed:
        guessed = {
            "event.action": {"type": "enum", "values": ["alert", "login", "denied"]},
            "user.email": {
                "type": "enum",
                "values": ["demo.user@example.com", "other@example.com"],
            },
        }
    # stream name without wildcard for data_stream write target
    stream_write = stream.replace("-*", "-default").replace("*", "default")
    if stream_write.endswith("-"):
        stream_write += "default"
    return {
        "stream": stream_write,
        "n": 500,
        "seed": 42,
        "days": 14,
        "fields": guessed,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inventory", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True, help="esql/panels.json")
    ap.add_argument("--dash-id", default="")
    ap.add_argument("--stream", default="logs-demo.events-*")
    ap.add_argument(
        "--fields-out",
        type=Path,
        help="Write fields.json skeleton (default: PACKAGE/data_factories/fields.json)",
    )
    ap.add_argument("--force", action="store_true", help="Overwrite existing panels.json")
    args = ap.parse_args()

    if args.out.exists() and not args.force:
        raise SystemExit(f"{args.out} exists — pass --force to overwrite stub")

    inv = json.loads(args.inventory.read_text(encoding="utf-8"))
    dash_id = args.dash_id or (inv.get("dashboard_id") or "dashboard").replace("_", "-")
    title = inv.get("title") or dash_id
    packer = GridPacker()

    panels = []
    empty_esql = 0
    sankey = 0
    for p in inv.get("panels") or []:
        stub = stub_panel(p, args.stream, dash_id, packer)
        if stub.get("viz") == "vega_ref":
            sankey += 1
            empty_esql += 1
        elif stub.get("viz") == "markdown":
            empty_esql += 1
        panels.append(stub)

    pinned = stub_controls(inv.get("inputs") or [])
    catalog = {
        "dashboard": {
            "id": dash_id,
            "title": title,
            "description": f"Stub catalog from inventory ({inv.get('kind') or 'Splunk'})",
            "time_range": {"from": "now-30d", "to": "now"},
        },
        "pinned_panels": pinned,
        "panels": panels,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")

    fields_path = args.fields_out
    if fields_path is None:
        # inventory parent = PACKAGE_ROOT
        fields_path = args.inventory.parent / "data_factories" / "fields.json"
    fields_path.parent.mkdir(parents=True, exist_ok=True)
    if not fields_path.exists() or args.force:
        fields_path.write_text(
            json.dumps(fields_skeleton(inv, args.stream), indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {fields_path}")

    print(
        f"stubbed panels={len(panels)} controls={len(pinned)} "
        f"empty_esql={empty_esql} sankey={sankey}"
    )
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
