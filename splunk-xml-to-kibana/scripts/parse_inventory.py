#!/usr/bin/env python3
"""Parse Splunk Classic Simple XML or Dashboard Studio XML → inventory + queries.

Writes (under --out-dir):
  inventory.json       compact panel/input inventory
  splunk_queries.json  full SPL per panel (offline; do not paste into chat)
  source.xml           copy of input when --copy-source

Usage:
  python parse_inventory.py --source dashboard.xml --out-dir ./elastic/my_dash
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path


def _text(el: ET.Element | None, default: str = "") -> str:
    if el is None or el.text is None:
        return default
    return el.text.strip()


def _query_from_panel(panel: ET.Element) -> str:
    """Classic nests <search><query> under table/chart/single, not only under panel."""
    for tag in ("table", "chart", "single", "viz", "event", "map", "search"):
        el = panel.find(tag)
        if el is None:
            continue
        search = el if tag == "search" else el.find("search")
        if search is None:
            continue
        q = search.find("query")
        if q is not None and (q.text or "").strip():
            return (q.text or "").strip()
    return ""


def parse_classic(root: ET.Element) -> dict:
    title = _text(root.find("label"), "Untitled")
    theme = root.attrib.get("theme", "")
    inputs: list[dict] = []
    for inp in root.findall(".//input"):
        token = inp.attrib.get("token") or inp.attrib.get("searchWhenChangedToken") or ""
        itype = inp.attrib.get("type", "text")
        label = _text(inp.find("label"), token)
        default_el = inp.find("default")
        if itype == "time" and default_el is not None:
            earliest = _text(default_el.find("earliest"))
            latest = _text(default_el.find("latest"))
            default = f"<earliest>{earliest}</earliest> <latest>{latest}</latest>"
        else:
            default = _text(default_el)
        choices = inp.findall("choice")
        inputs.append(
            {
                "token": token,
                "type": itype,
                "label": label,
                "default": default,
                "n_choices": len(choices),
            }
        )

    panels: list[dict] = []
    queries: dict[str, str] = {}
    for ri, row in enumerate(root.findall(".//row")):
        for pi, panel in enumerate(row.findall("panel")):
            title_p = _text(panel.find("title"), f"row{ri}_panel{pi}")
            viz = "table"
            chart_type = ""
            custom_viz = ""
            if panel.find("single") is not None:
                viz = "single"
            elif panel.find("chart") is not None:
                viz = "chart"
                chart = panel.find("chart")
                opt = chart.find("option[@name='charting.chart']") if chart is not None else None
                chart_type = _text(opt) if opt is not None else ""
                # also <option name="charting.chart"> inside chart
                if not chart_type and chart is not None:
                    for o in chart.findall("option"):
                        if o.attrib.get("name") == "charting.chart":
                            chart_type = _text(o)
            elif panel.find("viz") is not None:
                viz = "custom"
                v = panel.find("viz")
                custom_viz = (v.attrib.get("type") if v is not None else "") or ""
            elif panel.find("table") is not None:
                viz = "table"
            elif panel.find("html") is not None or panel.find("markdown") is not None:
                viz = "html"

            q = _query_from_panel(panel)
            key = f"r{ri}_p{pi}"
            queries[key] = q
            head = q[:140].replace("\n", " ")
            tail = q[-140:].replace("\n", " ") if len(q) > 140 else q.replace("\n", " ")
            panels.append(
                {
                    "row": ri,
                    "panel": pi,
                    "id": key,
                    "title": title_p,
                    "viz": viz,
                    "chart_type": chart_type,
                    "custom_viz": custom_viz,
                    "depends": "",
                    "rejects": "",
                    "query_len": len(q),
                    "query_head": head,
                    "query_tail": tail,
                }
            )

    dash_id = re.sub(r"[^a-z0-9_]+", "_", title.lower()).strip("_")
    return {
        "kind": "Classic form" if root.tag == "form" else "Classic dashboard",
        "title": title,
        "theme": theme,
        "dashboard_id": dash_id,
        "inputs": inputs,
        "panels": panels,
        "queries": queries,
    }


def parse_studio(xml_text: str, root: ET.Element) -> dict:
    """Dashboard Studio: definition CDATA JSON inside XML wrapper."""
    m = re.search(r"<definition><!\[CDATA\[(.*?)\]\]></definition>", xml_text, re.S)
    if not m:
        # sometimes definition is a child text node
        defn = root.find(".//definition")
        raw = (defn.text or "").strip() if defn is not None else ""
        if not raw:
            raise SystemExit("Studio XML missing <definition> CDATA JSON")
    else:
        raw = m.group(1)
    data = json.loads(raw)
    # Studio JSON title; fall back to XML <label>
    title = (
        data.get("title")
        or _text(root.find("label"))
        or data.get("definition", {}).get("title")
        or "Untitled"
    )
    layout = data.get("layout") or {}
    tabs_items = ((layout.get("tabs") or {}).get("items")) or []
    tabs = [{"id": t.get("id"), "label": t.get("label")} for t in tabs_items]
    viz = data.get("visualizations") or {}
    data_sources = data.get("dataSources") or {}
    inputs_raw = data.get("inputs") or {}
    inputs = []
    for iid, inp in inputs_raw.items():
        opts = inp.get("options") or {}
        token = opts.get("token") or iid
        inputs.append(
            {
                "token": token,
                "id": iid,
                "type": (inp.get("type") or inp.get("inputType") or "text"),
                "label": (inp.get("title") or opts.get("title") or token),
                "default": str(
                    (inp.get("defaults") or {}).get("value")
                    or opts.get("defaultValue")
                    or inp.get("default")
                    or ""
                ),
                "n_choices": len(opts.get("items") or opts.get("choices") or []),
            }
        )

    def _query_from_ds_ref(ref) -> str:
        """Resolve Studio dataSources.primary (string id or inline dict)."""
        if isinstance(ref, str):
            ds_obj = data_sources.get(ref) or {}
            opts = ds_obj.get("options") or {}
            return (opts.get("query") or ds_obj.get("query") or "").strip()
        if isinstance(ref, dict):
            opts = ref.get("options") or {}
            nested = ref.get("dataSource") if isinstance(ref.get("dataSource"), dict) else {}
            return (
                opts.get("query")
                or ref.get("query")
                or nested.get("query")
                or ""
            ).strip()
        return ""

    panels: list[dict] = []
    queries: dict[str, str] = {}
    # Walk layoutDefinitions structure if present
    layouts = data.get("layoutDefinitions") or layout.get("layoutDefinitions") or {}
    idx = 0
    structures = []
    if layouts:
        for lid, ldef in layouts.items():
            structures.append((lid, (ldef or {}).get("structure") or []))
    elif tabs_items:
        for t in tabs_items:
            structures.append((t.get("id"), t.get("layout") or []))
    else:
        structures.append(("main", layout.get("structure") or []))

    for tab_id, structure in structures:
        for item in structure:
            vid = item.get("item") or item.get("id")
            if not vid or vid not in viz:
                continue
            v = viz[vid]
            ds = v.get("dataSources") or {}
            primary = ds.get("primary")
            q = _query_from_ds_ref(primary)
            if not q and isinstance(v.get("search"), dict):
                q = (v["search"].get("query") or "").strip()
            key = vid
            queries[key] = q
            vtype = v.get("type") or "table"
            chart_type = ""
            if isinstance(vtype, str) and vtype.startswith("splunk."):
                chart_type = vtype.split(".", 1)[-1]
            panels.append(
                {
                    "row": idx,
                    "panel": 0,
                    "id": key,
                    "tab": tab_id,
                    "title": v.get("title") or key,
                    "viz": vtype,
                    "chart_type": chart_type,
                    "custom_viz": vtype if "viz." in str(vtype) else "",
                    "depends": "",
                    "rejects": "",
                    "query_len": len(q),
                    "query_head": q[:140].replace("\n", " "),
                    "query_tail": (q[-140:] if len(q) > 140 else q).replace("\n", " "),
                }
            )
            idx += 1

    # Orphan visualizations not placed in layout (still inventory them)
    placed = {p["id"] for p in panels}
    for vid, v in viz.items():
        if vid in placed:
            continue
        ds = v.get("dataSources") or {}
        q = _query_from_ds_ref(ds.get("primary"))
        queries[vid] = q
        vtype = v.get("type") or "table"
        chart_type = vtype.split(".", 1)[-1] if isinstance(vtype, str) and "." in vtype else ""
        panels.append(
            {
                "row": idx,
                "panel": 0,
                "id": vid,
                "tab": "unplaced",
                "title": v.get("title") or vid,
                "viz": vtype,
                "chart_type": chart_type,
                "custom_viz": vtype if "viz." in str(vtype) else "",
                "depends": "",
                "rejects": "",
                "query_len": len(q),
                "query_head": q[:140].replace("\n", " "),
                "query_tail": (q[-140:] if len(q) > 140 else q).replace("\n", " "),
            }
        )
        idx += 1

    return {
        "kind": "Studio",
        "title": title,
        "theme": data.get("theme") or root.attrib.get("theme", ""),
        "dashboard_id": re.sub(r"[^a-z0-9_]+", "_", str(title).lower()).strip("_"),
        "tabs": tabs,
        "inputs": inputs,
        "panels": panels,
        "queries": queries,
    }


def parse_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    # Studio marker
    if 'version="2"' in text[:500] or "<definition>" in text and "visualizations" in text:
        root = ET.fromstring(text)
        return parse_studio(text, root)
    root = ET.fromstring(text)
    if root.tag in ("form", "dashboard"):
        return parse_classic(root)
    # try nested
    for tag in ("form", "dashboard"):
        el = root.find(f".//{tag}")
        if el is not None:
            return parse_classic(el)
    raise SystemExit(f"Unrecognized Splunk XML root: {root.tag}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, help="Classic or Studio XML")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument(
        "--from-audit",
        type=Path,
        help="Audit JSON (dashboard_to_migrate.json); requires --name",
    )
    ap.add_argument(
        "--name",
        default="",
        help="Dashboard/view/label match when using --from-audit",
    )
    ap.add_argument("--copy-source", action="store_true", default=True)
    ap.add_argument("--no-copy-source", action="store_true")
    args = ap.parse_args()

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)

    source = args.source
    if args.from_audit:
        if not args.name:
            raise SystemExit("--from-audit requires --name")
        # Import sibling without package install
        import extract_from_audit as _extract  # type: ignore

        source = _extract.extract(args.from_audit, args.name, out)
    elif not source:
        raise SystemExit("Need --source or --from-audit + --name")

    parsed = parse_file(source)
    queries = parsed.pop("queries")

    inv_path = out / "inventory.json"
    inv_path.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")

    q_path = out / "splunk_queries.json"
    q_path.write_text(json.dumps(queries, indent=2) + "\n", encoding="utf-8")

    if args.copy_source and not args.no_copy_source:
        dest = out / "source.xml"
        src = source.resolve()
        if src != dest.resolve():
            shutil.copy2(source, dest)

    n = len(parsed.get("panels") or [])
    ni = len(parsed.get("inputs") or [])
    print(f"kind={parsed.get('kind')} title={parsed.get('title')!r}")
    print(f"panels={n} inputs={ni}")
    print(f"Wrote {inv_path}")
    print(f"Wrote {q_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
