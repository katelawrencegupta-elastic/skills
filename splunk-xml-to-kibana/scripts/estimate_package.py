#!/usr/bin/env python3
"""Warn on oversized hand-written builders / missing catalog.

Usage:
  python estimate_package.py --package PACKAGE_ROOT
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--package", type=Path, required=True)
    ap.add_argument("--max-build-lines", type=int, default=200)
    args = ap.parse_args()
    root = args.package
    issues = []

    inv = root / "inventory.json"
    catalog = root / "esql" / "panels.json"
    build = root / "build_dashboard.py"
    mockup = root / "mockup" / "index.html"

    if inv.exists():
        import json

        n = len(json.loads(inv.read_text()).get("panels") or [])
        print(f"inventory panels={n}")
    else:
        issues.append("missing inventory.json")

    if catalog.exists():
        import json

        n = len(json.loads(catalog.read_text()).get("panels") or [])
        print(f"catalog panels={n}")
    else:
        issues.append("missing esql/panels.json (prefer catalog + build_from_catalog.py)")

    if build.exists():
        lines = len(build.read_text(encoding="utf-8").splitlines())
        print(f"build_dashboard.py lines={lines}")
        if lines > args.max_build_lines and not catalog.exists():
            issues.append(
                f"build_dashboard.py has {lines} lines (>{args.max_build_lines}) without esql/panels.json — "
                "prefer panels_from_inventory.py + run_pipeline.py"
            )
        elif lines > args.max_build_lines:
            issues.append(
                f"build_dashboard.py has {lines} lines (>{args.max_build_lines}); "
                "prefer esql/panels.json + build_from_catalog.py / run_pipeline.py"
            )
        elif catalog.exists():
            print("hint: catalog present — prefer run_pipeline.py over custom build_dashboard.py")

    fields = root / "data_factories" / "fields.json"
    if catalog.exists() and not fields.exists():
        print("hint: add data_factories/fields.json for generate_from_fields.py / run_pipeline")

    if not mockup.exists():
        issues.append("missing mockup/index.html")

    metrics = root / "metrics" / "token_usage.json"
    if metrics.exists():
        import json

        m = json.loads(metrics.read_text(encoding="utf-8"))
        tot = (m.get("totals") or {})
        print(
            f"token_usage cost_relevant={tot.get('cost_relevant_tokens')} "
            f"billed_in={tot.get('billed_input_incl_cache')} "
            f"turns={m.get('assistant_turns_est')}"
        )
    else:
        issues.append("missing metrics/token_usage.json (run record_token_usage.py or enable Cursor stop hook)")

    if issues:
        print("WARN:")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("OK package looks catalog-friendly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
