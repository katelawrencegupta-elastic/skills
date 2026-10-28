#!/usr/bin/env python3
"""Scaffold PACKAGE_ROOT for a new Splunk→Kibana conversion.

Usage:
  python scaffold_use_case.py --use-case aws_user_investigation --out ./elastic
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = SKILL_ROOT / "templates"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--use-case", required=True)
    ap.add_argument("--out", type=Path, default=Path("./elastic"))
    ap.add_argument("--dash-id", default="", help="Kibana dashboard id (default: derived)")
    args = ap.parse_args()

    root = args.out / args.use_case
    for sub in ("mockup", "kibana", "data_factories/out", "esql", "metrics"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    # mockup
    src_mock = TEMPLATES / "mockup"
    if src_mock.is_dir():
        for p in src_mock.iterdir():
            dest = root / "mockup" / p.name
            if p.is_file() and not dest.exists():
                shutil.copy2(p, dest)

    # MAPPING template
    mapping_src = TEMPLATES / "MAPPING.md"
    mapping_dest = root / "MAPPING.md"
    if mapping_src.is_file() and not mapping_dest.exists():
        text = mapping_src.read_text(encoding="utf-8")
        text = text.replace("<use_case>", args.use_case)
        mapping_dest.write_text(text, encoding="utf-8")

    analysis = root / "ANALYSIS.md"
    if not analysis.exists():
        analysis.write_text(
            f"# {args.use_case} — conversion analysis\n\n"
            f"**Mockup:** [`mockup/index.html`](mockup/index.html)\n"
            f"**Mapping:** [`MAPPING.md`](MAPPING.md)\n\n"
            f"Fill after inventory + deploy.\n",
            encoding="utf-8",
        )

    dash_id = args.dash_id or args.use_case.replace("_", "-")
    manifest = {
        "id": args.use_case,
        "elastic": {"dashboard_id": dash_id},
        "package_root": str(root),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    factory_src = TEMPLATES / "factories" / "minimal_ndjson.py"
    factory_dest = root / "data_factories" / "generate_demo.py"
    if factory_src.is_file() and not factory_dest.exists():
        shutil.copy2(factory_src, factory_dest)

    fields_ex = TEMPLATES / "factories" / "fields.example.json"
    fields_dest = root / "data_factories" / "fields.example.json"
    if fields_ex.is_file() and not fields_dest.exists():
        shutil.copy2(fields_ex, fields_dest)

    ex = TEMPLATES / "esql" / "panels.example.json"
    dest_ex = root / "esql" / "panels.example.json"
    if ex.is_file() and not dest_ex.exists():
        shutil.copy2(ex, dest_ex)

    print(f"Scaffolded {root}")
    print(f"  dashboard_id={dash_id}")
    print("Next:")
    print("  parse_inventory.py [--from-audit … --name …] → panels_from_inventory.py")
    print("  → edit esql/panels.json + data_factories/fields.json")
    print("  → run_pipeline.py --package … --assume-env")
    print("Target: ≤12 assistant turns; one USE_CASE per chat; metrics/token_usage.json required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
