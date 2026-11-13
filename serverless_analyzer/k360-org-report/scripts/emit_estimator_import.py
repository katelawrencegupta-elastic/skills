#!/usr/bin/env python3
"""Emit clipboard-ready Apps Script JSON from an org report.

  python scripts/emit_estimator_import.py --org-json <folder>/<key>_org_report.json --kind o11y
  python scripts/emit_estimator_import.py --org-json <folder>/<key>_org_report.json --kind both --out <folder>/payload.json

The Search (ES3) block is emitted only when estimator_layout.py records an ES3
layout. Until then `--kind es3` stops and `--kind both` emits Observability.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from estimator_layout import ES3_LAYOUT_MISSING, layout_recorded, upgrade_observability_cells
from report_mode import write_private


def import_payload(data: dict, kind: str) -> tuple[dict, list[str]]:
    """The JSON the Apps Script importer takes, and notes for the operator. No network.

    The Observability block is moved to the current cell addresses and tagged
    with its layout. Its region cell is taken out of `cells`: `cloud` carries the
    org's provider and region, and the importer writes the cell only when the
    sheet lists that option.
    """
    payload = (data.get("offload") or {}).get("apps_script_payload")
    if not payload:
        raise SystemExit("No offload.apps_script_payload — re-run fetch_org.py")
    notes: list[str] = []
    out: dict = {"org": payload.get("org")}
    want_es3 = kind in ("es3", "both")
    if want_es3 and not layout_recorded("es3"):
        if kind == "es3":
            raise SystemExit(f"ERROR: {ES3_LAYOUT_MISSING}")
        notes.append(ES3_LAYOUT_MISSING)
        want_es3 = False
    if want_es3 and payload.get("es3"):
        out["es3"] = payload["es3"]
    if kind in ("o11y", "both") and payload.get("o11y"):
        # An org JSON fetched before estimator v1.22 carries the old cell addresses.
        try:
            block = upgrade_observability_cells(payload["o11y"])
        except ValueError as ex:
            raise SystemExit(f"ERROR: {ex}") from ex
        summary = (data.get("infrastructure") or {}).get("summary") or {}
        cloud = dict(block.get("cloud") or payload.get("cloud") or {})
        cloud.setdefault("provider", summary.get("provider"))
        cloud.setdefault("region", summary.get("region"))
        cloud.setdefault("cell", "G8")
        cells = {a1: v for a1, v in (block.get("cells") or {}).items() if a1 != cloud["cell"]}
        out["o11y"] = {**block, "cloud": cloud, "cells": cells}
    return out, notes


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org-json", required=True, help="Path from fetch_org.py --out")
    ap.add_argument("--kind", choices=("o11y", "es3", "both"), default="both")
    ap.add_argument("--out", default="-", help="Write JSON here (default stdout)")
    args = ap.parse_args()

    data = json.loads(Path(args.org_json).read_text())
    out, notes = import_payload(data, args.kind)
    for note in notes:
        print(f"NOTE: {note}", file=sys.stderr)

    text = json.dumps(out, indent=2)
    if args.out == "-":
        sys.stdout.write(text + "\n")
    else:
        write_private(Path(args.out), text + "\n")
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
