"""Pure checks for a diagnostic serverless quote. No network and no sheets."""
from __future__ import annotations

import re

G28_TOLERANCE_GB_DAY = 0.51


def money(value) -> float | None:
    """Parse a sheet money cell. Parentheses mark a credit, so ($1,234) is negative."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^\d.\-]", "", text)
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if negative and number > 0:
        return -number
    return number


def g28_gate(written_bulk: float, sheet_raw) -> dict:
    """Accept Observability only when G28 is within 0.51 GB/day of the bulk written."""
    sheet = money(sheet_raw)
    try:
        written = float(written_bulk)
    except (TypeError, ValueError):
        written = 0.0
    accepted = sheet is not None and abs(sheet - written) <= G28_TOLERANCE_GB_DAY
    return {
        "written_bulk": round(written, 3),
        "sheet_raw_gb_day": None if sheet is None else round(sheet, 3),
        "accepted": accepted,
    }


# Layout this skill writes and reads, by estimator. Each anchor is the text that
# sits beside a cell the skill uses, so a moved row is caught before any write.
# `min_version` is the oldest layout these addresses are known to fit. A sheet
# reports 1.20 as "1.2", so versions compare as numbers.
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
            "D73": "cps add-on charges",
            "D74": "total monthly charges",
        },
    },
}


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
    """Does this estimator copy have the layout the skill writes? No network.

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


def _integrity_row(name: str, score, passed, detail: str) -> dict:
    return {
        "check": name,
        "score": None if score is None else round(float(score), 3),
        "passed": passed,
        "detail": detail,
    }


def _sheet_echo(observability: dict, security: dict) -> dict:
    """G28 distance and a parsed E73 of 0. A missing sheet is left out of this check."""
    parts: list[float] = []
    passes: list[bool] = []
    bits: list[str] = []
    if observability.get("has_workload") and observability.get("sheet_read"):
        written = float(observability.get("written_bulk") or 0)
        sheet = money(observability.get("sheet_raw"))
        if sheet is None:
            parts.append(0.0)
            passes.append(False)
            bits.append("G28 was blank or unreadable")
        else:
            gap = abs(sheet - written)
            parts.append(1.0 - min(gap / G28_TOLERANCE_GB_DAY, 1.0))
            passes.append(gap <= G28_TOLERANCE_GB_DAY)
            bits.append(f"G28 is {gap:.3f} GB/day from the written bulk")
    if security.get("has_workload") and security.get("sheet_read"):
        amount = money(security.get("e73"))
        if amount is None:
            parts.append(0.0)
            passes.append(False)
            bits.append("E73 was blank or unreadable")
        elif abs(amount) > 0.005:
            parts.append(0.0)
            passes.append(False)
            bits.append(f"E73 is {amount:g}")
        else:
            parts.append(1.0)
            passes.append(True)
            bits.append("E73 is 0")
    if not parts:
        return _integrity_row("Sheet echo", None, None, "No Observability or Security sheet was read.")
    return _integrity_row("Sheet echo", sum(parts) / len(parts), all(passes), ". ".join(bits) + ".")


def _coverage_row(coverage: dict | None) -> dict:
    if not coverage:
        return _integrity_row("Bundle coverage", None, None, "This run has no diagnostic coverage.")
    matched = float(coverage.get("matched") or 0)
    fallback = float(coverage.get("fallback") or 0)
    active = coverage.get("active")
    if active is None:
        denominator = matched + fallback
        label = "matched bundles"
    else:
        denominator = float(active)
        label = "active deployments"
    if denominator <= 0:
        return _integrity_row("Bundle coverage", None, None, "No active deployments to cover.")
    score = min(matched / denominator, 1.0)
    passed = matched >= denominator and fallback == 0
    detail = f"{matched:g} of {denominator:g} {label} have a bundle."
    names = [str(name) for name in (coverage.get("fallback_names") or []) if name]
    if names:
        detail += " Missing: " + ", ".join(names) + "."
    return _integrity_row("Bundle coverage", score, passed, detail)


def _retention_row(coverage: dict | None) -> dict:
    if not coverage:
        return _integrity_row("Measured retention", None, None, "This run has no diagnostic coverage.")
    bulk = float(coverage.get("bulk_gb_day") or 0)
    unmeasured = float(coverage.get("unmeasured_bulk") or 0)
    if bulk <= 0:
        return _integrity_row("Measured retention", None, None, "No bulk to weight.")
    score = max(0.0, 1.0 - unmeasured / bulk)
    return _integrity_row(
        "Measured retention",
        score,
        unmeasured == 0,
        f"{unmeasured:.3f} GB/day of {bulk:.3f} has no measured age.",
    )


# Share of local store that may sit in unrated indices before the check is flagged.
UNRATED_STORE_PASS = 0.05


def _rated_row(coverage: dict | None) -> dict:
    """Store in indices that took bulk writes and have no rate, as a share of local store."""
    unrated = (coverage or {}).get("unrated")
    if not coverage or unrated is None:
        return _integrity_row("Rated ingest", None, None, "The bundles were parsed without the unrated-index count.")
    count = int(unrated.get("indices") or 0)
    store = float(unrated.get("store_gb") or 0)
    share = unrated.get("share_of_store")
    if count == 0:
        return _integrity_row("Rated ingest", 1.0, True, "Every index that took bulk writes is rated or is a rolled generation.")
    if share is None:
        return _integrity_row(
            "Rated ingest", 0.0, False,
            f"{count} indices took bulk writes and have no rate, and there is no local store to compare them with.",
        )
    share = min(max(float(share), 0.0), 1.0)
    detail = (
        f"{count} indices took bulk writes and have no rate. They hold {store:,.1f} GB, "
        f"{share * 100:.1f}% of local store. Their ingest is not in bulk GB/day."
    )
    names = [f"{row.get('name')} ({float(row.get('store_gb') or 0):,.0f} GB)" for row in (unrated.get("deployments") or [])[:6] if row.get("name")]
    if names:
        detail += " Largest: " + ", ".join(names) + "."
    return _integrity_row("Rated ingest", 1.0 - share, share <= UNRATED_STORE_PASS, detail)


def _direct_row(solutions: list[dict]) -> dict:
    applicable = [row for row in solutions if row.get("has_workload")]
    if not applicable:
        return _integrity_row("Direct read-back", None, None, "No solution has a workload.")
    direct = [row for row in applicable if row.get("direct")]
    return _integrity_row(
        "Direct read-back",
        len(direct) / len(applicable),
        len(direct) == len(applicable),
        f"{len(direct)} of {len(applicable)} solutions with a workload use their own read-back.",
    )


def _line_row(headline, lines: list[dict]) -> dict:
    headline = round(float(headline or 0), 2)
    total = round(sum(float(row.get("monthly") or 0) for row in lines), 2)
    if headline == 0 and total == 0:
        return _integrity_row("Line agreement", 1.0, True, "The headline and the quote lines are both $0.")
    base = headline or total
    gap = abs(total - headline)
    return _integrity_row(
        "Line agreement",
        1.0 - min(gap / base, 1.0),
        gap < 0.005,
        f"Quote lines sum to ${total:,.2f}. The headline is ${headline:,.2f}.",
    )


def _year_cents(value) -> float | None:
    amount = money(value)
    if amount is None:
        return None
    return round(amount, 2)


def _year_row(lines: list[dict]) -> dict:
    """Steady annual against 12 × the unrounded monthly.

    Year 1 is a separate cell. It has to match the steady annual only when the
    sheet is written as 1 month. A longer keep may ramp, so year 1 can sit below
    that steady figure. A line with no steady annual uses its year-1 figure.
    """
    scored: list[float] = []
    bad = 0
    bits: list[str] = []
    for row in lines:
        monthly = row.get("monthly_unrounded")
        if monthly is None:
            monthly = row.get("monthly")
        year1 = _year_cents(row.get("annual_year1"))
        steady_cell = row.get("annual_steady_state")
        steady = _year_cents(steady_cell if steady_cell is not None else row.get("annual_year1"))
        if monthly is None and steady is None and year1 is None:
            continue
        if monthly is None or steady is None:
            scored.append(0.0)
            bad += 1
            bits.append("A quote line is missing a monthly or a steady annual.")
            continue
        expected = round(float(monthly) * 12, 2)
        gap = abs(steady - expected)
        base = abs(steady) or abs(expected) or 1.0
        line_score = 1.0 - min(gap / base, 1.0)
        line_bad = gap >= 0.005
        bits.append(
            f"Steady annual is ${steady:,.2f}. 12 × the unrounded monthly is ${expected:,.2f}, gap ${gap:,.2f}."
        )
        months = row.get("retention_months")
        one_month = months is not None and float(months) == 1
        if one_month and steady_cell is not None:
            if year1 is None:
                line_score = 0.0
                line_bad = True
                bits.append("Year 1 is missing on a 1-month sheet.")
            else:
                year_gap = abs(year1 - steady)
                year_base = abs(steady) or abs(year1) or 1.0
                line_score = min(line_score, 1.0 - min(year_gap / year_base, 1.0))
                if year_gap >= 0.005:
                    line_bad = True
                bits.append(
                    f"Year 1 is ${year1:,.2f} and the steady annual is ${steady:,.2f}, gap ${year_gap:,.2f}."
                )
        scored.append(line_score)
        if line_bad:
            bad += 1
    if not scored:
        return _integrity_row("Year agreement", None, None, "No quote line has a monthly and a steady annual.")
    lead = f"{bad} of {len(scored)} quote lines differ from 12 × the unrounded monthly."
    return _integrity_row(
        "Year agreement",
        sum(scored) / len(scored),
        bad == 0,
        lead + " " + " ".join(bits),
    )


PASS_SCORE = 0.90


def integrity_score(facts: dict) -> dict:
    """Quote integrity, 0 to 1. The mean skips a check that does not apply.

    A score of 0.90 or higher passes. A check that misses its own bar stays
    flagged. A quote whose every scored check passes also passes. A G28 gap of
    0.51 scores 0 on that component and still passes that component. This is
    not a serverless bill.
    """
    coverage = facts.get("coverage")
    checks = [
        _sheet_echo(facts.get("observability") or {}, facts.get("security") or {}),
        _coverage_row(coverage),
        _retention_row(coverage),
        _rated_row(coverage),
        _direct_row(list(facts.get("solutions") or [])),
        _line_row(facts.get("headline_monthly"), list(facts.get("lines") or [])),
        _year_row(list(facts.get("lines") or [])),
    ]
    scored_rows = [row for row in checks if row["score"] is not None]
    scored = [row["score"] for row in scored_rows]
    score = None if not scored else round(sum(scored) / len(scored), 3)
    if score is None:
        passed = None
    else:
        passed = score >= PASS_SCORE or all(row["passed"] for row in scored_rows)
    return {
        "score": score,
        "passed": passed,
        "checks": checks,
    }


def e73_blocks_quote(value) -> bool:
    """E74 includes E73. A blank, unreadable, or non-zero add-on blocks the quote.

    Call this only after a Security sheet was read. A missing project is not a blank cell.
    Zero still passes.
    """
    amount = money(value)
    if amount is None:
        return True
    return abs(amount) > 0.005


def addon_off_value(validation: dict | None, formatted) -> object | None:
    """Value that turns a Security add-on input off. None means leave the cell alone."""
    condition = ((validation or {}).get("condition") or {})
    kind = str(condition.get("type") or "")
    choices = [str((item or {}).get("userEnteredValue") or "") for item in (condition.get("values") or [])]
    if kind == "ONE_OF_LIST":
        for candidate in ("No", "no", "N", "FALSE", "False", "0"):
            if candidate in choices:
                return candidate
        return None
    if kind == "BOOLEAN":
        return False
    if kind.startswith("NUMBER") or kind == "":
        if kind == "" and isinstance(formatted, str):
            text = formatted.strip()
            if text and not re.fullmatch(r"[\d.,%\s$\-()]+", text):
                return None
        return 0
    return None
