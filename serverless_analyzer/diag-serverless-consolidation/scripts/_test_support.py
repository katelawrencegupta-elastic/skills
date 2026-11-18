"""Shared fixtures for the offline suites.

The leading underscore keeps this module out of unittest discovery: it holds the
account builders and bundle helpers the suites share, not tests of its own.
"""
from __future__ import annotations

import json
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(HERE))
from skill_paths import find_skill, scripts_dir  # noqa: E402

K360 = scripts_dir(find_skill("k360-account-analysis", HERE))
sys.path.insert(0, str(K360))

from admin_diags import analyze_bundle  # noqa: E402
from consolidation import apply_quote_lookback  # noqa: E402


def _quote_account(*, g28: float, e73, observability_monthly: float = 100) -> dict:
    return {
        "ecu": {"lookback_days": 365, "total_lookback": 120, "monthly_avg_complete_months": 10},
        "storage": {"data_stored_gb": 0},
        "solutions": {"by_deployment": [], "totals": {}},
        "meta": {"org": {}, "customer": "Example", "run_key": "example"},
        "workload": {},
        "sizing": {
            "triggered": True,
            "ingest_source": "diagnostics",
            "notes": [],
            "consolidated": {
                "observability": {
                    "stored_gb": 0, "daily_raw_gb": 10, "retention_months": 1,
                    "deployments": 1, "ecu_monthly_avg": 5,
                },
                "security": {
                    "stored_gb": 0, "daily_raw_gb": 4, "retention_months": 3,
                    "deployments": 1, "ecu_monthly_avg": 5,
                },
                "search": {"stored_gb": 0, "daily_raw_gb": 0, "deployments": 0},
            },
            "estimators": {
                "observability": {
                    "readback": {
                        "monthly_steady_state": observability_monthly,
                        "total_raw_gb_day": g28,
                        "metered_gb_day": 16,
                        "annual_year1": observability_monthly * 12,
                    },
                },
                "security": {
                    "readback": {
                        "monthly_steady_state": 80,
                        "cps_addon_monthly": e73,
                        "echo_gb_day": 4,
                        "annual_year1": 960,
                    },
                },
                "search": {
                    "profiles": [
                        {"Search Power Setting": "performant", "Total $/month": 999},
                        {"Search Power Setting": "performant", "Total $/month": 1200},
                    ],
                },
            },
            "deployment_plan": {
                "bars": [
                    {"label": "0%", "monthly": 40, "projects": 4},
                    {"label": "100%", "monthly": None, "projects": 1, "reused_headline": True},
                ],
            },
        },
    }


def _stamp_reconciled_billing(acc: dict) -> None:
    """One billed day with no deployments, so priced share is unscored rather than failed."""
    day = "2026-11-01"
    ecu = acc.setdefault("ecu", {})
    ecu["quote_lookback_days"] = 1
    ecu["daily_days"] = [day]
    ecu["daily_totals"] = [1.0]
    ecu["daily_deployment_costs"] = {
        "complete": True,
        "days": [day],
        "org_daily_totals": [1.0],
        "by_deployment": {},
        "status": {"complete": True},
    }


def _stamp_search_eis(acc: dict, name: str, *, eis: float, tokens: float, ml: float) -> None:
    """One reconciled quote-window day of Search inference billing, plus complete telemetry."""
    day = "2026-11-01"
    _stamp_reconciled_billing(acc)
    acc["workload"] = {"quote_window_telemetry": {"status": "complete"}}
    acc["ai_ml"] = {
        "billing_by_deployment_complete": True,
        "billing_by_deployment": {
            name: {"eis_ecu": eis, "million_tokens": tokens, "ml_node_ecu": ml},
        },
        "daily_usage_complete": True,
        "daily_usage_by_deployment": {
            name: {
                "days": [day],
                "eis_ecu": [eis],
                "million_tokens": [tokens],
                "ml_node_ecu": [ml],
                "models": [["jina-embeddings-v3"]],
            },
        },
        "by_deployment": [{
            "name": name,
            "ml_node_ecu": ml,
            "models": ["jina-embeddings-v3"],
            "model_labels": ["Jina"],
        }],
    }

COLLECTION_MS = 20 * 86_400_000


def _write_phase(write_days: int, *, age_days: int) -> tuple[int, int]:
    created = COLLECTION_MS - age_days * 86_400_000
    return created, created + write_days * 86_400_000


def _analyze(files: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bundle.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, body in files.items():
                archive.writestr(name, json.dumps(body))
        return analyze_bundle(path, captured=datetime(2026, 11, 1, tzinfo=timezone.utc))


def _guard_args(**over):
    """Args for the clean-tree guard in main(): real values, so a branch is not taken by accident."""
    import argparse
    base = dict(lookback_days=90, allow_dirty=False, retention_days=None, preflight=False,
                env=".env", reuse=None, skip_vpn_check=True, min_priced_ecu_pct=90.0,
                ech_scope="priced", keep_serverless_spend=False)
    return argparse.Namespace(**{**base, **over})


class _SheetCall:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.bodies = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def files(self):
        return self

    def batchUpdate(self, spreadsheetId=None, body=None):
        self.bodies.append(body)
        return self

    def copy(self, **kwargs):
        return self

    def list(self, **kwargs):
        return self

    def execute(self):
        return self.payload


def scope_account(**options) -> dict:
    from datetime import date, timedelta
    acc = _quote_account(g28=10, e73=0)
    first = date(2026, 7, 1)
    days = [(first + timedelta(days=i)).isoformat() for i in range(123)]      # July through October
    acc["ecu"].update({
        "daily_days": days,
        "daily_totals": [10.0] * 123,                                          # the org bills 10 a day
        "monthly": {"months": ["2026-07", "2026-08", "2026-09", "2026-10"], "totals": [279, 279, 270, 279]},
        # per day: o11y 5, sec 1, no-bundle 2, off-roster 0.5, serverless 1, and 0.5 with no deployment name
        "monthly_by_deployment": {
            "o11y": [155, 155, 150, 155], "sec": [31, 31, 30, 31], "nobundle": [62, 62, 60, 62],
            "gone": [15.5, 15.5, 15, 15.5],
        },
        "monthly_serverless": [31, 31, 30, 31],
        "quote_lookback_days": 90,
        "daily_deployment_costs": {
            "complete": True,
            "days": days,
            "org_daily_totals": [10.0] * 123,
            "by_deployment": {
                "o11y": [5.0] * 123,
                "sec": [1.0] * 123,
                "nobundle": [2.0] * 123,
                "gone": [0.5] * 123,
                "proj": [1.0] * 123,
            },
            "status": {"complete": True},
        },
    })
    acc["solutions"]["by_deployment"] = [
        {"name": "o11y", "solution": "observability", "platform": "ech", "ecu": 600, "daily_raw_gb": 10, "ingest_source": "diagnostics"},
        {"name": "sec", "solution": "security", "platform": "ech", "ecu": 120, "daily_raw_gb": 4, "ingest_source": "diagnostics"},
        {"name": "nobundle", "solution": "observability", "platform": "ech", "ecu": 240, "daily_raw_gb": 0, "ingest_source": "active_no_bundle"},
        {"name": "proj", "solution": "search", "platform": "serverless", "ecu": 120},
    ]
    if options:
        acc["sizing"]["quote_options"] = options
    apply_quote_lookback(acc, 90)
    return acc
