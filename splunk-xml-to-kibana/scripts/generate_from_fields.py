#!/usr/bin/env python3
"""Generate synthetic NDJSON from data_factories/fields.json (or a list / glob).

Usage:
  python generate_from_fields.py --fields PACKAGE/data_factories/fields.json --out PACKAGE/data_factories/out
  python generate_from_fields.py --fields-dir PACKAGE/data_factories/fields --out …
"""

from __future__ import annotations

import argparse
import json
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _set_nested(doc: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = doc
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _sample(spec: dict, rng: random.Random):
    t = (spec.get("type") or "enum").lower()
    if t == "enum":
        vals = spec.get("values") or ["demo"]
        return rng.choice(vals)
    if t == "int":
        return rng.randint(int(spec.get("min", 0)), int(spec.get("max", 100)))
    if t == "float":
        lo = float(spec.get("min", 0.0))
        hi = float(spec.get("max", 1.0))
        return round(rng.uniform(lo, hi), 2)
    if t == "bool":
        return rng.choice([True, False])
    if t == "const":
        return spec.get("value")
    return spec.get("value", "demo")


def _stream_parts(stream: str) -> tuple[str, str, str]:
    """logs-bx.notable-default → (logs, bx.notable, default) for data_stream meta."""
    name = stream.strip()
    m = re.match(r"^(logs|metrics|traces)-(.+)-([^-]+)$", name)
    if m:
        return m.group(1), m.group(2), m.group(3)
    if name.startswith("logs-"):
        rest = name[5:]
        if "-" in rest:
            ds, ns = rest.rsplit("-", 1)
            return "logs", ds, ns
        return "logs", rest, "default"
    return "logs", name, "default"


def generate_one(cfg: dict) -> tuple[str, list[dict]]:
    stream = cfg.get("stream") or "logs-demo.events-default"
    n = int(cfg.get("n") or 200)
    seed = int(cfg.get("seed") or 42)
    days = float(cfg.get("days") or 7)
    fields = cfg.get("fields") or {}
    rng = random.Random(seed)
    now = datetime.now(timezone.utc)
    ds_type, dataset, namespace = _stream_parts(stream)

    docs: list[dict] = []
    for i in range(n):
        age = rng.random() * days
        ts = now - timedelta(days=age)
        doc: dict = {
            "@timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "data_stream": {
                "type": ds_type,
                "dataset": dataset,
                "namespace": namespace,
            },
            "event": {"dataset": dataset, "kind": "event"},
            "tags": ["sx2k-factory"],
        }
        for key, spec in fields.items():
            if not isinstance(spec, dict):
                _set_nested(doc, key, spec)
            else:
                _set_nested(doc, key, _sample(spec, rng))
        docs.append(doc)
    docs.sort(key=lambda d: d["@timestamp"])
    return stream, docs


def load_configs(fields: Path | None, fields_dir: Path | None) -> list[dict]:
    configs: list[dict] = []
    if fields and fields.exists():
        raw = json.loads(fields.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            configs.extend(raw)
        elif isinstance(raw, dict):
            configs.append(raw)
        else:
            raise SystemExit(f"Unrecognized fields JSON in {fields}")
    if fields_dir and fields_dir.is_dir():
        for p in sorted(fields_dir.glob("*.json")):
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                configs.extend(raw)
            else:
                configs.append(raw)
    if not configs:
        raise SystemExit("No fields config found — pass --fields or --fields-dir")
    return configs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fields", type=Path, help="fields.json (object or list)")
    ap.add_argument("--fields-dir", type=Path, help="Directory of *.json field configs")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    configs = load_configs(args.fields, args.fields_dir)
    total = 0
    for cfg in configs:
        stream, docs = generate_one(cfg)
        safe = re.sub(r"[^\w.\-]+", "_", stream)
        path = args.out / f"{safe}.ndjson"
        path.write_text("\n".join(json.dumps(d) for d in docs) + "\n", encoding="utf-8")
        print(f"Wrote {path} docs={len(docs)} stream={stream}")
        total += len(docs)
    print(f"total_docs={total} configs={len(configs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
