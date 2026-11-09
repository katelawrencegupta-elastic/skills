#!/usr/bin/env python3
"""Shared exec|full report mode helpers for the org-report skill."""
from __future__ import annotations

REPORT_CHOICES = ("exec", "full")
DEFAULT_REPORT = "exec"

# PDF / HTML stem suffix: <customer>_<date>_{Exec|Full}_report.pdf
PDF_SUFFIX = {
    "exec": "Exec_report",
    "full": "Full_report",
}

# Suggested canvas filename: <slug>-{exec|full}-report.canvas.tsx
CANVAS_KIND = {
    "exec": "exec-report",
    "full": "full-report",
}

# Agent canvas layout (see references/{exec,full}-report.md)
CANVAS_LAYOUT = {
    "exec": "TOC → condensed §1–8 → §9 → §10",
    "full": "TOC → full §1–10 (each section expanded)",
}


def normalize_report(value: str | None) -> str:
    v = (value or DEFAULT_REPORT).strip().lower()
    if v not in REPORT_CHOICES:
        raise SystemExit(f"--report must be one of {REPORT_CHOICES}, got {value!r}")
    return v


def pdf_stem(run_key: str, report: str) -> str:
    """Basename without extension: <run_key>_Exec_report"""
    return f"{run_key}_{PDF_SUFFIX[normalize_report(report)]}"


def pdf_name(run_key: str, report: str) -> str:
    return f"{pdf_stem(run_key, report)}.pdf"


def html_name(run_key: str, report: str) -> str:
    return f"{pdf_stem(run_key, report)}.html"


def canvas_suggestion(org_slug: str, report: str) -> str:
    """Kebab canvas file hint from org slug (underscores → nothing special)."""
    slug = (org_slug or "customer").replace("_", "-").lower()
    # collapse multi-dash
    while "--" in slug:
        slug = slug.replace("--", "-")
    return f"{slug}-{CANVAS_KIND[normalize_report(report)]}.canvas.tsx"


def add_report_arg(parser) -> None:
    parser.add_argument(
        "--report",
        choices=REPORT_CHOICES,
        default=DEFAULT_REPORT,
        help="exec = shortened exec report; full = expanded §1–10 report (default: exec)",
    )
