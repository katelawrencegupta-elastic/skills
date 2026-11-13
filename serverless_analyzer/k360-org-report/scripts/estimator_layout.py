"""Layout of the Observability and Security serverless estimators. No network.

Every script that writes an estimator by cell address checks the copy against
this file first, so a moved row stops the run instead of filling the wrong cell.
`diag-serverless-consolidation/scripts/quote_checks.py` re-exports these names.
When an estimator release moves a row, change the cell maps, this file, and
`diag-serverless-consolidation/references/sizing-cells.md` together.
"""
from __future__ import annotations

import re

VERSION_TAB = "Version Control"
OBSERVABILITY_TAB = "Observability Estimator"
SECURITY_TAB = "Security Estimator"

# Each anchor is the text beside a cell a script writes or reads. `min_version`
# is the oldest layout these addresses fit. A sheet prints 1.20 as "1.2", so
# versions compare as numbers.
TEMPLATE_ANCHORS = {
    "observability": {
        "min_version": 1.22,
        "labels": {
            "D4": "feature tier",
            "D5": "technical support tier",
            "D8": "cloud provider and region",
            "D11": "everything except tsds metrics",
            "D12": "ingested tsds metrics",
            "D15": "synthetic monitoring",
            "D19": "estimated monthly (bytes)",
            "D20": "estimated monthly (bytes)",
            "D23": "daily logs raw data ingest",
            "D24": "kubernetes",
            "D25": "daily metrics raw data ingest",
            "D26": "traces per minute",
            "D27": "sampling rate",
            "D28": "total raw ingest in gb/day",
            "D29": "metered ingest in gb/day",
            "D33": "cross-project search",
            # The GB/day each ingest ladder prices. A metered fill reads these back as its echo.
            "Estimator_Reference!A2": "ingest",
            "Estimator_Reference!D2": "gb/day",
            "Estimator_Reference-Metrics!A2": "ingest-metrics",
            "Estimator_Reference-Metrics!D2": "gb/day",
            "D37": "monthly estimate (steady state)",
            "E37": "annual estimate (first year)",
            "F37": "annual estimate (steady state)",
        },
    },
    "security": {
        "min_version": 2.6,
        "labels": {
            "D4": "daily siem raw data ingest",
            "E5": "actual metered ingest",
            "D6": "retention period",
            "D7": "cloud provider region",
            "D8": "feature tier",
            "D9": "elastic defend",
            "D21": "technical support",
            "D35": "annual estimate (year 1)",
            "E35": "annual estimate (year 2)",
            "F35": "annual estimate (year 3)",
            "D61": "data ingest charges",
            "D62": "data retention charges",
            "D69": "support charges",
            "D73": "cps add-on charges",
            "D74": "total monthly charges",
            # Month 36 on the reference tab, read as the steady state above 12 months.
            "Estimator_Reference!CB1": "36",
            "Estimator_Reference!I242": "data ingest charges",
            "Estimator_Reference!I243": "data retention charges",
            "Estimator_Reference!I250": "support charges",
            "Estimator_Reference!I254": "total monthly charges",
        },
    },
}

# Input cells on the Observability estimator, v1.22.
OBSERVABILITY_LAYOUT = "1.22"
OBSERVABILITY_INPUT_CELLS = frozenset({
    "G4", "G5", "G8", "G11", "G12", "G15", "G19", "G20",
    "G23", "G24", "G25", "G26", "G27", "G33",
})
# Where each v1.19 input moved. v1.20 to v1.22 added the TSDS retention row,
# a second Option 1 row, and the cross-project search rows.
_V119_TO_V122 = {
    "G4": "G4", "G5": "G5", "G8": "G8", "G11": "G11",
    "G14": "G15",   # synthetics
    "G18": "G19",   # Option 1, everything except TSDS metrics
    "G21": "G23",   # logs GB/day
    "G22": "G24",   # Kubernetes share of logs
    "G23": "G25",   # metrics GB/day
    "G24": "G26",   # traces per minute
    "G25": "G27",   # trace sampling
}


def upgrade_observability_cells(block: dict) -> dict:
    """Observability input block at the v1.22 addresses.

    A block tagged with the current layout is returned unchanged. An untagged
    block was built for v1.19: its cells are moved to where those inputs sit
    now, TSDS retention takes the same month, and the rows v1.19 did not have
    are set off. A cell that is in neither layout raises ValueError.
    """
    cells = dict((block or {}).get("cells") or {})
    if (block or {}).get("layout") == OBSERVABILITY_LAYOUT:
        stray = sorted(set(cells) - OBSERVABILITY_INPUT_CELLS)
        if stray:
            raise ValueError(f"Observability cells {', '.join(stray)} are not inputs on estimator v{OBSERVABILITY_LAYOUT}")
        return dict(block)
    stray = sorted(set(cells) - set(_V119_TO_V122))
    if stray:
        raise ValueError(
            f"Observability cells {', '.join(stray)} are not v1.19 inputs and the block has no layout tag. "
            "Fetch the organization again so the cells are built for the current estimator."
        )
    moved = {_V119_TO_V122[a1]: value for a1, value in cells.items()}
    moved.setdefault("G12", moved.get("G11"))
    moved.setdefault("G20", 0)
    moved.setdefault("G33", "No")
    return {**(block or {}), "layout": OBSERVABILITY_LAYOUT, "cells": moved, "upgraded_from": "1.19"}


def template_version(column) -> str | None:
    """Last filled cell of the Version Control tab's first column, the sheet's own version."""
    last = None
    for value in column or []:
        text = str(value if value is not None else "").strip()
        if text:
            last = text
    return last


def _version_number(text) -> float | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)?)", str(text or "").strip(), re.IGNORECASE)
    return float(match.group(1)) if match else None


def template_check(kind: str, labels: dict, version) -> dict:
    """Does this estimator copy have the layout the scripts write? No network.

    `problems` is empty when every anchor label is where it is expected and the
    version is readable and not older than `min_version`. A newer version with
    every label in place is accepted and marked `newer`, because estimator
    releases that only add regions keep the layout.
    """
    spec = TEMPLATE_ANCHORS.get(kind)
    if spec is None:
        return {"kind": kind, "version": None, "newer": False, "problems": [f"no layout is recorded for {kind!r}"]}
    problems: list[str] = []
    for cell, want in spec["labels"].items():
        have = " ".join(str((labels or {}).get(cell) or "").lower().split())
        if want not in have:
            shown = have[:60] or "blank"
            problems.append(f"{cell} should read '{want}' and is '{shown}'")
    number = _version_number(version)
    newer = False
    if number is None:
        problems.append(f"the Version Control tab gave no readable version ({version!r})")
    elif number < spec["min_version"] - 1e-9:
        problems.append(f"version {version} is older than {spec['min_version']:g}, the layout these cells fit")
    else:
        newer = number > spec["min_version"] + 1e-9
    return {"kind": kind, "version": None if version is None else str(version), "newer": newer, "problems": problems}


def cell_range(tab: str, a1: str) -> str:
    """A1 range for a cell. `Other Tab!A1` names its own tab; a bare address is on `tab`."""
    if "!" in a1:
        other, cell = a1.rsplit("!", 1)
        return f"'{other.strip(chr(39))}'!{cell}"
    return f"'{tab}'!{a1}"


def layout_ranges(kind: str, tab: str) -> tuple[list[str], list[str]]:
    """Anchor cells, and the A1 ranges that read them plus the version column."""
    cells = list(TEMPLATE_ANCHORS[kind]["labels"])
    return cells, [cell_range(tab, a1) for a1 in cells] + [f"'{VERSION_TAB}'!A1:A300"]


def check_value_ranges(kind: str, cells: list[str], value_ranges: list[dict]) -> dict:
    """template_check on a Sheets batchGet result for `layout_ranges`."""
    labels = {}
    for a1, vr in zip(cells, value_ranges or []):
        labels[a1] = (((vr or {}).get("values") or [[None]])[0] or [None])[0]
    tail = value_ranges[len(cells)] if value_ranges and len(value_ranges) > len(cells) else {}
    column = [(row or [None])[0] for row in ((tail or {}).get("values") or [])]
    return template_check(kind, labels, template_version(column))
