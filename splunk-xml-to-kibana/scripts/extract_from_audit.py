#!/usr/bin/env python3
"""Extract Splunk Classic/Studio eai:data from an audit export JSON.

Writes PACKAGE_ROOT/source.xml. Does not print XML body (token-safe).

Usage:
  python extract_from_audit.py \\
    --audit dashboard_to_migrate.json \\
    --name secops_detection_engineering_-_studio \\
    --out-dir ./elastic/my_dash
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def _load_items(audit: Path) -> list[dict]:
    data = json.loads(audit.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("dashboards", "results", "entry", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    raise SystemExit(f"Unrecognized audit JSON shape in {audit}")


def _names(d: dict) -> list[str]:
    out: list[str] = []
    for k in (
        "dashboard",
        "name",
        "title",
        "ko_title",
        "view_name",
        "view_label",
        "label",
        "id",
    ):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    # nested content
    content = d.get("content") if isinstance(d.get("content"), dict) else {}
    for k in ("label", "name", "title"):
        v = content.get(k)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    eai = d.get("eai:data") or ""
    if isinstance(eai, str) and eai:
        m = re.search(r"<label>([^<]+)</label>", eai)
        if m:
            out.append(m.group(1).strip())
    return out


def find_dashboard(items: list[dict], name: str) -> dict:
    needle = name.strip().lower()
    exact: list[dict] = []
    partial: list[dict] = []
    for d in items:
        if not (d.get("eai:data") or "").strip():
            continue
        names = _names(d)
        lowered = [n.lower() for n in names]
        if any(n == needle for n in lowered):
            exact.append(d)
        elif any(needle in n or n in needle for n in lowered):
            partial.append(d)
    hits = exact or partial
    if not hits:
        raise SystemExit(f"No dashboard matching --name {name!r} in audit")
    if len(hits) > 1 and exact:
        # prefer exact unique
        if len(exact) == 1:
            return exact[0]
    if len(hits) > 1:
        labels = [_names(h)[:2] for h in hits[:5]]
        raise SystemExit(f"Ambiguous --name {name!r}; matches={labels}")
    return hits[0]


def detect_kind(eai: str) -> str:
    head = eai[:800]
    if 'version="2"' in head or "<definition>" in eai[:2000]:
        return "Studio"
    if "<form" in head:
        return "Classic"
    if "<dashboard" in head:
        return "Classic"
    return "Unknown"


def title_from_eai(eai: str) -> str:
    try:
        root = ET.fromstring(eai)
        label = root.find("label")
        if label is not None and (label.text or "").strip():
            return label.text.strip()
    except ET.ParseError:
        pass
    m = re.search(r"<label>([^<]+)</label>", eai)
    return m.group(1).strip() if m else "Untitled"


def extract(audit: Path, name: str, out_dir: Path) -> Path:
    items = _load_items(audit)
    d = find_dashboard(items, name)
    eai = d["eai:data"]
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "source.xml"
    dest.write_text(eai, encoding="utf-8")
    kind = detect_kind(eai)
    title = title_from_eai(eai)
    print(f"extracted kind={kind} title={title!r} bytes={len(eai)} → {dest}")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audit", type=Path, required=True)
    ap.add_argument("--name", required=True, help="dashboard / view / label match")
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args()
    extract(args.audit, args.name, args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
