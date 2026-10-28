#!/usr/bin/env python3
"""Minimal demo NDJSON factory — adapt fields to match panel ES|QL.

Usage:
  python minimal_ndjson.py --out ./out --n 200 --stream-dataset my.dataset
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path


def generate(n: int, seed: int, dataset: str, namespace: str) -> list[dict]:
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    docs = []
    for i in range(n):
        ts = now - timedelta(minutes=rng.randint(1, 60 * 24))
        docs.append(
            {
                "@timestamp": ts.isoformat(),
                "data_stream": {
                    "type": "logs",
                    "dataset": dataset,
                    "namespace": namespace,
                },
                "event": {
                    "action": rng.choice(["read", "write", "login", "denied"]),
                    "dataset": dataset,
                    "outcome": rng.choice(["success", "failure"]),
                },
                "user": {"email": rng.choice(["demo.user@example.com", "other@example.com"])},
                "source": {"ip": rng.choice(["10.0.0.10", "203.0.113.5"])},
                "message": f"demo event {i}",
                "tags": ["splunk-xml-to-kibana-demo"],
            }
        )
    docs.sort(key=lambda d: d["@timestamp"])
    return docs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--stream-dataset", default="demo.events")
    ap.add_argument("--namespace", default="default")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    docs = generate(args.n, args.seed, args.stream_dataset, args.namespace)
    path = args.out / f"logs-{args.stream_dataset.replace('.', '_')}.ndjson"
    with path.open("w", encoding="utf-8") as f:
        for d in docs:
            f.write(json.dumps(d) + "\n")
    print(f"Wrote {len(docs)} docs → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
