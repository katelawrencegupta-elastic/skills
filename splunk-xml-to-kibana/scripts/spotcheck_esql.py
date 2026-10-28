#!/usr/bin/env python3
"""Run each panel ES|QL via POST {ES}/_query — one-line pass/fail.

Usage:
  python spotcheck_esql.py --catalog PACKAGE_ROOT/esql/panels.json \\
    --es-url "$ELASTICSEARCH_URL" --api-key "$ELASTICSEARCH_API_KEY"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def strip_params(esql: str) -> str:
    """Replace ?params with safe literals for headless validation."""
    q = esql
    # time params → now-24h window using ES|QL-friendly literals is hard;
    # drop time WHERE lines that only reference ?_tstart/?_tend
    q = re.sub(
        r"\|?\s*WHERE\s+@timestamp\s*<=\s*\?_tend\s+AND\s+@timestamp\s*>\s*\?_tstart\s*",
        "| ",
        q,
        flags=re.I,
    )
    q = re.sub(r"\?_tstart|\?_tend", "now()", q)
    # string-ish params
    q = re.sub(r"\?([a-zA-Z_][a-zA-Z0-9_]*)", r'"*"', q)
    # clean double pipes
    q = re.sub(r"\|\s*\|", "|", q)
    return q.strip(" |")


def es_query(es_url: str, api_key: str, query: str):
    data = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        es_url.rstrip("/") + "/_query",
        data=data,
        headers={
            "Authorization": f"ApiKey {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = raw
        return e.code, body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", type=Path, required=True)
    ap.add_argument("--es-url", default=os.environ.get("ELASTICSEARCH_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("ELASTICSEARCH_API_KEY", ""))
    ap.add_argument("--limit", type=int, default=0, help="Max panels to check (0=all)")
    args = ap.parse_args()
    if not args.es_url or not args.api_key:
        print("Need --es-url / ELASTICSEARCH_URL and --api-key", file=sys.stderr)
        return 2

    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    panels = [p for p in (catalog.get("panels") or []) if p.get("esql")]
    if args.limit:
        panels = panels[: args.limit]

    failed = 0
    for p in panels:
        pid = p.get("id") or "?"
        q = strip_params(p["esql"])
        st, body = es_query(args.es_url, args.api_key, q)
        if st == 200:
            n = len((body or {}).get("values") or [])
            print(f"OK  {pid} rows={n}")
        else:
            failed += 1
            err = body
            if isinstance(body, dict):
                err = body.get("error") or body.get("message") or body
            print(f"FAIL {pid} status={st} err={err}")
    print(f"checked={len(panels)} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
