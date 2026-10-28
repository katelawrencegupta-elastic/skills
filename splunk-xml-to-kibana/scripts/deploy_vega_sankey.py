#!/usr/bin/env python3
"""Create/overwrite a classic Kibana Vega Sankey visualization (saved object).

Usage:
  python deploy_vega_sankey.py --viz-id bx-example-vega-sankey --title "Flow" \\
    --stream logs-bx.notable-* --src rule.name --dst closure.code \\
    --kibana-url "$KIBANA_URL" --api-key "$ELASTICSEARCH_API_KEY"

  python deploy_vega_sankey.py --viz-id … --title … --esql-file query.esql …
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "templates" / "vega"))
from sankey import edge_esql, visualization_attributes  # noqa: E402


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--viz-id", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--esql", default="", help="Inline ES|QL (src/dst/count aliases)")
    ap.add_argument("--esql-file", type=Path)
    ap.add_argument("--stream", default="")
    ap.add_argument("--src", default="src")
    ap.add_argument("--dst", default="dst")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--kibana-url", default=os.environ.get("KIBANA_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("ELASTICSEARCH_API_KEY", ""))
    args = ap.parse_args()

    kb = args.kibana_url or es_url_to_kb(os.environ.get("ELASTICSEARCH_URL", ""))
    key = args.api_key
    if not kb or not key:
        print("Need --kibana-url / KIBANA_URL and --api-key", file=sys.stderr)
        return 2

    if args.esql_file:
        esql = args.esql_file.read_text(encoding="utf-8").strip()
    elif args.esql:
        esql = args.esql.strip()
    elif args.stream:
        esql = edge_esql(args.stream, args.src, args.dst, args.limit)
    else:
        raise SystemExit("Need --esql, --esql-file, or --stream + --src/--dst")

    body = {"attributes": visualization_attributes(args.title, esql)}
    st, resp = kb_request(
        kb, key, "POST", f"/api/saved_objects/visualization/{args.viz_id}?overwrite=true", body
    )
    print(f"vega {args.viz_id}: {st}")
    if st >= 300:
        raise SystemExit(resp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
