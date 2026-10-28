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
                f"build_dashboard.py has {lines} lines (>{args.max_build_lines}) without esql/panels.json"
            )
        elif lines > args.max_build_lines:
            issues.append(
                f"build_dashboard.py has {lines} lines (>{args.max_build_lines}); prefer catalog codegen"
            )

    if not mockup.exists():
        issues.append("missing mockup/index.html")

    if issues:
        print("WARN:")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("OK package looks catalog-friendly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
