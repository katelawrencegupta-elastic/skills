"""Pure checks for a diagnostic serverless quote. No network and no sheets."""
from __future__ import annotations

import re

G26_TOLERANCE_GB_DAY = 0.51


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


def g26_gate(written_bulk: float, sheet_raw) -> dict:
    """Accept Observability only when G26 is within 0.51 GB/day of the bulk written."""
    sheet = money(sheet_raw)
    try:
        written = float(written_bulk)
    except (TypeError, ValueError):
        written = 0.0
    accepted = sheet is not None and abs(sheet - written) <= G26_TOLERANCE_GB_DAY
    return {
        "written_bulk": round(written, 3),
        "sheet_raw_gb_day": None if sheet is None else round(sheet, 3),
        "accepted": accepted,
    }


def e73_blocks_quote(value) -> bool:
    """E74 includes E73. A non-zero add-on means the template was not cleared."""
    amount = money(value)
    return amount is not None and abs(amount) > 0.005


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
