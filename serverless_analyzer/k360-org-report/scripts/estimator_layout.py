"""Layout of the Observability and Security serverless estimators. No network.

Every script that writes an estimator by cell address checks the copy against
this file first, so a moved row stops the run instead of filling the wrong cell.
The Search (ES3) estimator has no entry in TEMPLATE_ANCHORS yet, so no script
fills it: see ES3_LAYOUT_MISSING.
`diag-serverless-consolidation/scripts/quote_checks.py` re-exports these names.
When an estimator release moves a row, change the cell maps, this file, and
`diag-serverless-consolidation/references/sizing-cells.md` together.
"""
from __future__ import annotations

import re

VERSION_TAB = "Version Control"
OBSERVABILITY_TAB = "Observability Estimator"
SECURITY_TAB = "Security Estimator"
ES3_TAB = "Estimator"
REFERENCE_TAB = "Estimator_Reference"

# Golden templates. They are copied, never written. The env names are the overrides
# that point a run at another template; an id set there is a template too.
GOLDEN_ES3 = "1Jsi5cMIDGfaP-S1_xqDcGeMYJSgklcBbu4dSJAEjlA0"
GOLDEN_OBSERVABILITY = "1tUdwJbTmd4helFOzEHMi_gpHw7qu_aCgprjlqmHh2hs"  # v1.22
GOLDEN_SECURITY = "1b91Uoa2e4vBrep_7I4RoY_8Mr19poqaUgWl7LqhIOqY"       # v2.6
TEMPLATE_ENV = ("ES3_SHEET_ID", "O11Y_SHEET_ID", "SECURITY_SHEET_ID")

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

# Cells fetch_org.py builds for the Search (ES3) estimator, tab `Estimator`.
# Their meaning comes from the code that fills them. The label text beside each
# cell on the sheet is not recorded anywhere in this repository, so there is no
# "es3" entry in TEMPLATE_ANCHORS and the fill is refused. To turn it on, read
# the label beside each of these cells (and beside C5 and C6) on the golden ES3
# template, add them as TEMPLATE_ANCHORS["es3"] = {"min_version": <number, or
# None when the workbook has no Version Control tab>, "labels": {...}}, and set
# the block's `layout` in fetch_org.estimator_payload to that version.
ES3_INPUT_CELLS = {
    "C12": "search stored GB",
    "C13": "documents",
    "C14": "average document size, KB",
    "C19": "peak QPS",
    "C20": "peak query complexity",
    "C21": "peak share of hours",
    "C23": "non-peak QPS",
    "C24": "non-peak query complexity",
    "C25": "non-peak share of hours",
    "C27": "idle share of hours",
    "C30": "ingest documents per second",
    "C31": "average document size, bytes",
    "C32": "active ingest hours a month",
}
ES3_LAYOUT_MISSING = (
    "The Search (ES3) estimator is not filled: estimator_layout.py records no layout for it, "
    "so its copy cannot be checked before cells are written. "
    "Add TEMPLATE_ANCHORS[\"es3\"] with the label beside each input cell to turn the fill on."
)


def layout_recorded(kind: str) -> bool:
    """True when TEMPLATE_ANCHORS can check a copy of this estimator."""
    spec = TEMPLATE_ANCHORS.get(kind)
    return bool(spec and spec.get("labels"))


def sheet_id(value) -> str:
    """Spreadsheet id from a bare id or a docs.google.com/spreadsheets/d/<id>/… URL."""
    text = str(value or "").strip()
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else text


def golden_template_ids(environ=None) -> set[str]:
    """Ids no fill may write to: the built-in templates and any id the env overrides name."""
    import os

    env = os.environ if environ is None else environ
    ids = {GOLDEN_ES3, GOLDEN_OBSERVABILITY, GOLDEN_SECURITY}
    for name in TEMPLATE_ENV:
        if env.get(name):
            ids.add(sheet_id(env[name]))
    return {i for i in ids if i}


def is_golden_template(spreadsheet_id, environ=None) -> bool:
    sid = sheet_id(spreadsheet_id)
    return bool(sid) and sid in golden_template_ids(environ)


# ----------------------------------------------------------------------------- region
# Region dropdown strings on the estimators: "AWS: us-east-1 (Virginia)".
_REGION_OPT = re.compile(r"^(AWS|AZURE|GCP)\s*:?\s*([a-z0-9\-]+)", re.IGNORECASE)
_COMPASS = "east|west|north|south|central|southeast|northeast"
_PLACES = (
    "us|europe|asia|india|australia|uk|japan|brazil|canada|germany|france|norway|sweden|"
    "switzerland|uae|korea|africa|qatar|poland|italy|israel|spain"
)


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _region_aliases(region: str) -> set[str]:
    """`eastus2` and `us-east-2` are the same Azure region; so are `westeurope` and `europe-west`."""
    r = _norm(re.sub(r"^(aws|gcp|azure)-", "", str(region or "").strip().lower()))
    out = {r} if r else set()
    m = re.match(rf"^({_COMPASS})({_PLACES})(\d*)$", r)
    if m:
        out.add(f"{m.group(2)}{m.group(1)}{m.group(3)}")
    m = re.match(rf"^({_PLACES})({_COMPASS})(\d*)$", r)
    if m:
        out.add(f"{m.group(2)}{m.group(1)}{m.group(3)}")
    return out


def region_options(values) -> list[str]:
    """Every 'PROVIDER: region (Name)' string in a block of sheet values, in sheet order."""
    out: list[str] = []
    for row in values or []:
        for cell in row or []:
            if isinstance(cell, str) and _REGION_OPT.match(cell.strip()) and cell.strip() not in out:
                out.append(cell.strip())
    return out


def match_region(provider, region, options) -> tuple[str | None, str]:
    """→ (option, how). how is exact, alias, or none.

    Only an option of the same provider whose region is this region is a
    match. With no match the option is None and how is "none": the caller
    leaves the sheet's own value and never writes another cloud's or another
    region's option.
    """
    prov = str(provider or "").strip().upper()
    want = _region_aliases(region or "")
    if not prov or not want:
        return None, "none"
    plain = _norm(re.sub(r"^(aws|gcp|azure)-", "", str(region or "").strip().lower()))
    for option in options or []:
        m = _REGION_OPT.match(option)
        if not m or m.group(1).upper() != prov:
            continue
        token = _norm(m.group(2))
        if token in want:
            return option, "exact" if token == plain else "alias"
    return None, "none"


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
    if spec.get("min_version") is None:
        # This workbook has no version to read. The labels are the whole check.
        return {"kind": kind, "version": None if version is None else str(version), "newer": False, "problems": problems}
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
    spec = TEMPLATE_ANCHORS[kind]
    cells = list(spec["labels"])
    ranges = [cell_range(tab, a1) for a1 in cells]
    if spec.get("min_version") is not None:
        ranges.append(f"'{VERSION_TAB}'!A1:A300")
    return cells, ranges


def check_value_ranges(kind: str, cells: list[str], value_ranges: list[dict]) -> dict:
    """template_check on a Sheets batchGet result for `layout_ranges`."""
    labels = {}
    for a1, vr in zip(cells, value_ranges or []):
        labels[a1] = (((vr or {}).get("values") or [[None]])[0] or [None])[0]
    tail = value_ranges[len(cells)] if value_ranges and len(value_ranges) > len(cells) else {}
    column = [(row or [None])[0] for row in ((tail or {}).get("values") or [])]
    return template_check(kind, labels, template_version(column))
