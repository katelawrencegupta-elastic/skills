"""Shared imports and cell maps for the serverless sizing scripts."""
from __future__ import annotations






import re


import sys




from pathlib import Path

HERE = Path(__file__).resolve().parent

sys.path.insert(0, str(HERE))

from skill_paths import find_skill, scripts_dir  # noqa: E402

ORG_SKILL = find_skill("k360-org-report", HERE)

_account_root = find_skill("k360-account-analysis", HERE)

K360_SCRIPTS = scripts_dir(_account_root)

sys.path.insert(0, str(K360_SCRIPTS))

sys.path.insert(0, str(ORG_SKILL / "scripts"))

from privacy import seal_dir, seal_file, seal_text  # noqa: E402

from quote_checks import (  # noqa: E402
    addon_off_value, cell_range, check_value_ranges, e73_blocks_quote, echo_gate, layout_ranges,
    observability_echo_gate,
    money as sheet_money, observability_readback, security_readback,
)

try:
    from sheets_auth import get_creds  # noqa: E402
except ImportError as e:  # pragma: no cover
    raise SystemExit(f"sheets_auth.py not found under {ORG_SKILL}/scripts — set K360_ORG_SKILL_DIR") from e

DEFAULT_O11Y = "1tUdwJbTmd4helFOzEHMi_gpHw7qu_aCgprjlqmHh2hs"  # Observability estimator v1.22

DEFAULT_SECURITY = "1b91Uoa2e4vBrep_7I4RoY_8Mr19poqaUgWl7LqhIOqY"

_SHEET_LABELS = frozenset({
    "Complete", "Essentials", "Enterprise", "Standard", "Premium",
    "Gold", "Platinum", "No", "Yes", "no", "N", "FALSE", "False",
    "0%", "100%",
})

_NUMBER_TEXT = re.compile(r"\d+(\.\d+)?")

_FORMULA_PREFIX = "=+-@"

O11Y_TAB = "Observability Estimator"

SEC_TAB = "Security Estimator"

REF_TAB = "Estimator_Reference"

O11Y_READBACK = {
    "total_raw_gb_day": "G28",
    "metered_gb_day": "G29",
    # GB/day the two ingest ladders price. On the metered basis these are G19 and G20 turned back into GB/day.
    "metered_ex_metrics_gb_day": "Estimator_Reference!C2",
    "metered_metrics_gb_day": "Estimator_Reference-Metrics!C2",
    "monthly_steady_state": "D38",
    "annual_year1": "E38",
    "annual_steady_state": "F38",
}

SEC_READBACK = {
    "metered_gb_day": "G5",
    "monthly_steady_state": "E74",
    "annual_year1": "D36",
    "annual_year2": "E36",
    "annual_year3": "F36",
    "annual_steady_state": "E36",
    "cps_addon_monthly": "E73",
    # The three lines a SIEM quote is made of, so any other line in E74 shows up.
    "ingest_monthly": "E61",
    "retention_monthly": "E62",
    "support_monthly": "E69",
}

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

FOLDER_MIME = "application/vnd.google-apps.folder"

GSHEET_MIME = "application/vnd.google-apps.spreadsheet"

_REGION_OPT = re.compile(r"^(AWS|AZURE|GCP)\s*:?\s*([a-z0-9\-]+)", re.IGNORECASE)


class TemplateMismatch(RuntimeError):
    """The estimator copy is not the layout these cell addresses were written for."""

SEC_ADDON_CELLS = [
    "G11", "G12", "G14", "G15", "G16", "G17", "G18", "G20",
    "G23", "G24", "G25", "G27", "G28", "G29", "G30", "G31", "G32", "G33", "G34",
]

from constants import DAYS_PER_MONTH, HOURS_PER_MONTH

_ALLOW_NEAREST = False

ESTIMATE_FOLDER_NAME = "generated_estimates"

PLAN_FOLDER_NAME = "plan_sheets"

COMPRESSION_FOLDER_NAME = "compression_applied"

COMPRESSION_NAME_MARK = "compression-applied"

ESTIMATE_NAME_MARK = "serverless_estimate"

LEGACY_RETENTION_FOLDERS = ("full_retention", "30d")

LEGACY_ESTIMATE_DIRS = tuple(
    prefix + (name,) for prefix in ((), (COMPRESSION_FOLDER_NAME,)) for name in LEGACY_RETENTION_FOLDERS
)
