#!/usr/bin/env python3
"""Rewire Dashboards API lens refs → classic visualization refs for Vega panels.

After PUT /api/dashboards/{id} with config.ref_id, Kibana often stores the
reference as saved-object type `lens`. Classic Vega objects are type
`visualization`, so the UI shows "visualization type not found" until rewired.

Map file JSON:
  {
    "panel_index_or_id": {
      "viz_id": "my-vega-sankey",
      "title": "Visual Representation of Flow"
    }
  }

Usage:
  python rewire_vega_refs.py --dashboard-id my-dash --map vega_map.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def es_url_to_kb(es_url: str) -> str:
    return es_url.rstrip("/").replace(".es.", ".kb.")


def so_request(kb_url: str, api_key: str, method: str, path: str, body=None):
    url = f"{kb_url.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"ApiKey {api_key}",
        "Accept": "application/json",
        "kbn-xsrf": "true",
        "x-elastic-internal-origin": "kibana",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        return e.code, payload


def rewire(kb_url: str, api_key: str, dash_id: str, vega_map: dict[str, dict]) -> None:
    st, sod = so_request(kb_url, api_key, "GET", f"/api/saved_objects/dashboard/{dash_id}")
    if st != 200:
        raise SystemExit(f"GET dashboard SO {dash_id} failed: {st} {sod}")

    attrs = sod["attributes"]
    panels = json.loads(attrs["panelsJSON"])
    viz_ids = {meta["viz_id"] for meta in vega_map.values()}

    refs = [
        r
        for r in (sod.get("references") or [])
        if r.get("id") not in viz_ids
        and not any(
            str(r.get("name") or "").startswith(f"{pid}:") for pid in vega_map
        )
    ]

    matched = 0
    for panel in panels:
        pid = panel.get("panelIndex") or (panel.get("gridData") or {}).get("i")
        meta = vega_map.get(str(pid)) or vega_map.get(pid)
        if not meta:
            continue
        matched += 1
        grid = panel.get("gridData") or {}
        ref_name = f"panel_{pid}"
        panel["type"] = "visualization"
        panel["id"] = meta["viz_id"]
        panel["panelRefName"] = ref_name
        panel["panelIndex"] = pid
        panel["gridData"] = {
            "x": grid.get("x", 0),
            "y": grid.get("y", 0),
            "w": grid.get("w", 24),
            "h": grid.get("h", 16),
            "i": pid,
        }
        panel["embeddableConfig"] = {
            "title": meta.get("title") or meta["viz_id"],
            "enhancements": {},
        }
        refs.append({"name": ref_name, "type": "visualization", "id": meta["viz_id"]})

    if matched == 0:
        raise SystemExit(
            f"No panels matched map keys {list(vega_map)} — check panelIndex values"
        )

    body = {
        "attributes": {
            **{k: v for k, v in attrs.items() if k != "panelsJSON"},
            "panelsJSON": json.dumps(panels),
        },
        "references": refs,
    }
    st, resp = so_request(
        kb_url, api_key, "PUT", f"/api/saved_objects/dashboard/{dash_id}", body
    )
    if st not in (200, 201):
        raise SystemExit(f"rewire failed: {st} {resp}")
    print(f"OK rewired {matched} Vega visualization ref(s) on {dash_id}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dashboard-id", required=True)
    ap.add_argument("--map", type=Path, required=True, help="JSON panel→viz map")
    ap.add_argument("--kibana-url", default=os.environ.get("KIBANA_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("ELASTICSEARCH_API_KEY", ""))
    args = ap.parse_args()

    kb = args.kibana_url or es_url_to_kb(os.environ.get("ELASTICSEARCH_URL", ""))
    key = args.api_key
    if not kb or not key:
        print("Need --kibana-url / KIBANA_URL and --api-key", file=sys.stderr)
        return 2

    vega_map = json.loads(args.map.read_text(encoding="utf-8"))
    rewire(kb, key, args.dashboard_id, vega_map)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
