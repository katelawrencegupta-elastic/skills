"""Pure checks for a diagnostic serverless quote. No network and no sheets."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from skill_paths import find_skill

# The estimator layout lives in k360-org-report, which also writes those sheets.
_ORG_SCRIPTS = str(find_skill("k360-org-report", Path(__file__).resolve().parent) / "scripts")
if _ORG_SCRIPTS not in sys.path:
    sys.path.insert(0, _ORG_SCRIPTS)
from estimator_layout import (  # noqa: E402,F401
    TEMPLATE_ANCHORS, cell_range, check_value_ranges, layout_ranges, template_check, template_version,
)
from constants import (  # noqa: E402
    BILLING_WINDOW_MIN_SHARE, COUNTER_AGE_MAX_SHARE, ECHO_TOLERANCE_GB_DAY, ECHO_TOLERANCE_SHARE,
    PLAN_HEADLINE_SHARE, PLAN_HEADLINE_USD, RATE_WINDOW_MAX_SHARE, RETENTION_ASSUMED_MAX_SHARE,
    UNRATED_STORE_MAX_SHARE,
)

# The sheet echo has to match the GB/day written. The sheet rounds a little, so there is a floor,
# and a large rate gets a share of itself. It was a flat 0.51 GB/day, which no sheet under 0.51
# GB/day could ever fail and which let a 5% error through at 10 GB/day.
ECHO_TOLERANCE_FLOOR_GB_DAY = ECHO_TOLERANCE_GB_DAY


def echo_tolerance(written) -> float:
    """GB/day the echo may differ from the written bulk: 0.1% of it, never less than 0.02."""
    try:
        base = abs(float(written))
    except (TypeError, ValueError):
        base = 0.0
    return max(ECHO_TOLERANCE_FLOOR_GB_DAY, ECHO_TOLERANCE_SHARE * base)


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


def ingest_echo(readback: dict):
    """GB/day the sheet says it is pricing, to compare with what was written.

    A diagnostic fill writes metered volume, so the echo is the sheet's metered
    GB/day (`echo_gb_day`). A read-back saved before that, and the K360 backup's
    raw fill, echo through the sheet's total raw GB/day.
    """
    readback = readback or {}
    if readback.get("echo_gb_day") is not None:
        return readback.get("echo_gb_day")
    return readback.get("total_raw_gb_day")


def observability_readback(readback: dict, basis: str) -> dict:
    """Observability read-back with the echo for the basis that was written.

    Metered basis (Option 1): the sheet prices `Estimator_Reference!C2` plus
    `Estimator_Reference-Metrics!C2`, the GB/day it derived from G19 and G20, so
    their sum is the echo and the metered GB/day. Raw basis (Option 2): the echo
    is G28, the total raw GB/day.
    """
    out = dict(readback or {})
    if basis == "metered":
        parts = [money(out.get("metered_ex_metrics_gb_day")), money(out.get("metered_metrics_gb_day"))]
        echo = None if any(part is None for part in parts) else sum(parts)
        out["echo_gb_day"] = echo
        out["metered_gb_day"] = echo
        out["total_raw_gb_day"] = None
    else:
        out["echo_gb_day"] = out.get("total_raw_gb_day")
    out["ingest_basis"] = basis
    return out


def echo_gate(written_bulk: float, sheet_raw) -> dict:
    """Accept a sheet only when its echo is within `echo_tolerance` of the GB/day written."""
    sheet = money(sheet_raw)
    try:
        written = float(written_bulk)
    except (TypeError, ValueError):
        written = 0.0
    tolerance = echo_tolerance(written)
    accepted = sheet is not None and abs(sheet - written) <= tolerance
    return {
        "written_bulk": round(written, 3),
        "sheet_gb_day": None if sheet is None else round(sheet, 3),
        "tolerance": round(tolerance, 4),
        "accepted": accepted,
    }


def observability_echo_gate(mix: dict | None, readback: dict | None) -> dict:
    """Accept a diagnostic Observability sheet only when G19 and G20 each echo.

    G19 is the GB/day the sheet priced for logs plus traces. G20 is metrics.
    Each cell uses the same tolerance as a single echo. A blank cell fails that
    cell. A swap that keeps the same total fails.
    """
    mix = mix or {}
    readback = readback or {}
    written_logs = float(mix.get("logs_gb_day") or 0) + float(mix.get("traces_gb_day") or 0)
    written_metrics = float(mix.get("metrics_gb_day") or 0)
    logs = echo_gate(written_logs, readback.get("metered_ex_metrics_gb_day"))
    metrics = echo_gate(written_metrics, readback.get("metered_metrics_gb_day"))
    sheet_sum = None
    if logs["sheet_gb_day"] is not None and metrics["sheet_gb_day"] is not None:
        sheet_sum = round(logs["sheet_gb_day"] + metrics["sheet_gb_day"], 3)
    written_sum = round(logs["written_bulk"] + metrics["written_bulk"], 3)
    return {
        "written_bulk": written_sum,
        "sheet_gb_day": sheet_sum,
        "tolerance": round(echo_tolerance(written_sum), 4),
        "accepted": bool(logs["accepted"] and metrics["accepted"]),
        "written_logs_traces": logs["written_bulk"],
        "written_metrics": metrics["written_bulk"],
        "sheet_logs_traces": logs["sheet_gb_day"],
        "sheet_metrics": metrics["sheet_gb_day"],
    }


def _integrity_row(name: str, score, passed, detail: str) -> dict:
    return {
        "check": name,
        "score": None if score is None else round(float(score), 3),
        "passed": passed,
        "detail": detail,
    }


def _echo_component(label: str, written, sheet_raw, parts: list, passes: list, bits: list) -> None:
    """One echo comparison: score 1 − gap ÷ tolerance, pass when the gap is within the tolerance."""
    written = float(written or 0)
    sheet = money(sheet_raw)
    if sheet is None:
        parts.append(0.0)
        passes.append(False)
        bits.append(f"{label} echo was blank or unreadable")
        return
    tolerance = echo_tolerance(written)
    gap = abs(sheet - written)
    parts.append(1.0 - min(gap / tolerance, 1.0))
    passes.append(gap <= tolerance)
    bits.append(f"{label} echo is {gap:.3f} GB/day from the written bulk (tolerance {tolerance:.3f})")


def _e73_component(label: str, e73, parts: list, passes: list, bits: list) -> None:
    amount = money(e73)
    if amount is None:
        parts.append(0.0)
        passes.append(False)
        bits.append(f"{label}E73 was blank or unreadable")
    elif abs(amount) > 0.005:
        parts.append(0.0)
        passes.append(False)
        bits.append(f"{label}E73 is {amount:g}")
    else:
        parts.append(1.0)
        passes.append(True)
        bits.append(f"{label}E73 is 0")


def _sheet_echo(observability: dict, security: dict, regional: list[dict] | None = None) -> dict:
    """Echo distance and a parsed E73 of 0, on every sheet that was read. A missing sheet is left out.

    The fleet sheets and each regional sheet count: a solution sized per region has no fleet
    sheet, and its regional sheets carry the whole quote.
    """
    parts: list[float] = []
    passes: list[bool] = []
    bits: list[str] = []
    if observability.get("has_workload") and observability.get("sheet_read"):
        if observability.get("cell_echo"):
            _echo_component("Observability logs and traces", observability.get("written_logs_traces"), observability.get("sheet_logs_traces"), parts, passes, bits)
            _echo_component("Observability metrics", observability.get("written_metrics"), observability.get("sheet_metrics"), parts, passes, bits)
        else:
            _echo_component("The sheet", observability.get("written_bulk"), observability.get("sheet_raw"), parts, passes, bits)
    if security.get("has_workload") and security.get("sheet_read"):
        if "sheet_raw" in security:
            _echo_component("The Security sheet (G5)", security.get("written_bulk"), security.get("sheet_raw"), parts, passes, bits)
        _e73_component("", security.get("e73"), parts, passes, bits)
    for sheet in regional or []:
        where = f"{str(sheet.get('solution') or '').title()} {sheet.get('region') or ''}".strip()
        if sheet.get("cell_echo"):
            _echo_component(f"{where} logs and traces", sheet.get("written_logs_traces"), sheet.get("sheet_logs_traces"), parts, passes, bits)
            _echo_component(f"{where} metrics", sheet.get("written_metrics"), sheet.get("sheet_metrics"), parts, passes, bits)
        else:
            _echo_component(f"{where}", sheet.get("written_bulk"), sheet.get("sheet_raw"), parts, passes, bits)
        if sheet.get("solution") == "security":
            _e73_component(f"{where} ", sheet.get("e73"), parts, passes, bits)
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


def _eis_completeness_row(eis: dict | None) -> dict:
    """EIS dollars are allowed only from complete filtered totals."""
    if not eis or eis.get("status") == "not_applicable":
        return _integrity_row("Search EIS data", None, None, "No quotable Search EIS estimate applies.")
    status = eis.get("status")
    if status == "complete":
        basis = eis.get("usage_basis") or "verified deployment totals"
        if basis == "quote_window_daily":
            detail = f"Daily SKU data reconciles to complete deployment totals over {int(eis.get('usage_days') or 0)} quote-window days."
        else:
            return _integrity_row("Search EIS data", 0.0, False, "EIS lacks exact quote-window daily telemetry.")
        return _integrity_row("Search EIS data", 1.0, True, detail)
    reason = str(eis.get("reason") or "filtered deployment totals are incomplete")
    return _integrity_row("Search EIS data", 0.0, False, f"EIS was not priced: {reason}.")


def _retention_row(coverage: dict | None) -> dict:
    """Share of bulk a configured delete policy or data-stream retention ages.

    A deployment counts while the store it leaves at the 30-day default is inside
    `RETENTION_ASSUMED_MAX_SHARE`, so one unconfigured index does not drop a
    configured cluster's whole bulk to zero. Past that share the deployment is
    blended or assumed and contributes nothing. More than that share of fleet
    bulk on an assumed age is a warning: the quote is still produced, because
    the written month is the index average, not this share.
    """
    if not coverage:
        return _integrity_row("Retention basis", None, None, "This run has no diagnostic coverage.")
    bulk = float(coverage.get("bulk_gb_day") or 0)
    if bulk <= 0:
        return _integrity_row("Retention basis", None, None, "No bulk to weight.")
    if coverage.get("retention_policy_bulk_gb_day") is None:
        return _integrity_row("Retention basis", None, None, "Configured retention was not recorded for this run.")
    limit = float(coverage.get("retention_assumed_max_share") or RETENTION_ASSUMED_MAX_SHARE)
    policy = max(0.0, float(coverage.get("retention_policy_bulk_gb_day") or 0))
    score = min(policy / bulk, 1.0)
    partial = float(coverage.get("retention_policy_within_threshold_gb_day") or 0)
    detail = (
        f"{policy:.3f} GB/day of {bulk:.3f} ({score * 100:.1f}%) is on deployments a delete policy or "
        f"data-stream retention ages, allowing up to {limit * 100:.0f}% of a deployment's store at the "
        f"30-day default. The bar is {(1 - limit) * 100:.0f}%."
    )
    if partial > 0:
        detail += f" {partial:.3f} GB/day of that is on deployments inside the threshold, not fully configured."
    if score >= 1 - limit:
        return _integrity_row("Retention basis", score, True, detail)
    row = _integrity_row(
        "Retention basis", score, True,
        detail + f" More than {limit * 100:.0f}% of bulk is priced at the 30-day assumed default. "
        "The quote is still produced.",
    )
    row["warning"] = True
    return row


def _retention_month_row(retention: dict | None) -> dict:
    """The month written to every sheet: the index average, or the default.

    The month sets the estimator's retained-data ladder, so a month nobody
    measured is a warning beside the quote. It does not fail the quote, the way
    Rate window and Counter age do not.
    """
    retention = retention or {}
    months = retention.get("months")
    if months is None or not retention.get("has_bulk"):
        return _integrity_row("Retention month", None, None, "No Observability or Security month was written.")
    months = int(months)
    explicit = retention.get("explicit_days")
    if explicit:
        return _integrity_row(
            "Retention month", 1.0, True,
            f"{months} months was set by --retention-days ({int(explicit)} days).",
        )
    count = retention.get("index_count")
    if count:
        days = retention.get("index_days")
        measured = f", {float(days):,.1f} days across {int(count):,} indexes" if days is not None else f" across {int(count):,} indexes"
        return _integrity_row(
            "Retention month", 1.0, True,
            f"{months} months is the average retention across indexes, rounded up{measured}.",
        )
    row = _integrity_row(
        "Retention month", 0.0, True,
        f"No index retention age was counted, so every sheet was written at the {months}-month default "
        "instead of the index average. Retention sets the estimator's retained-data ladder. "
        "The quote is still produced.",
    )
    row["warning"] = True
    return row


def _rated_row(coverage: dict | None) -> dict:
    """Store in indices that took bulk writes and have no rate, as a share of local store.

    A warning, not a failure. An index that is not in a data stream, has no write
    alias and was never rolled over has no write phase in the bundle, so its bulk
    counter cannot be turned into a rate. That is a limit of the measurement, and
    it is the usual shape of a long-lived index written by name. Failing the quote
    on it withheld every quote on fleets that are built that way, and said nothing
    about whether the bulk that *was* rated is right. Above
    `UNRATED_STORE_MAX_SHARE` the quote says how much ingest the rate did not see.
    """
    unrated = (coverage or {}).get("unrated")
    if not coverage or unrated is None:
        return _integrity_row("Rated ingest", None, None, "The bundles were parsed without the unrated-index count.")
    count = int(unrated.get("indices") or 0)
    store = float(unrated.get("store_gb") or 0)
    share = unrated.get("share_of_store")
    if count == 0:
        return _integrity_row("Rated ingest", 1.0, True, "Every index that took bulk writes is rated or is a rolled generation.")
    tail = " The quote is still produced, and the bulk that was rated is unchanged."
    if share is None:
        row = _integrity_row(
            "Rated ingest", 0.0, True,
            f"{count} indices took bulk writes and have no rate, and there is no local store to "
            f"compare them with.{tail}",
        )
        row["warning"] = True
        return row
    share = min(max(float(share), 0.0), 1.0)
    detail = (
        f"{count} indices took bulk writes and have no rate. They hold {store:,.1f} GB, "
        f"{share * 100:.1f}% of local store. Their ingest is not in bulk GB/day. "
        f"The bar is {UNRATED_STORE_MAX_SHARE * 100:.0f}%."
    )
    names = [f"{row.get('name')} ({float(row.get('store_gb') or 0):,.0f} GB)" for row in (unrated.get("deployments") or [])[:6] if row.get("name")]
    if names:
        detail += " Largest: " + ", ".join(names) + "."
    if share <= UNRATED_STORE_MAX_SHARE:
        return _integrity_row("Rated ingest", 1.0 - share, True, detail)
    row = _integrity_row("Rated ingest", 1.0 - share, True, detail + tail)
    row["warning"] = True
    return row


def _priced_row(priced: dict | None) -> dict:
    """Share of the ECH deployments' billed cost that belongs to deployments with a serverless price."""
    if priced and priced.get("status") == "unavailable":
        reason = str(priced.get("reason") or "daily per-deployment billing could not be verified")
        return _integrity_row("Priced share", 0.0, False, f"Daily deployment billing is unavailable or unreconciled: {reason}.")
    if not priced or priced.get("share_pct") is None:
        return _integrity_row("Priced share", None, None, "This run has no per-deployment billing to split.")
    share = min(max(float(priced["share_pct"]), 0.0), 100.0)
    minimum = float(priced.get("min_pct") if priced.get("min_pct") is not None else 90.0)
    unpriced = int(priced.get("unpriced") or 0)
    detail = f"Deployments with a serverless price carry {share:.1f}% of the ECH deployments' billed cost. The bar is {minimum:g}%."
    if unpriced:
        detail += f" {unpriced} {'deployment has' if unpriced == 1 else 'deployments have'} no price, ${float(priced.get('unpriced_mo') or 0):,.0f}/mo"
        names = [str(name) for name in (priced.get("names") or []) if name]
        detail += (": " + ", ".join(names)) if names else ""
        detail += "."
    return _integrity_row("Priced share", share / 100.0, share >= minimum, detail)


# Packing projects together can only save money or hold it level: it removes a project's fixed ingest
# and minimum search power. A step that costs more than the step before it means an estimator
# returned a total that does not fall as the fleet merges (the es3 model is not additive, and the
# Observability sheet's ladders are). The read-back stays the dollar; the quote does not pass.
PLAN_RISE_SHARE = 0.02
PLAN_RISE_FLOOR_USD = 5.0


def _plan_row(plan: list[dict] | None) -> dict:
    """Deployment-count bars must not rise as projects merge."""
    bars = [row for row in (plan or []) if isinstance(row.get("monthly"), (int, float)) and not isinstance(row.get("monthly"), bool)]
    if len(bars) < 2:
        return _integrity_row("Estimator monotone", None, None, "The deployment-count plan has fewer than two priced steps.")
    rises = []
    for earlier, later in zip(bars, bars[1:]):
        before, after = float(earlier["monthly"]), float(later["monthly"])
        if after > before * (1 + PLAN_RISE_SHARE) + PLAN_RISE_FLOOR_USD:
            rises.append(
                f"{later.get('label') or '?'} ({later.get('projects')} projects) ${after:,.0f} is above "
                f"{earlier.get('label') or '?'} ({earlier.get('projects')} projects) ${before:,.0f}"
            )
    steps = len(bars) - 1
    if not rises:
        return _integrity_row("Estimator monotone", 1.0, True, f"{steps} merge steps, none costs more than the step before it.")
    return _integrity_row(
        "Estimator monotone", 1.0 - len(rises) / steps, False,
        f"{len(rises)} of {steps} merge steps cost more than the step before: " + "; ".join(rises[:4]) + ".",
    )


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


# Short-window bulk, restarted counters, and the floor gap live in constants.py.


def region_match_how(value) -> str:
    """The match recorded on a sheet: exact, alias, nearest, provider_default, none, or unknown."""
    text = str(value or "").strip()
    if not text:
        return ""
    found = re.findall(r"\((exact|alias|nearest|provider_default|none)\b", text, re.IGNORECASE)
    if found:
        return found[-1].lower()
    low = text.lower()
    if low in {"exact", "alias", "nearest", "provider_default", "none"}:
        return low
    return "unknown"


def _quote_present_row(solutions: list[dict]) -> dict:
    """A solution with a workload and no monthly price is not a quote."""
    watched = [row for row in solutions if row.get("has_workload") and "serverless_mo" in row]
    if not watched:
        return _integrity_row("Quote present", None, None, "No solution has a workload.")
    missing = []
    for row in watched:
        monthly = row.get("serverless_mo")
        if monthly is None or float(monthly) == 0:
            missing.append(str(row.get("solution") or "a solution"))
    if not missing:
        return _integrity_row("Quote present", 1.0, True, "Every solution with a workload has a serverless monthly price.")
    return _integrity_row(
        "Quote present",
        0.0,
        False,
        f"{', '.join(missing)} has a workload and no serverless monthly price.",
    )


def _region_fidelity_row(sheets: list[dict]) -> dict:
    """Where each sheet was written, against the region it was meant for.

    A warning, not a failure. A region that is not on the estimator list is
    priced at the same provider's default (AWS us-east-1, Azure eastus, GCP
    us-east1) and that read-back is a real dollar, so withholding the whole
    quote over it leaves the fleet with nothing. The substitute is named here,
    on the cover and in the notes, because the price is for that region and not
    the one the deployments run in. Written to another cloud, or with no region
    recorded, is reported first: those are the ones worth acting on.
    """
    written = [row for row in sheets if row.get("region_match") or row.get("readback_rejected")]
    if not written:
        return _integrity_row("Region fidelity", None, None, "No region was written to an estimator.")
    bad = []
    for row in written:
        how = region_match_how(row.get("region_match"))
        region = str(row.get("region") or "").strip()
        if how not in {"exact", "alias"}:
            bad.append(f"{region or 'a region'} ({how or 'none'})")
            continue
        if how == "alias":
            # An alias is only checkable against the region the sheet was scoped to. Say that
            # the region is missing rather than reporting a region-number mismatch that was
            # never measured.
            if not region:
                bad.append(f"{str(row.get('solution') or 'a sheet')} recorded an alias match and no region")
            elif not _alias_same_number(region, str(row.get("region_match") or "")):
                bad.append(f"{region} (alias of a different region)")
        if not alias_provider_matches(str(row.get("region_match") or ""), row.get("provider")):
            bad.append(f"{region or 'a region'} (written to another cloud)")
    if bad:
        # Another cloud, or a region nobody recorded, first: a substitute on the same cloud is
        # a deliberate fallback, those two are not.
        serious = [item for item in bad if "another cloud" in item or "no region" in item]
        rest = [item for item in bad if item not in serious]
        row = _integrity_row(
            "Region fidelity",
            round(1.0 - len(bad) / len(written), 3),
            True,
            f"{len(bad)} of {len(written)} written regions are not an exact or same-region alias: "
            + "; ".join(serious + rest)
            + ". Each is priced at that read-back, for the region named, not the one the "
            "deployments run in. The quote is still produced.",
        )
        row["warning"] = True
        return row
    return _integrity_row("Region fidelity", 1.0, True, f"{len(written)} written regions match the estimator list.")


_ALIAS_SPLIT = re.compile(r"[^a-z0-9]+")
_GEO_WORDS = {
    "us": "us", "usa": "us", "united": "us",
    "eu": "eu", "europe": "eu",
    "ap": "ap", "apac": "ap", "asia": "ap",
    "ca": "ca", "canada": "ca",
    "sa": "sa", "me": "me", "af": "af", "il": "il",
    "north": "north", "south": "south", "east": "east", "west": "west", "central": "central",
    "northeast": "northeast", "northwest": "northwest",
    "southeast": "southeast", "southwest": "southwest",
}


def _region_shape(text: str) -> tuple[frozenset[str], list[str]]:
    """Geography words and digits in a region name, however the name is spelled.

    `us-east-1`, `us-east1` and `useast1` are the same place. `ap-southeast-1`
    and `ap-northeast-1` are not: they share a number and differ in a direction.
    """
    parts = [p for p in _ALIAS_SPLIT.split(str(text or "").lower()) if p]
    words, digits = set(), []
    for part in parts:
        for chunk in re.findall(r"[a-z]+|\d+", part):
            if chunk.isdigit():
                digits.append(chunk)
            elif chunk in _GEO_WORDS:
                words.add(_GEO_WORDS[chunk])
    return frozenset(words), digits


def _alias_same_number(region: str, recorded: str) -> bool:
    """An alias may respell the name. It may not change the place, the number or the provider."""
    option = recorded.split("(")[0]
    want_words, want_digits = _region_shape(region)
    got_words, got_digits = _region_shape(option)
    if want_digits != got_digits:
        return False
    # The provider prefix only appears on the recorded side ("AWS: us-east-1"), so compare
    # the geography the two sides do share rather than requiring identical word sets.
    if want_words and got_words and not want_words <= got_words:
        return False
    return True


def alias_provider_matches(region_match: str, provider: str | None) -> bool:
    """The cloud named on the sheet is the cloud the deployments run on. Unknown passes."""
    named = re.match(r"\s*(AWS|AZURE|GCP)\b", str(region_match or ""), re.IGNORECASE)
    if not named or not provider:
        return True
    want = str(provider).strip().lower()
    alias = {"gcp": "gcp", "google": "gcp", "aws": "aws", "amazon": "aws", "azure": "azure", "microsoft": "azure"}
    return alias.get(want, want) == named.group(1).lower()


def _plan_consistency_row(block: dict | None) -> dict:
    """The floor matches the headline, and no bar prices a solution the headline left out."""
    block = block or {}
    if block.get("bar_prices_unquoted"):
        return _integrity_row(
            "Plan consistency",
            0.0,
            False,
            "A deployment-count bar prices a solution the headline did not quote.",
        )
    headline = block.get("headline")
    floor = block.get("floor_monthly")
    floor_projects = block.get("floor_projects")
    regional = block.get("regional_projects")
    if headline is None or floor is None or floor_projects is None or regional is None:
        return _integrity_row("Plan consistency", None, None, "The plan has no floor to compare with the headline.")
    if int(floor_projects) != int(regional):
        return _integrity_row("Plan consistency", None, None, "The floor is not one project per solution and region.")
    gap = abs(float(floor) - float(headline))
    limit = abs(float(headline)) * PLAN_HEADLINE_SHARE + PLAN_HEADLINE_USD
    passed = gap <= limit
    return _integrity_row(
        "Plan consistency",
        1.0 if passed else 0.0,
        passed,
        f"Floor ${float(floor):,.2f} versus headline ${float(headline):,.2f}, gap ${gap:,.2f}. The bar is 1% plus $5.",
    )


def _billing_window_row(window: dict | None) -> dict:
    """Fail when an earlier priced month is under half the latest month.

    The comparison is then the latest month. Fewer than two months is n/a.
    """
    window = window or {}
    months = list(window.get("months") or [])
    if len(months) < 2 or window.get("stable") is None:
        return _integrity_row("Billing window", None, None, "Fewer than two priced months were recorded.")
    latest = months[-1]
    if window.get("stable"):
        return _integrity_row(
            "Billing window",
            1.0,
            True,
            f"Each of {len(months)} months is at least half of {latest.get('month')} (${float(latest.get('cost') or 0):,.2f}).",
        )
    earlier = ", ".join(f"{row.get('month')} ${float(row.get('cost') or 0):,.2f}" for row in months[:-1])
    return _integrity_row(
        "Billing window",
        0.0,
        False,
        f"The comparison is {latest.get('month')} ${float(latest.get('cost') or 0):,.2f}. Not averaged: {earlier}.",
    )


def _share_limit_row(name: str, coverage: dict | None, key: str, limit: float, noun: str, *, blocks_quote: bool = True) -> dict:
    if not coverage or not float(coverage.get("bulk_gb_day") or 0):
        return _integrity_row(name, None, None, "No bulk to compare.")
    if coverage.get(key) is None:
        return _integrity_row(name, None, None, f"{noun.capitalize()} was not recorded.")
    bulk = float(coverage.get("bulk_gb_day") or 0)
    part = float(coverage.get(key) or 0)
    share = part / bulk if bulk else 0.0
    passed = share <= limit
    detail = f"{part:.3f} GB/day of {bulk:.3f} is {noun} ({share * 100:.1f}%). The bar is {limit * 100:.0f}%."
    row = _integrity_row(name, round(max(0.0, 1.0 - min(share, 1.0)), 3), passed, detail)
    if not blocks_quote and passed is False:
        row["passed"] = True
        row["warning"] = True
        row["detail"] = detail + " The quote is still produced."
    return row


def integrity_score(facts: dict) -> dict:
    """Quote integrity, 0 to 1. The mean skips a check that does not apply.

    The quote passes only when every scored check passes its own bar: 100% of
    the checks, whatever the mean is. One failed check fails the quote. Rate
    window, Counter age, Rated ingest, Region fidelity, Retention basis, and
    Retention month warn and do not fail the quote: the first two when their
    share is above 25%, Rated ingest when unrated store is above 10%, Region
    fidelity when a sheet was written to a substitute region, Retention basis
    when more than 10% of bulk sits at the 30-day assumed age, and Retention
    month when the written month is the default rather than the index average.
    Each names a weakness in the inputs or in where a sheet was written, not a
    dollar that was read back wrong.
    The score is the mean and is reported beside that verdict. It
    does not decide it, because a check can pass its bar and score under 1
    (an echo gap inside the tolerance, or unrated store under 5%).
    This is not a serverless bill.
    """
    coverage = facts.get("coverage")
    checks = [
        _sheet_echo(facts.get("observability") or {}, facts.get("security") or {}, list(facts.get("regional_sheets") or [])),
        _coverage_row(coverage),
        _retention_row(coverage),
        _retention_month_row(facts.get("retention_month")),
        _rated_row(coverage),
        _priced_row(facts.get("priced")),
        _plan_row(facts.get("plan")),
        _direct_row(list(facts.get("solutions") or [])),
        _line_row(facts.get("headline_monthly"), list(facts.get("lines") or [])),
        _year_row(list(facts.get("lines") or [])),
        _eis_completeness_row(facts.get("eis")),
        _region_fidelity_row(list(facts.get("written_regions") or [])),
        _quote_present_row(list(facts.get("solutions") or [])),
        _plan_consistency_row(facts.get("plan_consistency")),
        _share_limit_row("Rate window", coverage, "bulk_short_window_gb_day", RATE_WINDOW_MAX_SHARE, "rated over a short window", blocks_quote=False),
        _share_limit_row("Counter age", coverage, "bulk_restart_gb_day", COUNTER_AGE_MAX_SHARE, "from a restarted bulk counter", blocks_quote=False),
        _billing_window_row(facts.get("billing_window")),
    ]
    applicable = [row for row in checks if row.get("passed") is not None]
    scored = [row["score"] for row in applicable if row.get("score") is not None]
    score = None if not scored else round(sum(scored) / len(scored), 3)
    passed = None if score is None else all(row["passed"] is True for row in applicable)
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


def security_readback(readback: dict, retention_months) -> dict:
    """Security read-back with the steady month and the add-on lines made explicit.

    E74 is the sheet's steady monthly total and the monthly quote.

    E74 also sums the endpoint, cloud, workflow, LLM, and SOAR lines. Whatever
    E74 holds beyond ingest, retention, support, and E73 is `other_addons_monthly`.
    A non-zero amount is folded into `cps_addon_monthly`, the value every Security
    gate already refuses, and the sheet's own E73 stays in `cps_addon_e73`.
    """
    out = dict(readback or {})
    month12 = money(out.get("monthly_steady_state"))
    out["month12_monthly"] = month12
    cps = money(out.get("cps_addon_monthly"))
    parts = [money(out.get(key)) for key in ("ingest_monthly", "retention_monthly", "support_monthly")]
    if month12 is not None and cps is not None and all(part is not None for part in parts):
        other = round(month12 - cps - sum(parts), 2)
        out["other_addons_monthly"] = other
        if abs(other) > 0.005:
            out["cps_addon_e73"] = out.get("cps_addon_monthly")
            out["cps_addon_monthly"] = round(cps + other, 2)
            out["addon_note"] = f"Security add-on lines E63 to E72 total ${other:,.2f}/mo on the sheet."
    del retention_months
    out["steady_basis"] = "month 12 (E74)"
    return out


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
