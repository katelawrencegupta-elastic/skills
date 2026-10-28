#!/usr/bin/env python3
"""PUT a Kibana dashboard via Dashboards API 2023-10-31.

Env (optional if flags set):
  KIBANA_URL, ELASTICSEARCH_API_KEY
  ELASTICSEARCH_URL — used to derive .kb. URL when KIBANA_URL unset

Usage:
  python put_dashboard.py --body kibana/my-dash.json
  python put_dashboard.py --body dash.json --kibana-url https://….kb….elastic.cloud:443 --api-key …
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


def kb_request(kb_url: str, api_key: str, method: str, path: str, body=None):
    url = f"{kb_url.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"ApiKey {api_key}",
        "Accept": "application/json",
        "kbn-xsrf": "true",
        "x-elastic-internal-origin": "kibana",
        "Elastic-Api-Version": "2023-10-31",
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


def load_body(path: Path) -> tuple[str, dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "dashboards" in raw and isinstance(raw["dashboards"], list):
        if len(raw["dashboards"]) != 1 and not os.environ.get("DASH_ID"):
            # allow --id to select
            pass
        d = raw["dashboards"][0]
        return d["id"], d
    if "id" in raw and "panels" in raw:
        return raw["id"], raw
    if "title" in raw and "panels" in raw:
        raise SystemExit("Body has panels but no id — pass a document with top-level id")
    raise SystemExit(f"Unrecognized dashboard JSON shape in {path}")


def upsert_body(d: dict) -> dict:
    body = {
        "title": d["title"],
        "description": d.get("description") or "",
        "time_range": d.get("time_range") or {"from": "now-24h", "to": "now"},
        "panels": d["panels"],
    }
    if d.get("pinned_panels"):
        body["pinned_panels"] = d["pinned_panels"]
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--body", type=Path, required=True, help="Dashboard JSON or bundle")
    ap.add_argument("--id", help="Override dashboard id / select from bundle")
    ap.add_argument("--kibana-url", default=os.environ.get("KIBANA_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("ELASTICSEARCH_API_KEY", ""))
    ap.add_argument("--export", action="store_true", help="GET dashboard and write --out")
    ap.add_argument("--out", type=Path, help="Export destination")
    args = ap.parse_args()

    kb = args.kibana_url or es_url_to_kb(os.environ.get("ELASTICSEARCH_URL", ""))
    key = args.api_key
    if not kb or not key:
        print("Need --kibana-url (or KIBANA_URL / ELASTICSEARCH_URL) and --api-key", file=sys.stderr)
        return 2

    if args.export:
        dash_id = args.id
        if not dash_id:
            dash_id, _ = load_body(args.body)
        st, resp = kb_request(kb, key, "GET", f"/api/dashboards/{dash_id}")
        if st != 200:
            raise SystemExit(f"GET failed: {st} {resp}")
        data = resp["data"]
        out = {
            "id": dash_id,
            "title": data["title"],
            "description": data.get("description") or "",
            "time_range": data.get("time_range"),
            "panels": data.get("panels") or [],
            "pinned_panels": data.get("pinned_panels") or [],
        }
        dest = args.out or Path(f"{dash_id}.exported.json")
        dest.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {dest}")
        return 0

    raw = json.loads(args.body.read_text(encoding="utf-8"))
    dashboards = raw["dashboards"] if "dashboards" in raw else [raw]
    if args.id:
        dashboards = [d for d in dashboards if d.get("id") == args.id]
        if not dashboards:
            raise SystemExit(f"No dashboard with id={args.id}")

    for d in dashboards:
        dash_id = d["id"]
        st, resp = kb_request(kb, key, "PUT", f"/api/dashboards/{dash_id}", upsert_body(d))
        if st not in (200, 201):
            raise SystemExit(f"PUT {dash_id} failed: {st} {resp}")
        n_ctl = len(d.get("pinned_panels") or [])
        print(f"OK {dash_id} panels={len(d['panels'])} controls={n_ctl}")
        print(f"Open: {kb.rstrip('/')}/app/dashboards#/view/{dash_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
