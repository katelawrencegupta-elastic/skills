#!/usr/bin/env python3
"""Emit clipboard-ready Apps Script JSON from an org report.

  python scripts/emit_estimator_import.py --org-json /tmp/org_report.json --kind o11y
  python scripts/emit_estimator_import.py --org-json /tmp/org_report.json --kind es3
  python scripts/emit_estimator_import.py --org-json /tmp/org_report.json --kind both --out /tmp/payload.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org-json", required=True, help="Path from fetch_org.py --out")
    ap.add_argument("--kind", choices=("o11y", "es3", "both"), default="both")
    ap.add_argument("--out", default="-", help="Write JSON here (default stdout)")
    args = ap.parse_args()

    data = json.loads(Path(args.org_json).read_text())
    payload = data.get("offload", {}).get("apps_script_payload")
    if not payload:
        raise SystemExit("No offload.apps_script_payload — re-run fetch_org.py")
    if payload.get("o11y"):
        # An org JSON fetched before estimator v1.22 carries the old cell addresses.
        from estimator_layout import upgrade_observability_cells
        try:
            payload = {**payload, "o11y": upgrade_observability_cells(payload["o11y"])}
        except ValueError as ex:
            raise SystemExit(f"ERROR: {ex}") from ex

    if args.kind == "o11y":
        out = {"org": payload.get("org"), "o11y": payload["o11y"]}
    elif args.kind == "es3":
        out = {"org": payload.get("org"), "es3": payload["es3"]}
    else:
        out = payload

    text = json.dumps(out, indent=2)
    if args.out == "-":
        sys.stdout.write(text + "\n")
    else:
        Path(args.out).write_text(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)

    # One-line hint for the agent/user
    primary = (data.get("offload") or {}).get("primary") or {}
    hybrid = (data.get("offload") or {}).get("hybrid") or {}
    if primary:
        print(
            f"primary {primary.get('id')}: "
            f"SVLS ~${primary.get('serverless', {}).get('total_mo')}/mo · "
            f"save vs ECH slice ~${primary.get('save_vs_ech_slice_mo')}/mo",
            file=sys.stderr,
        )
    if hybrid:
        print(
            f"hybrid ~${hybrid.get('total_mo')}/mo · "
            f"save vs full ECH ~${hybrid.get('save_vs_full_ech_yr')}/yr",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
