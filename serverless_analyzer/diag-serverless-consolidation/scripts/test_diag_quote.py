#!/usr/bin/env python3
"""Offline checks for the diagnostic quote. No Google and no sheets."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(HERE))
from skill_paths import find_skill, scripts_dir  # noqa: E402

K360 = scripts_dir(find_skill("k360-account-analysis", HERE))
sys.path.insert(0, str(K360))

from admin_diags import (  # noqa: E402
    _parse_zip, _user_index, admin_base, analyze_bundle, capture_target, capture_url,
    parse_diagnostics, pull_diagnostics, quoted_index_bytes, safe_id,
)
from consolidation import apply_quote_lookback, build_consolidation, regional_quote  # noqa: E402
from diag_account import _sheet_months, apply_diagnostics, compressioncalc_account  # noqa: E402
from diag_inputs import deployment_count_plans, project_count, summarize, whole_months  # noqa: E402
from k360_infra import es_base_and_key  # noqa: E402
from privacy import seal_dir, seal_text, unignored_secret_warnings  # noqa: E402
from quote_checks import addon_off_value, e73_blocks_quote, g26_gate, integrity_score, money  # noqa: E402
from run_consolidation import _compression_totals, _estimator_flags, consolidation_brief, quote_pdf_jobs  # noqa: E402
from render_pdf import consolidation_pdf_stem, pdf_folder_name, render_appendix_html, render_consolidation_html, search_power_section, stat  # noqa: E402
from size_serverless import (  # noqa: E402
    COMPRESSION_FOLDER_NAME, DEFAULT_O11Y, DEFAULT_SECURITY, PLAN_FOLDER_NAME, _sheet_retention,
    assemble_plan_bars, assert_not_template, cell_input_mode, copy_template, estimate_folder_name,
    architecture_regions, estimate_subfolder, estimate_title, estimator_skip, fleet_sheet_region,
    prefer_regional_estimators,
    map_in_order, partition_cells, push_cells,
    _search_volume_scale,
    relocate_local_estimates, require_customer_parent,
)
from vpn_check import tunnel_reaches  # noqa: E402


def _quote_account(*, g26: float, e73, observability_monthly: float = 100) -> dict:
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
                        "total_raw_gb_day": g26,
                        "metered_gb_day": 16,
                        "annual_year1": observability_monthly * 12,
                    },
                },
                "security": {
                    "readback": {
                        "monthly_steady_state": 80,
                        "cps_addon_monthly": e73,
                        "annual_year1": 960,
                    },
                },
                "search": {
                    "profiles": [
                        {"Search Power Setting": "on-demand", "Total $/month": 999},
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


class StoreTests(unittest.TestCase):
    def test_partial_and_system_indexes_are_not_quoted(self):
        self.assertFalse(_user_index("partial-logs"))
        self.assertFalse(_user_index(".ds-.monitoring-000001"))
        self.assertFalse(_user_index(".ds-ilm-history-000001"))
        self.assertTrue(_user_index(".ds-logs-000001"))
        mounted = quoted_index_bytes("partial-snap", {
            "size_in_bytes": 5_000_000_000,
            "total_data_set_size_in_bytes": 50_000_000_000,
        })
        self.assertEqual(mounted["quote"], 0)
        self.assertEqual(mounted["snapshot"], 50_000_000_000)
        local = quoted_index_bytes("logs-000001", {
            "size_in_bytes": 20_000_000_000,
            "total_data_set_size_in_bytes": 80_000_000_000,
        })
        self.assertEqual(local["quote"], 20_000_000_000)
        self.assertEqual(local["dataset"], 80_000_000_000)

    def test_bundle_quotes_local_bytes_and_names_the_snapshot_gap(self):
        files = {
            "diag/indices_stats.json": {
                "indices": {
                    "logs-000001": {"primaries": {"store": {
                        "size_in_bytes": 20_000_000_000,
                        "total_data_set_size_in_bytes": 80_000_000_000,
                    }}},
                    ".ds-logs-000001": {"primaries": {"store": {
                        "size_in_bytes": 10_000_000_000,
                        "total_data_set_size_in_bytes": 10_000_000_000,
                    }}},
                    "partial-snap": {"primaries": {"store": {
                        "size_in_bytes": 5_000_000_000,
                        "total_data_set_size_in_bytes": 50_000_000_000,
                    }}},
                    ".kibana": {"primaries": {"store": {"size_in_bytes": 1_000_000_000}}},
                    ".ds-.monitoring-000001": {"primaries": {"store": {"size_in_bytes": 9_000_000_000}}},
                },
            },
            "diag/commercial/ilm_explain.json": {"indices": {
                "logs-000001": {"managed": True, "policy": "keep"},
                ".ds-logs-000001": {"managed": True, "policy": "short"},
                "partial-snap": {"managed": True, "policy": "keep"},
            }},
            "diag/commercial/ilm_policies.json": {
                "keep": {"policy": {"phases": {"delete": {"min_age": "60d"}}}},
                "short": {"policy": {"phases": {"delete": {"min_age": "0.042d"}}}},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.zip"
            with zipfile.ZipFile(path, "w") as archive:
                for name, body in files.items():
                    archive.writestr(name, json.dumps(body))
            row = analyze_bundle(path, captured=datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(row["store_gb"], 30.0)
        self.assertEqual(row["snapshot_gb"], 110.0)
        self.assertAlmostEqual(row["retention_days"], 40.014, places=3)
        self.assertIsNone(row["compression_ratio"])
        self.assertEqual(row["compressioncalc_accounted_for"], 0.0)


class RetentionTests(unittest.TestCase):
    def test_unmeasured_days_stay_out_of_the_weight(self):
        mixed = summarize([
            {"name": "a", "bulk_gb_day": 10, "retention_days": 0, "store_gb": 1},
            {"name": "b", "bulk_gb_day": 10, "retention_days": 30.416, "store_gb": 1},
        ])
        self.assertEqual(mixed["months_written"], 1)
        self.assertEqual(mixed["retention_unmeasured"], [{"name": "a", "bulk_gb_day": 10.0}])
        empty = summarize([{"name": "a", "bulk_gb_day": 5, "retention_days": None, "store_gb": 1}])
        self.assertIsNone(empty["months_written"])
        self.assertNotIn("G11_retention_months", empty["observability_cells"])
        subday = summarize([{"name": "a", "bulk_gb_day": 2, "retention_days": 0.042, "store_gb": 1}])
        self.assertEqual(subday["months_written"], 1)
        self.assertEqual(subday["retention_unmeasured"], [])
        year = summarize([{"name": "a", "bulk_gb_day": 10, "retention_days": 365, "store_gb": 1}])
        self.assertEqual(year["months_written"], 12)
        self.assertEqual(year["observability_cells"]["G11_retention_months"], 12)
        self.assertEqual(_sheet_months(365 / 30.416), 12)
        self.assertEqual(whole_months(365 / 30.416), 12)
        self.assertEqual(_sheet_retention(365 / 30.416), 12)
        self.assertEqual(_sheet_months(31 / 30.416), 2)
        self.assertEqual(whole_months(61 / 30.416), 3)
        self.assertEqual(_sheet_retention(30 / 30.416), 1)

    def test_account_weight_uses_measured_rows_only(self):
        acc = {
            "deployments": {"active": {"month": "2026-10", "names": ["alpha", "beta", "gamma"], "partial": False}},
            "solutions": {"by_deployment": [
                {"name": "alpha", "platform": "ech", "solution": "observability", "gb": 100, "ecu": 1},
                {"name": "beta", "platform": "ech", "solution": "observability", "gb": 100, "ecu": 1},
                {"name": "gamma", "platform": "ech", "solution": "search", "gb": 50, "ecu": 1},
            ], "totals": {"observability": {}, "security": {}, "search": {}}},
            "sizing": {"consolidated": {
                "observability": {"retention_months": 3},
                "security": {"retention_months": 3},
                "search": {"retention_months": 3},
            }},
            "storage": {"by_deployment": []},
        }
        rows = [
            {"name": "alpha", "bulk_gb_day": 10, "retention_days": 0.042, "store_gb": 5, "snapshot_gb": 40},
            {"name": "beta", "bulk_gb_day": 10, "retention_days": 0, "store_gb": 4, "snapshot_gb": 0},
            {"name": "gamma", "bulk_gb_day": 0, "retention_days": None, "store_gb": 8, "snapshot_gb": 0},
        ]
        out = apply_diagnostics(acc, rows)
        observability = out["sizing"]["consolidated"]["observability"]
        search = out["sizing"]["consolidated"]["search"]
        security = out["sizing"]["consolidated"]["security"]
        self.assertEqual(observability["retention_months"], 1)
        self.assertEqual(observability["stored_gb"], 9.0)
        self.assertEqual(out["sizing"]["diagnostics"]["retention_unmeasured"], [
            {"name": "beta", "bulk_gb_day": 10.0},
        ])
        self.assertEqual(out["sizing"]["diagnostics"]["snapshot_gb"], 40.0)
        self.assertNotIn("retention_months", search)
        self.assertNotIn("retention_months", security)
        self.assertFalse(search["retention_unmeasured"])
        rows[0]["retention_days"] = None
        bare = apply_diagnostics(acc, rows)
        block = bare["sizing"]["consolidated"]["observability"]
        self.assertNotIn("retention_months", block)
        self.assertTrue(block["retention_unmeasured"])


class GateTests(unittest.TestCase):
    def test_g26_accepts_half_a_gigabyte_and_refuses_past_it(self):
        self.assertTrue(g26_gate(10, 10.51)["accepted"])
        self.assertFalse(g26_gate(10, 10.52)["accepted"])
        self.assertEqual(money("($1,234)"), -1234.0)
        self.assertTrue(e73_blocks_quote("($1,234)"))
        self.assertTrue(e73_blocks_quote(None))
        self.assertTrue(e73_blocks_quote(""))
        self.assertTrue(e73_blocks_quote("n/a"))
        self.assertFalse(e73_blocks_quote(0))
        self.assertFalse(e73_blocks_quote("0"))
        self.assertFalse(e73_blocks_quote(0.0))

    def test_overhead_callout_uses_the_active_deployments(self):
        acc = _quote_account(g26=10, e73=0)
        acc["ecu"]["quote_usage_factor"] = 0.5
        acc["ecu"]["quote_lookback_days"] = 30
        acc["ecu"]["total_lookback"] = 24000
        acc["solutions"]["by_deployment"] = [{
            "name": "active", "solution": "observability", "platform": "ech", "ecu": 12000, "gb": 1,
        }]
        acc["sizing"]["diagnostics"] = {"inactive_excluded": 1}
        infra = {"by_deployment": [
            {"name": "active", "tiers": {"master": 1200, "datahot": 1000}},
            {"name": "gone", "tiers": {"master": 1200, "datahot": 1000}},
        ]}
        quoted = build_consolidation(acc, infra)
        overhead = next(row for row in quoted["callouts"] if row["title"].startswith("Fixed overhead"))
        active = quoted["fragmentation"]["fixed_overhead_ecu_mo"]
        year = quoted["ech_composition"]["fixed_overhead_mo"]
        self.assertEqual(overhead["title"], f"Fixed overhead is {active:,.0f} ECU/mo")
        self.assertNotEqual(round(active), round(year))
        self.assertIn(f"{year:,.0f}", overhead["detail"])
        self.assertIn("active deployments", overhead["detail"])
        self.assertIn("1 deployment with no bill", overhead["detail"])

    def test_readback_keeps_written_bulk_and_clears_unit_rates(self):
        quoted = build_consolidation(_quote_account(g26=10.51, e73=0), None)
        observability = quoted["serverless"]["by_solution"]["observability"]
        security = quoted["serverless"]["by_solution"]["security"]
        self.assertIn("quotable", observability["pricing"])
        self.assertEqual(observability["daily_raw_gb"], 10)
        self.assertEqual(observability["sheet_raw_gb_day"], 10.51)
        self.assertIsNone(observability["ingest_mo"])
        self.assertIsNone(observability["retention_mo"])
        self.assertIsNone(quoted["serverless"]["eff_ingest_usd_per_gb"])
        self.assertIsNone(quoted["serverless"]["eff_retain_usd_per_gb_mo"])
        self.assertIn("quotable", security["pricing"])
        self.assertEqual(quoted["search_power"]["profiles"][0]["Total $/month"], 999)
        self.assertEqual(quoted["savings"]["serverless_mo"], 180)
        integrity = quoted["integrity"]
        sheet = next(row for row in integrity["checks"] if row["check"] == "Sheet echo")
        self.assertEqual(sheet["score"], 0.5)
        self.assertTrue(sheet["passed"])
        self.assertEqual(integrity["score"], 0.875)
        self.assertTrue(integrity["passed"])
        page = search_power_section({"consolidation": quoted})
        self.assertIn("Quote integrity 0.875", page)
        self.assertIn(">Pass<", page)
        bare = _quote_account(g26=10, e73=0)
        bare["sizing"]["estimators"]["search"] = {"status": "skipped"}
        bare["storage"]["data_stored_gb"] = 100
        unpriced = build_consolidation(bare, None)
        self.assertIsNone(unpriced["search_power"])
        self.assertEqual(unpriced["savings"]["serverless_mo"], 180)
        self.assertEqual([row["monthly"] for row in quoted["deployment_plan"]], [40, 100])
        skipped = _quote_account(g26=10, e73=0)
        skipped["sizing"]["estimators"]["search"] = {"status": "skipped — no search workload detected"}
        skipped["storage"]["data_stored_gb"] = 100
        skipped["workload"] = {"search": {"peak": {"qps": 50}}}
        skipped["sizing"]["search_power"] = {
            "profiles": [{"Search Power Setting": "on-demand", "Total $/month": 4734}],
            "inputs": {"stored_gb": 100},
        }
        no_search = build_consolidation(skipped, None)
        self.assertIsNone(no_search["search_power"])
        page = search_power_section({"consolidation": {**no_search, "search_power": skipped["sizing"]["search_power"]}, "sizing": skipped["sizing"]})
        self.assertNotIn("Monthly search-power estimate", page)

    def test_g26_miss_and_nonzero_e73_leave_the_solution_unquoted(self):
        missed = build_consolidation(_quote_account(g26=10.52, e73=0), None)
        observability = missed["serverless"]["by_solution"]["observability"]
        self.assertTrue(observability["pricing"].startswith("rejected"))
        self.assertEqual(observability["total_mo"], 0)
        self.assertTrue(any("10.52" in note and "10" in note for note in missed["notes"]))
        blocked = build_consolidation(_quote_account(g26=10, e73="($25)"), None)
        security = blocked["serverless"]["by_solution"]["security"]
        self.assertTrue(security["pricing"].startswith("rejected"))
        self.assertEqual(security["total_mo"], 0)
        for blank in (None, ""):
            blanked = build_consolidation(_quote_account(g26=10, e73=blank), None)
            blank_security = blanked["serverless"]["by_solution"]["security"]
            self.assertTrue(blank_security["pricing"].startswith("rejected"))
            self.assertEqual(blank_security["total_mo"], 0)
        kept = build_consolidation(_quote_account(g26=10, e73=0), None)
        self.assertIn("quotable", kept["serverless"]["by_solution"]["security"]["pricing"])
        self.assertFalse(missed["integrity"]["passed"])
        self.assertFalse(blanked["integrity"]["passed"])
        self.assertTrue(kept["integrity"]["passed"])
        self.assertEqual(kept["integrity"]["score"], 1.0)
        self.assertTrue(any("E73" in note for note in blocked["notes"]))

    def test_integrity_score_fails_a_cent_and_a_missing_bundle(self):
        scored = integrity_score({
            "observability": {"has_workload": True, "sheet_read": True, "written_bulk": 10, "sheet_raw": 10},
            "security": {"has_workload": False},
            "coverage": {"matched": 1, "active": 2, "fallback": 1, "bulk_gb_day": 10, "unmeasured_bulk": 0},
            "solutions": [{"has_workload": True, "direct": True}],
            "headline_monthly": 100,
            "lines": [
                {"monthly": 33.33, "annual_year1": 399.96},
                {"monthly": 33.33, "annual_year1": 399.96},
                {"monthly": 33.33, "annual_year1": 399.96},
            ],
        })
        by_name = {row["check"]: row for row in scored["checks"]}
        self.assertEqual(by_name["Sheet echo"]["score"], 1.0)
        self.assertFalse(by_name["Bundle coverage"]["passed"])
        self.assertEqual(by_name["Bundle coverage"]["score"], 0.5)
        self.assertTrue(by_name["Measured retention"]["passed"])
        self.assertFalse(by_name["Line agreement"]["passed"])
        self.assertTrue(by_name["Year agreement"]["passed"])
        self.assertGreaterEqual(scored["score"], 0.90)
        self.assertTrue(scored["passed"])
        self.assertIn("Bundle coverage", [row["check"] for row in scored["checks"] if row["passed"] is False])
        named = integrity_score({
            "observability": {"has_workload": True, "sheet_read": True, "written_bulk": 10, "sheet_raw": 10},
            "security": {"has_workload": False},
            "coverage": {
                "matched": 1, "active": 2, "fallback": 1, "fallback_names": ["Secretsmanagement-Prod"],
                "bulk_gb_day": 10, "unmeasured_bulk": 0,
            },
            "solutions": [{"has_workload": True, "direct": True}],
            "headline_monthly": 100,
            "lines": [{"monthly": 100, "annual_year1": 1200}],
        })
        coverage = next(row for row in named["checks"] if row["check"] == "Bundle coverage")
        self.assertIn("Secretsmanagement-Prod", coverage["detail"])
        self.assertFalse(coverage["passed"])

    def test_year_agreement_uses_the_steady_annual(self):
        seven = integrity_score({
            "observability": {"has_workload": True, "sheet_read": True, "written_bulk": 10, "sheet_raw": 10},
            "security": {"has_workload": False},
            "solutions": [{"has_workload": True, "direct": True}],
            "headline_monthly": 39269.37,
            "lines": [{
                "monthly": 39269.37,
                "monthly_unrounded": 39269.37474016249,
                "annual_year1": 405419.046673825,
                "annual_steady_state": 471232.49688195,
                "retention_months": 7,
            }],
        })
        thirty = integrity_score({
            "observability": {"has_workload": True, "sheet_read": True, "written_bulk": 10, "sheet_raw": 10},
            "security": {"has_workload": False},
            "solutions": [{"has_workload": True, "direct": True}],
            "headline_monthly": 20053.33,
            "lines": [{
                "monthly": 20053.33,
                "monthly_unrounded": 20053.3266809625,
                "annual_year1": 240639.92017154992,
                "annual_steady_state": 240639.92017154992,
                "retention_months": 1,
            }],
        })
        off = integrity_score({
            "observability": {"has_workload": True, "sheet_read": True, "written_bulk": 10, "sheet_raw": 10},
            "security": {"has_workload": False},
            "solutions": [{"has_workload": True, "direct": True}],
            "headline_monthly": 100,
            "lines": [{
                "monthly": 100,
                "monthly_unrounded": 100,
                "annual_year1": 1200,
                "annual_steady_state": 1201,
                "retention_months": 7,
            }],
        })
        ramp_at_one_month = integrity_score({
            "observability": {"has_workload": True, "sheet_read": True, "written_bulk": 10, "sheet_raw": 10},
            "security": {"has_workload": False},
            "solutions": [{"has_workload": True, "direct": True}],
            "headline_monthly": 100,
            "lines": [{
                "monthly": 100,
                "monthly_unrounded": 100,
                "annual_year1": 1100,
                "annual_steady_state": 1200,
                "retention_months": 1,
            }],
        })

        def year(scored):
            return next(row for row in scored["checks"] if row["check"] == "Year agreement")

        self.assertTrue(year(seven)["passed"])
        self.assertIn("gap $0.00", year(seven)["detail"])
        self.assertTrue(year(thirty)["passed"])
        self.assertIn("Year 1 is $240,639.92", year(thirty)["detail"])
        self.assertFalse(year(off)["passed"])
        self.assertIn("gap $1.00", year(off)["detail"])
        self.assertFalse(year(ramp_at_one_month)["passed"])
        self.assertIn("gap $100.00", year(ramp_at_one_month)["detail"])

    def test_fallback_names_stay_on_the_coverage(self):
        acc = {
            "solutions": {"by_deployment": [
                {"name": "Secretsmanagement-Prod", "platform": "ech", "solution": "observability", "gb": 0},
                {"name": "Has-Bundle", "platform": "ech", "solution": "observability", "gb": 1},
            ], "totals": {}},
            "deployments": {"active": {"month": "2026-11", "names": ["Secretsmanagement-Prod", "Has-Bundle"], "partial": True}},
            "sizing": {},
            "storage": {},
        }
        rows = [{"name": "Has-Bundle", "store_gb": 1, "bulk_gb_day": 2, "retention_days": 30}]
        out = apply_diagnostics(acc, rows)
        coverage = out["sizing"]["diagnostics"]
        self.assertEqual(coverage["k360_fallback"], 1)
        self.assertEqual(coverage["fallback_names"], ["Secretsmanagement-Prod"])
        self.assertTrue(any("Secretsmanagement-Prod" in note for note in out["sizing"]["notes"]))

    def test_addon_clear_uses_the_cell_validation(self):
        yes_no = {"condition": {"type": "ONE_OF_LIST", "values": [
            {"userEnteredValue": "Yes"}, {"userEnteredValue": "No"},
        ]}}
        locked = {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": "Complete"}]}}
        self.assertEqual(addon_off_value(yes_no, "Yes"), "No")
        self.assertIsNone(addon_off_value(locked, "Complete"))
        self.assertEqual(addon_off_value({"condition": {"type": "NUMBER_GREATER"}}, "12"), 0)
        self.assertIsNone(addon_off_value(None, "Enterprise"))
        self.assertEqual(addon_off_value(None, "4"), 0)


class DeploymentCountTests(unittest.TestCase):
    def test_100_percent_is_one_project_per_deployment(self):
        self.assertEqual([project_count(4, pct) for pct in (100, 75, 50, 25, 0)], [4, 3, 2, 1, 1])
        self.assertEqual(project_count(1, 100), 1)
        self.assertEqual(project_count(1, 50), 0)
        rows = [
            {"name": str(i), "bulk_gb_day": i + 1, "store_gb": i + 1, "retention_days": 30}
            for i in range(4)
        ]
        plans = deployment_count_plans(rows)
        self.assertEqual([plan["cut_pct"] for plan in plans], [100, 75, 50, 25, 0])
        by_pct = {plan["cut_pct"]: plan for plan in plans}
        self.assertEqual([by_pct[pct]["projects"] for pct in (100, 75, 50, 25, 0)], [4, 3, 2, 1, 1])
        self.assertEqual(len(by_pct[100]["groups"]), 4)
        self.assertEqual(len(by_pct[0]["groups"][0]), 4)
        single = {plan["cut_pct"]: plan for plan in deployment_count_plans([{"name": "only", "bulk_gb_day": 1, "store_gb": 1}])}
        self.assertEqual(single[100]["projects"], 1)
        self.assertEqual(len(single[100]["groups"]), 1)

    def test_project_floor_stops_the_plan_at_the_region_count(self):
        from diag_inputs import apply_project_floor
        rows = [
            {"name": str(i), "bulk_gb_day": 100 - i, "store_gb": i + 1, "retention_days": 30}
            for i in range(11)
        ]
        plans = {plan["cut_pct"]: plan for plan in deployment_count_plans(rows, floor=8)}
        self.assertEqual([plans[pct]["projects"] for pct in (100, 75, 50, 25, 0)], [11, 8, 8, 8, 8])
        self.assertEqual(apply_project_floor(0, 1, 2), 0)
        self.assertEqual(apply_project_floor(1, 8, 11), 8)

    def test_logical_floor_counts_regions_above_ten_dollars(self):
        from consolidation import logical_project_floor
        groups = []
        for region, gb in (("a", 1000), ("b", 1000), ("c", 1000), ("d", 1000), ("tiny", 0.1)):
            groups.append({"solution": "search", "region": region, "stored_gb": gb, "daily_raw_gb": None, "retention_months": 1})
        for region in ("e", "f", "g", "h"):
            groups.append({"solution": "observability", "region": region, "stored_gb": 1000, "daily_raw_gb": 100, "retention_months": 1})
        groups.append({"solution": "security", "region": "e", "stored_gb": 0, "daily_raw_gb": 0})
        floor = logical_project_floor({"consolidation": {"architecture": {"groups": groups}}, "sizing": {"consolidated": {}}, "workload": {}})
        self.assertEqual(floor, 8)


class EstimateFolderTests(unittest.TestCase):
    def test_estimates_split_by_retention_under_generated_estimates(self):
        self.assertEqual(estimate_subfolder(""), "full_retention")
        self.assertEqual(estimate_subfolder(None), "full_retention")
        self.assertEqual(estimate_subfolder("from_k360"), "full_retention")
        self.assertEqual(estimate_subfolder("_30d"), "30d")
        self.assertEqual(estimate_subfolder("30d"), "30d")
        self.assertEqual(estimate_subfolder("compressioncalc_accounted_for"), "compression_applied/full_retention")
        self.assertEqual(estimate_subfolder("_compressioncalc_accounted_for"), "compression_applied/full_retention")
        self.assertEqual(estimate_subfolder("compressioncalc_accounted_for_30d"), "compression_applied/30d")
        for suffix in ("", None, "_30d", "30d", "from_k360", "compressioncalc_accounted_for", "compressioncalc_accounted_for_30d"):
            self.assertEqual(estimate_folder_name(suffix), "generated_estimates")
        self.assertEqual(PLAN_FOLDER_NAME, "plan_sheets")
        self.assertEqual(COMPRESSION_FOLDER_NAME, "compression_applied")

    def test_search_disk_scales_for_30d_and_compression(self):
        four = {"retention_months": 4}
        one = {"retention_months": 1}
        self.assertAlmostEqual(_search_volume_scale(four, None, ""), 1.0)
        self.assertAlmostEqual(_search_volume_scale(one, 1, "_30d"), 1.0)
        self.assertAlmostEqual(_search_volume_scale(four, 1, "_30d"), 0.25)
        self.assertAlmostEqual(_search_volume_scale(four, None, "compressioncalc_accounted_for"), 0.6)
        self.assertAlmostEqual(_search_volume_scale(four, 1, "compressioncalc_accounted_for_30d"), 0.15)
        self.assertAlmostEqual(_search_volume_scale(one, 1, "compressioncalc_accounted_for_30d"), 0.6)

    def test_estimates_move_into_retention_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "generated_estimates"
            parent.mkdir()
            (parent / "plan_sheets").mkdir()
            bulk = parent / "Acme_search_full retention_bulk_serverless_estimate.csv"
            day = parent / "Acme_search_30d_bulk_serverless_estimate.csv"
            comp = parent / "Acme_search_full retention_compression-applied_serverless_estimate.csv"
            comp_dir = parent / "compression_applied"
            comp_dir.mkdir()
            comp_30 = comp_dir / "Acme_search_30d_compression-applied_serverless_estimate.csv"
            plan = parent / "plan_sheets" / "Acme_Observability_plan_0_serverless_estimate.csv"
            other = parent / "notes.txt"
            for path, text in (
                (bulk, "bulk"), (day, "30"), (comp, "comp"), (comp_30, "comp30"), (plan, "plan"), (other, "note"),
            ):
                path.write_text(text)
            names = relocate_local_estimates(parent)
            self.assertEqual(sorted(names), sorted([bulk.name, day.name, comp.name, comp_30.name]))
            self.assertEqual((parent / "full_retention" / bulk.name).read_text(), "bulk")
            self.assertEqual((parent / "30d" / day.name).read_text(), "30")
            self.assertEqual((parent / "compression_applied" / "full_retention" / comp.name).read_text(), "comp")
            self.assertEqual((parent / "compression_applied" / "30d" / comp_30.name).read_text(), "comp30")
            self.assertEqual(plan.read_text(), "plan")
            self.assertEqual(other.read_text(), "note")
            self.assertEqual(relocate_local_estimates(parent), [])

    def test_estimate_titles_name_solution_retention_and_basis(self):
        key = "GlaxoSmithKline_LLC_2026-11-09"
        self.assertEqual(
            estimate_title(key, "observability", ""),
            f"{key}_observability_full retention_bulk_serverless_estimate",
        )
        self.assertEqual(
            estimate_title(key, "Observability", "_30d"),
            f"{key}_observability_30d_bulk_serverless_estimate",
        )
        self.assertEqual(
            estimate_title(key, "security", "compressioncalc_accounted_for"),
            f"{key}_security_full retention_compression-applied_serverless_estimate",
        )
        self.assertEqual(
            estimate_title(key, "search", "compressioncalc_accounted_for_30d"),
            f"{key}_search_30d_compression-applied_serverless_estimate",
        )
        self.assertEqual(
            estimate_title(key, "observability", "from_k360"),
            f"{key}_observability_full retention_k360_serverless_estimate",
        )
        self.assertEqual(
            estimate_title(key, "observability", "", "eastus2"),
            f"{key}_observability_full retention_bulk_eastus2_serverless_estimate",
        )
        self.assertEqual(
            estimate_title(key, "search", "", "GCP us-central1"),
            f"{key}_search_full retention_bulk_GCP-us-central1_serverless_estimate",
        )

    def test_a_split_solution_has_no_second_fleet_sheet(self):
        acc = {"consolidation": {"architecture": {"groups": [
            {"solution": "search", "region": "us-central1"},
            {"solution": "search", "region": "europe-west1"},
            {"solution": "observability", "region": "asia-east1"},
        ]}}}
        self.assertEqual(architecture_regions(acc, "search"), ["us-central1", "europe-west1"])
        self.assertIsNone(fleet_sheet_region(acc, "search", "us-central1"))
        self.assertEqual(fleet_sheet_region(acc, "observability", "us-central1"), "asia-east1")
        self.assertEqual(fleet_sheet_region({"consolidation": {}}, "security", "eastus2"), "eastus2")
        split = {
            "cloud": {"by_deployment": [
                {"name": "search-a", "region": "us-central1"},
                {"name": "search-b", "region": "europe-west1"},
            ]},
            "solutions": {"by_deployment": [
                {"name": "search-a", "solution": "search"},
                {"name": "search-b", "solution": "search"},
            ]},
        }
        self.assertIsNone(fleet_sheet_region(split, "search", "us-central1"))
        estimators = {
            "search": {"status": "estimated", "readback": {"monthly_steady_state": 887.48}},
            "projects": [
                {"solution": "search", "region": "us-central1", "readback": {"monthly_steady_state": 1501.05}},
                {"solution": "search", "region": "europe-west1", "readback": {"monthly_steady_state": 1798.32}},
            ],
        }
        prefer_regional_estimators(estimators)
        self.assertEqual(estimators["search"]["status"], "sized per region")
        self.assertNotIn("readback", estimators["search"])

    def test_pdfs_split_by_retention(self):
        self.assertEqual(pdf_folder_name("consolidation"), "full_retention")
        self.assertEqual(pdf_folder_name("consolidation_compressioncalc"), "full_retention")
        self.assertEqual(pdf_folder_name("consolidation_k360"), "full_retention")
        self.assertEqual(pdf_folder_name("consolidation_30d"), "30d")
        self.assertEqual(pdf_folder_name("consolidation_compressioncalc_30d"), "30d")
        self.assertIsNone(pdf_folder_name("account"))
        self.assertIsNone(pdf_folder_name("appendix"))
        key = "GlaxoSmithKline_LLC_2026-11-09"
        self.assertEqual(
            consolidation_pdf_stem(key, "consolidation"),
            f"{key}_full retention_bulk_serverless_consolidation_plan",
        )
        self.assertEqual(
            consolidation_pdf_stem(key, "consolidation_30d"),
            f"{key}_30d_bulk_serverless_consolidation_plan",
        )
        self.assertEqual(
            consolidation_pdf_stem(key, "consolidation_compressioncalc"),
            f"{key}_full retention_compression-applied_serverless_consolidation_plan",
        )
        self.assertEqual(
            consolidation_pdf_stem(key, "consolidation_compressioncalc_30d"),
            f"{key}_30d_compression-applied_serverless_consolidation_plan",
        )
        self.assertEqual(
            consolidation_pdf_stem(key, "consolidation_k360"),
            f"{key}_full retention_k360_serverless_consolidation_plan",
        )

    def test_empty_security_skips_as_no_workload(self):
        empty = {"has_workload": False, "retention_months": None, "daily_raw_gb": 0, "stored_gb": 0}
        self.assertEqual(estimator_skip(empty, None), "no_workload")
        self.assertEqual(estimator_skip({"has_workload": True, "daily_raw_gb": 4}, None), "retention")
        self.assertIsNone(estimator_skip({"has_workload": True, "retention_months": 6}, None))

    def test_discount_arrow_only_on_a_reduction(self):
        marked = stat("42.1%", "Estimated reduction versus the contract rate", "success", down=True)
        plain = stat("42.1%", "Estimated reduction versus the contract rate", "success")
        self.assertIn("class='down'", marked)
        self.assertNotIn("class='down'", plain)


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


class CompressionCalcTests(unittest.TestCase):
    def test_accounted_uses_the_daily_rate_ratio(self):
        # Index A writes for 1 day: 100 GB bulk, 20 GB on disk.
        # Index B writes for 2 days: 10 GB bulk, 5 GB on disk, so 5 and 2.5 GB/day.
        # Byte totals would say 110/25 = 4.4. The daily sums say 105/22.5.
        created_a, rolled_a = _write_phase(1, age_days=5)
        created_b, rolled_b = _write_phase(2, age_days=10)
        row = _analyze({
            "diag/nodes_stats.json": {"nodes": {"n1": {"timestamp": COLLECTION_MS}}},
            "diag/commercial/data_stream.json": {"data_streams": [
                {"name": "logs-a", "indices": [{"index_name": ".ds-logs-a-000001"}]},
                {"name": "logs-b", "indices": [{"index_name": ".ds-logs-b-000001"}]},
            ]},
            "diag/commercial/ilm_explain.json": {"indices": {
                ".ds-logs-a-000001": {"index_creation_date_millis": created_a, "lifecycle_date_millis": rolled_a},
                ".ds-logs-b-000001": {"index_creation_date_millis": created_b, "lifecycle_date_millis": rolled_b},
            }},
            "diag/indices_stats.json": {"indices": {
                ".ds-logs-a-000001": {"primaries": {"bulk": {"total_size_in_bytes": 100_000_000_000}, "store": {"size_in_bytes": 20_000_000_000}}},
                ".ds-logs-b-000001": {"primaries": {"bulk": {"total_size_in_bytes": 10_000_000_000}, "store": {"size_in_bytes": 5_000_000_000}}},
            }},
        })
        self.assertEqual(row["bulk_gb_day"], 105.0)
        self.assertEqual(row["compressioncalc_accounted_for"], 63.0)
        self.assertEqual(row["compression_ratio"], 0.6)
        self.assertEqual(row["bulk_gb_day"] * 0.60, row["compressioncalc_accounted_for"])

    def test_system_write_index_stays_in_the_compression_sums(self):
        created, rolled = _write_phase(1, age_days=3)
        row = _analyze({
            "diag/nodes_stats.json": {"nodes": {"n1": {"timestamp": COLLECTION_MS}}},
            "diag/commercial/data_stream.json": {"data_streams": [
                {"name": "logs", "indices": [{"index_name": ".ds-logs-000001"}]},
                {"name": ".monitoring", "indices": [{"index_name": ".ds-.monitoring-000001"}]},
            ]},
            "diag/commercial/ilm_explain.json": {"indices": {
                ".ds-logs-000001": {"index_creation_date_millis": created, "lifecycle_date_millis": rolled},
                ".ds-.monitoring-000001": {"index_creation_date_millis": created, "lifecycle_date_millis": rolled},
            }},
            "diag/indices_stats.json": {"indices": {
                ".ds-logs-000001": {"primaries": {"bulk": {"total_size_in_bytes": 100_000_000_000}, "store": {"size_in_bytes": 20_000_000_000}}},
                ".ds-.monitoring-000001": {"primaries": {"bulk": {"total_size_in_bytes": 50_000_000_000}, "store": {"size_in_bytes": 10_000_000_000}}},
            }},
        })
        self.assertEqual(row["store_gb"], 20.0)
        self.assertEqual(row["bulk_gb_day"], 150.0)
        self.assertEqual(row["compressioncalc_accounted_for"], 90.0)
        self.assertEqual(row["compression_ratio"], 0.6)

    def test_on_disk_above_bulk_still_keeps_sixty_percent(self):
        created, rolled = _write_phase(1, age_days=3)
        row = _analyze({
            "diag/nodes_stats.json": {"nodes": {"n1": {"timestamp": COLLECTION_MS}}},
            "diag/commercial/data_stream.json": {"data_streams": [
                {"name": "logs-a", "indices": [{"index_name": ".ds-logs-a-000001"}]},
                {"name": "logs-b", "indices": [{"index_name": ".ds-logs-b-000001"}]},
            ]},
            "diag/commercial/ilm_explain.json": {"indices": {
                ".ds-logs-a-000001": {"index_creation_date_millis": created, "lifecycle_date_millis": rolled},
                ".ds-logs-b-000001": {"index_creation_date_millis": created, "lifecycle_date_millis": rolled},
            }},
            "diag/indices_stats.json": {"indices": {
                ".ds-logs-a-000001": {"primaries": {"bulk": {"total_size_in_bytes": 10_000_000_000}, "store": {"size_in_bytes": 40_000_000_000}}},
                ".ds-logs-b-000001": {"primaries": {"bulk": {"total_size_in_bytes": 10_000_000_000}, "store": {"size_in_bytes": 2_000_000_000}}},
            }},
        })
        self.assertEqual(row["bulk_gb_day"], 20.0)
        self.assertEqual(row["compression_ratio"], 0.6)
        self.assertEqual(row["compressioncalc_accounted_for"], 12.0)

    def test_compression_account_swaps_only_observability_and_security(self):
        acc = {
            "deployments": {"active": {"month": "2026-10", "names": ["alpha", "beta", "gamma", "delta"], "partial": False}},
            "solutions": {"by_deployment": [
                {"name": "alpha", "platform": "ech", "solution": "observability", "gb": 100, "ecu": 1},
                {"name": "beta", "platform": "ech", "solution": "observability", "gb": 40, "ecu": 1},
                {"name": "gamma", "platform": "ech", "solution": "search", "gb": 50, "ecu": 1},
                {"name": "delta", "platform": "ech", "solution": "security", "gb": 10, "ecu": 1},
            ], "totals": {"observability": {}, "security": {}, "search": {}}},
            "sizing": {"consolidated": {"observability": {}, "security": {}, "search": {}}},
            "storage": {"by_deployment": []},
        }
        rows = [
            {"name": "alpha", "bulk_gb_day": 100, "compressioncalc_accounted_for": 20, "compression_ratio": 0.2, "retention_days": 30.416, "store_gb": 5, "snapshot_gb": 0},
            {"name": "beta", "bulk_gb_day": 10, "compressioncalc_accounted_for": 5, "compression_ratio": 0.5, "retention_days": 60.832, "store_gb": 4, "snapshot_gb": 0},
            {"name": "gamma", "bulk_gb_day": 8, "compressioncalc_accounted_for": 2, "compression_ratio": 0.25, "retention_days": 30.416, "store_gb": 8, "snapshot_gb": 0},
            {"name": "delta", "bulk_gb_day": 10, "compressioncalc_accounted_for": 0, "compression_ratio": None, "retention_days": 30.416, "store_gb": 1, "snapshot_gb": 0},
        ]
        quoted = apply_diagnostics(acc, rows)
        observability = quoted["sizing"]["consolidated"]["observability"]
        self.assertEqual(observability["daily_raw_gb"], 110.0)
        self.assertEqual(observability["logs_gb_day"], 99.0)
        self.assertEqual(observability["metrics_gb_day"], 11.0)
        self.assertEqual(observability["compressioncalc_accounted_for"], 66.0)
        self.assertEqual(observability["compression_ratio"], 0.6)
        self.assertEqual(quoted["sizing"]["consolidated"]["search"]["daily_raw_gb"], 8.0)
        quoted["sizing"]["estimators"] = {
            "search": {"kind": "search", "readback": {"monthly_steady_state": 12}},
            "observability": {"kind": "observability", "readback": {"monthly_steady_state": 99}},
            "projects": [
                {"solution": "search", "region": "aws-us-east-1"},
                {"solution": "observability", "region": "aws-us-east-1"},
            ],
        }
        priced = compressioncalc_account(quoted)
        self.assertEqual(quoted["sizing"]["consolidated"]["observability"]["daily_raw_gb"], 110.0)
        swapped = priced["sizing"]["consolidated"]["observability"]
        security = priced["sizing"]["consolidated"]["security"]
        search = priced["sizing"]["consolidated"]["search"]
        self.assertEqual(priced["sizing"]["ingest_basis"], "compressioncalc_accounted_for")
        self.assertEqual(swapped["daily_raw_gb"], 66.0)
        self.assertEqual(swapped["logs_gb_day"], 59.4)
        self.assertEqual(swapped["metrics_gb_day"], 6.6)
        self.assertEqual(swapped["bulk_gb_day"], 110.0)
        self.assertEqual(swapped["compression_ratio"], 0.6)
        self.assertEqual(swapped["retention_months"], 2)
        self.assertEqual(security["daily_raw_gb"], 6.0)
        self.assertEqual(security["bulk_gb_day"], 10.0)
        self.assertEqual(security["compression_ratio"], 0.6)
        self.assertEqual(security["retention_months"], 1)
        self.assertEqual(search["daily_raw_gb"], 8.0)
        self.assertEqual(priced["sizing"]["estimators"]["search"]["readback"]["monthly_steady_state"], 12)
        self.assertNotIn("observability", priced["sizing"]["estimators"])
        self.assertEqual([p["solution"] for p in priced["sizing"]["estimators"]["projects"]], ["search"])
        self.assertNotIn("consolidation", priced)
        self.assertTrue(priced["sizing"]["notes"][0].startswith("compressioncalc_accounted_for"))
        bulk, ratio, accounted = _compression_totals(priced)
        self.assertEqual(bulk, 128.0)
        self.assertEqual(accounted, 76.8)
        self.assertEqual(ratio, 0.6)


class RegionalQuoteTests(unittest.TestCase):
    def test_projection_skips_a_region_at_or_under_ten_dollars(self):
        from consolidation import REGION_ESTIMATE_MIN_USD, projected_region_monthly

        tiny = projected_region_monthly(
            "observability", stored_gb=1, daily_raw_gb=0.01, retention_months=1,
            solution_stored_gb=1, workload=None,
        )
        large = projected_region_monthly(
            "observability", stored_gb=1000, daily_raw_gb=50, retention_months=1,
            solution_stored_gb=1000, workload=None,
        )
        self.assertLessEqual(tiny, REGION_ESTIMATE_MIN_USD)
        self.assertGreater(large, REGION_ESTIMATE_MIN_USD)
        search = projected_region_monthly(
            "search", stored_gb=1, daily_raw_gb=None, retention_months=1,
            solution_stored_gb=1001, workload={},
        )
        kept = projected_region_monthly(
            "search", stored_gb=1000, daily_raw_gb=None, retention_months=1,
            solution_stored_gb=1001, workload={},
        )
        self.assertLessEqual(search, REGION_ESTIMATE_MIN_USD)
        self.assertGreater(kept, REGION_ESTIMATE_MIN_USD)
    def test_one_own_readback_keeps_the_consolidated_total(self):
        quote = regional_quote(
            [
                {"region": "east", "stored_gb": 10, "kind": "own", "own_monthly": 180, "own_annual": 2000, "own_pricing": "sheet read-back"},
                {"region": "west", "stored_gb": 10, "kind": "missing"},
            ],
            300,
            solution="observability",
            consolidated_annual=3600,
        )
        self.assertEqual(quote["mode"], "store_split")
        self.assertEqual(quote["total_mo"], 300)
        self.assertEqual(quote["total_yr"], 3600)
        self.assertEqual([row["monthly"] for row in quote["rows"]], [150.0, 150.0])
        self.assertEqual([row["annual_year1"] for row in quote["rows"]], [1800.0, 1800.0])

    def test_every_region_sums_its_own_readback(self):
        quote = regional_quote(
            [
                {"region": "east", "stored_gb": 10, "kind": "own", "own_monthly": 180, "own_annual": 2160, "own_pricing": "re-estimated on-demand"},
                {"region": "west", "stored_gb": 10, "kind": "own", "own_monthly": 90, "own_annual": 1080, "own_pricing": "re-estimated on-demand"},
            ],
            300,
            solution="search",
            consolidated_annual=3600,
        )
        self.assertEqual(quote["mode"], "per_region")
        self.assertEqual(quote["total_mo"], 270.0)
        self.assertEqual([row["monthly"] for row in quote["rows"]], [180, 90])

    def test_a_rejected_region_zeros_that_solution(self):
        quote = regional_quote(
            [
                {"region": "east", "stored_gb": 10, "kind": "own", "own_monthly": 180},
                {"region": "west", "stored_gb": 10, "kind": "rejected", "own_pricing": "rejected — G26 differs from the written bulk"},
            ],
            300,
            solution="observability",
        )
        self.assertEqual(quote["mode"], "rejected")
        self.assertEqual(quote["total_mo"], 0.0)
        self.assertTrue(all(row["monthly"] is None for row in quote["rows"]))


class EfficiencyTests(unittest.TestCase):
    def test_plan_bars_keep_cut_order_and_a_gate_miss_clears_them(self):
        specs = [
            {
                "cut_pct": 0,
                "label": "0%",
                "projects": 2,
                "groups": [
                    {"sheet": 1, "names": ["b"], "bulk_gb_day": 1, "months_written": 1},
                    {"sheet": 0, "names": ["a"], "bulk_gb_day": 10, "months_written": 3},
                ],
            },
            {"cut_pct": 25, "label": "25%", "projects": 1, "groups": [
                {"sheet": 2, "names": ["a", "b"], "bulk_gb_day": 11, "months_written": 3},
            ]},
            {"cut_pct": 100, "label": "100%", "projects": 1, "reused_headline": True, "groups": []},
        ]
        fills = [
            {"monthly": 20, "url": "http://a"},
            {"monthly": 5, "url": "http://b"},
            {"monthly": 22, "url": "http://ab"},
        ]
        built = assemble_plan_bars(specs, fills)
        self.assertEqual([row["label"] for row in built["bars"]], ["0%", "25%", "100%"])
        self.assertEqual(built["bars"][0]["groups"][0]["names"], ["b"])
        self.assertEqual(built["bars"][0]["groups"][0]["monthly"], 5)
        self.assertEqual(built["bars"][0]["monthly"], 25)
        self.assertEqual(built["bars"][1]["monthly"], 22)
        self.assertTrue(built["bars"][2]["reused_headline"])
        missed = assemble_plan_bars(specs, [fills[0], {"error": "G26 is 11 GB/day against 10 written."}, fills[2]])
        self.assertEqual(missed["bars"], [])
        self.assertIn("G26", missed["error"])

    def test_sheet_pool_keeps_input_order(self):
        os.environ["SHEET_WORKERS"] = "4"

        def slow(item):
            if item == 0:
                time.sleep(0.05)
            return item

        self.assertEqual(map_in_order(slow, [0, 1, 2]), [0, 1, 2])

    def test_consolidation_brief_is_the_quote_slice(self):
        acc = _quote_account(g26=10, e73=0)
        acc["analysis"] = {"burndown": {"level": "ok", "series": {"months": []}}}
        acc["platform"] = {"label": "ECH"}
        acc["sizing"]["diagnostics"] = {"inactive_excluded": 2, "snapshot_gb": 0, "k360_fallback": 1}
        acc["sizing"]["estimators"]["observability"]["g26_gate"] = {
            "accepted": True, "written_bulk": 10, "sheet_raw_gb_day": 10.1,
        }
        acc["sizing"]["estimators"]["observability"]["cells"] = {"G11": 1, "G21": 9, "G23": 1}
        acc["compressioncalc_accounted_for"] = {"gb_day": 6, "compression_ratio": 0.6, "bulk_gb_day": 10, "monthly": 40}
        cons = build_consolidation(acc, None)
        cons["deployment_plan"] = [
            {"label": "0%", "monthly": 40, "projects": 4, "groups": [{"url": "http://sheet", "names": ["hidden"]}]},
            {"label": "100%", "monthly": 100, "projects": 1},
        ]
        cons["thirty_day"] = {"monthly": 50, "annual_year1": 600}
        cons["metered_rates"] = [{"name": "alpha", "raw_gb_day": 1, "metered_gb_day": 1.595, "usd_per_gb": 0.5}]
        cons["search_power"] = {"profiles": [{"Search Power Setting": "on-demand", "Total $/month": 12}]}
        acc["consolidation"] = cons
        brief = consolidation_brief(acc)
        self.assertNotIn("burndown", brief)
        self.assertNotIn("platform", brief)
        self.assertEqual(brief["ingest_source"], "diagnostics")
        self.assertEqual(brief["deployment_plan"], [
            {"label": "0%", "monthly": 40, "projects": 4},
            {"label": "100%", "monthly": 100, "projects": 1},
        ])
        self.assertEqual(brief["thirty_day"]["monthly"], 50)
        self.assertTrue(brief["estimators"]["observability"]["g26_gate"]["accepted"])
        self.assertEqual(brief["estimators"]["observability"]["cells"]["G21"], 9)
        self.assertNotIn("url", brief["deployment_plan"][0])
        self.assertTrue(brief["metered_rates"][0]["at_half_dollar"])
        self.assertEqual(brief["search_power"]["profiles"][0]["monthly"], 12)
        self.assertIn("score", brief["integrity"])
        self.assertIn("passed", brief["integrity"])
        self.assertIn("failed", brief["integrity"])
        self.assertEqual(brief["compressioncalc_accounted_for"]["gb_day"], 6)
        self.assertEqual(brief["diagnostics_coverage"]["inactive_excluded"], 2)

    def test_quote_pdf_jobs_batch_the_finished_scenarios(self):
        account = {"meta": {"run_key": "a"}, "consolidation": {"source": "k360"}}
        diag = {"meta": {"run_key": "a"}, "consolidation": {"source": "diagnostics"}}
        ok = {"meta": {"run_key": "a"}, "consolidation": {"source": "diagnostics"}}
        bad = {"meta": {"run_key": "a"}, "consolidation": {"error": "sheet failed"}}
        jobs = quote_pdf_jobs(account, diag, [
            (ok, "consolidation_30d"),
            (bad, "consolidation_compressioncalc"),
            (ok, "consolidation_compressioncalc_30d"),
            (ok, "consolidation_k360"),
        ])
        self.assertEqual(
            [kind for _doc, kind in jobs],
            ["account", "consolidation", "consolidation_30d", "consolidation_compressioncalc_30d", "consolidation_k360", "appendix"],
        )

    def test_xlsx_export_is_off_unless_asked(self):
        class Args:
            xlsx = False
            always_copy = False

        self.assertEqual(_estimator_flags(Args()), [])
        Args.xlsx = True
        self.assertEqual(_estimator_flags(Args()), ["--xlsx"])

    def test_two_zip_parse_matches_the_serial_rows(self):
        captured = datetime(2026, 11, 1, tzinfo=timezone.utc)

        def write_zip(path: Path, nbytes: int) -> None:
            body = {"indices": {"logs-000001": {"primaries": {"store": {"size_in_bytes": nbytes}}}}}
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("diag/indices_stats.json", json.dumps(body))

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_zip(folder / "small__aaa.zip", 2_000_000_000)
            write_zip(folder / "large__bbb.zip", 5_000_000_000)
            (folder / "broken__ccc.zip").write_text("not a zip")
            when = captured.isoformat()
            names = {}
            payloads = [(str(path), when, names) for path in sorted(folder.glob("*.zip"))]
            serial = []
            for payload in payloads:
                item = _parse_zip(payload)
                if not item.get("warn"):
                    serial.append(item)
            serial.sort(key=lambda row: -(row.get("bulk_gb_day") or 0))
            pooled = parse_diagnostics(folder, captured=captured, workers=2)
        self.assertEqual([(row["id"], row["store_gb"]) for row in pooled], [(row["id"], row["store_gb"]) for row in serial])
        self.assertEqual({row["id"] for row in pooled}, {"aaa", "bbb"})
        self.assertNotIn("ccc", {row["id"] for row in pooled})


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


class PrivacyTests(unittest.TestCase):
    def test_admin_host_is_pinned_https(self):
        self.assertEqual(admin_base({}), "https://admin.found.no")
        self.assertEqual(admin_base({"FOUND_ADMIN_URL": "https://admin.found.no/"}), "https://admin.found.no")
        self.assertEqual(admin_base({"FOUND_ADMIN_URL": "https://admin.found.no:9243"}), "https://admin.found.no:9243")
        allowed = admin_base({
            "FOUND_ADMIN_URL": "https://other.example",
            "FOUND_ADMIN_HOST_ALLOWLIST": "other.example, admin.found.no",
        })
        self.assertEqual(allowed, "https://other.example")
        for raw in (
            "http://admin.found.no",
            "https://user:pw@admin.found.no",
            "https://admin.found.no/api",
            "https://admin.found.no?x=1",
            "https://admin.found.no#frag",
            "https://evil.example",
        ):
            with self.assertRaises(ValueError):
                admin_base({"FOUND_ADMIN_URL": raw})

    def test_bad_org_refuses_before_the_key_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out"
            with self.assertRaises(ValueError):
                pull_diagnostics({"FOUND_ADMIN_API_KEY": "secret", "FOUND_ADMIN_URL": "http://admin.found.no"}, "abc", dest)
            self.assertFalse(dest.exists())
            with self.assertRaises(ValueError):
                pull_diagnostics({"FOUND_ADMIN_API_KEY": "secret"}, "bad/id", dest)
            self.assertFalse(dest.exists())

    def test_capture_ids_reject_slash_and_question_mark(self):
        with self.assertRaises(ValueError):
            capture_url("https://admin.found.no", "abc/def", "main-elasticsearch")
        with self.assertRaises(ValueError):
            capture_url("https://admin.found.no", "abc", "main?x=1")
        with self.assertRaises(ValueError):
            safe_id("123/456", "organization id")
        url = capture_url("https://admin.found.no", "abc_DEF-1", "main-elasticsearch")
        self.assertIn("/deployments/abc_DEF-1/elasticsearch/main-elasticsearch/diagnostics/_capture?mode=full", url)
        self.assertEqual(capture_target({"id": "abc/def", "ref_id": "main", "name": "one"})["result"], "skipped")
        self.assertEqual(capture_target({"id": "abc", "ref_id": "main?x", "name": "two"})["result"], "skipped")
        self.assertNotIn("result", capture_target({"id": "abc", "ref_id": "main-elasticsearch", "name": "three"}))

    def test_kibana_url_must_be_https(self):
        base, key = es_base_and_key({"KIBANA_URL": "https://x.kb.example/", "ELASTIC_API_KEY": "k"})
        self.assertEqual(base, "https://x.es.example")
        self.assertEqual(key, "k")
        with self.assertRaises(SystemExit):
            es_base_and_key({"KIBANA_URL": "http://x.kb.example", "ELASTIC_API_KEY": "k"})
        with self.assertRaises(SystemExit):
            es_base_and_key({"KIBANA_URL": "https://user:pw@x.example", "ELASTIC_API_KEY": "k"})

    def test_formula_text_is_rejected_and_numbers_are_raw(self):
        self.assertEqual(cell_input_mode(12.5), "RAW")
        self.assertEqual(cell_input_mode(False), "RAW")
        self.assertEqual(cell_input_mode("Complete"), "USER_ENTERED")
        self.assertEqual(cell_input_mode("0%"), "USER_ENTERED")
        for bad in ("=1+1", "+1", "-1", "@cmd", " AWS: =1"):
            with self.assertRaises(ValueError):
                cell_input_mode(bad, allowed_text={"AWS: us-east-1"})
        raw, entered = partition_cells({"G21": 12.5, "G4": "Complete", "G8": None})
        self.assertEqual(raw, [{"range": "G21", "values": [[12.5]]}])
        self.assertEqual(entered, [{"range": "G4", "values": [["Complete"]]}])
        sheets = _SheetCall()
        written = push_cells(sheets, "copy-id", "Observability Estimator", {"G21": 12.5, "G4": "Complete", "G22": "0%"})
        self.assertEqual(written, 3)
        self.assertEqual([body["valueInputOption"] for body in sheets.bodies], ["RAW", "USER_ENTERED"])
        self.assertEqual(sheets.bodies[0]["data"][0]["values"], [[12.5]])
        with self.assertRaises(ValueError):
            push_cells(sheets, "copy-id", "Observability Estimator", {"G8": "=1+1"}, allowed_text={"AWS: us-east-1"})

    def test_golden_template_is_refused(self):
        with self.assertRaises(RuntimeError):
            assert_not_template(DEFAULT_O11Y)
        with self.assertRaises(RuntimeError):
            assert_not_template(
                "https://docs.google.com/spreadsheets/d/1b91Uoa2e4vBrep_7I4RoY_8Mr19poqaUgWl7LqhIOqY/edit"
            )
        self.assertEqual(require_customer_parent("folder-id"), "folder-id")
        with self.assertRaises(RuntimeError):
            require_customer_parent("")
        with self.assertRaises(RuntimeError):
            require_customer_parent(DEFAULT_SECURITY)
        with self.assertRaises(RuntimeError):
            push_cells(object(), DEFAULT_O11Y, "Tab", {"G21": 1})
        drive = _SheetCall({"id": DEFAULT_O11Y, "name": "golden"})
        with self.assertRaises(RuntimeError):
            copy_template(drive, DEFAULT_O11Y, "title", "folder-id", reuse=False)
        listed = _SheetCall({"files": [{"id": DEFAULT_SECURITY, "name": "golden"}]})
        with self.assertRaises(RuntimeError):
            copy_template(listed, DEFAULT_SECURITY, "title", "folder-id", reuse=True)

    def test_tunnel_check_does_not_send_a_key(self):
        ok, detail = tunnel_reaches("127.0.0.1", port=1, timeout=0.5)
        self.assertFalse(ok)
        self.assertIn("Aviatrix", detail)
        self.assertNotIn("ApiKey", detail)
        self.assertNotIn("FOUND_ADMIN", detail)

    def test_customer_files_are_private_and_gitignore_is_warned(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "run"
            seal_dir(folder)
            path = folder / "brief.json"
            seal_text(path, "{}")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            notes = unignored_secret_warnings(root)
            self.assertTrue(any(note.startswith(".env ") for note in notes))
            self.assertTrue(any(note.startswith("artifacts ") for note in notes))
            (root / ".gitignore").write_text(".env\nartifacts/\n")
            self.assertEqual(unignored_secret_warnings(root), [])
            (root / ".env").write_text("K=1\n")
            subprocess.run(["git", "add", "-f", ".env"], cwd=root, check=True, capture_output=True)
            tracked = unignored_secret_warnings(root)
            self.assertTrue(any(note.startswith(".env ") for note in tracked))


class ConsolidationLayoutTests(unittest.TestCase):
    def test_body_flows_and_the_appendix_is_its_own_document(self):
        acc = _quote_account(g26=10, e73=0)
        acc["consolidation"] = build_consolidation(acc, None)
        html = render_consolidation_html(acc)
        appendix = render_appendix_html(acc)
        self.assertEqual(html.count("<h2>Appendix A</h2>"), 0)
        self.assertEqual(html.count("<h2>Appendix B</h2>"), 0)
        self.assertIn("Reconciliation and what to confirm", html)
        self.assertEqual(appendix.count("<h2>Appendix A</h2>"), 1)
        self.assertEqual(appendix.count("<h2>Appendix B</h2>"), 1)
        self.assertGreater(appendix.rfind("<h2>Appendix B</h2>"), appendix.rfind("<h2>Appendix A</h2>"))
        self.assertNotIn("<h2>1. ", html)
        self.assertNotIn("<h2>6. ", html)
        self.assertNotIn("<h2>9. ", html)
        self.assertNotIn("<div class='page-break'>", html)
        self.assertEqual(html.count("sec newpage"), 0)
        self.assertIn("<section class='sec'><h2>Appendix A</h2>", appendix)
        self.assertIn("<section class='sec newpage'><h2>Appendix B</h2>", appendix)
        self.assertIn("class='tbl'", html)
        self.assertIn("class='consolidation'", html)
        self.assertLess(appendix.find("Data used, and how the headline is calculated"), appendix.find("Quote integrity"))

    def test_appendix_b_prints_formulas_and_skips_an_unpriced_solution(self):
        acc = _quote_account(g26=10, e73=0)
        acc["workload"] = {
            "window_days": 21,
            "indices": 40,
            "search": {
                "peak": {"qps": 12.5, "pct_hours_mo": 25.0},
                "nonpeak": {"qps": 4.0, "pct_hours_mo": 60.0},
                "idle_pct_hours_mo": 15.0,
            },
            "ingest": {"docs_per_sec": 8.0, "active_hours_mo": 500},
        }
        acc["sizing"]["consolidated"]["security"] = {"stored_gb": 0, "daily_raw_gb": 0, "deployments": 0}
        acc["sizing"]["estimators"]["security"] = {"status": "skipped — no security workload detected"}
        acc["sizing"]["consolidated"]["search"] = {
            "stored_gb": 400, "daily_raw_gb": 0, "retention_months": 4,
            "retention_months_measured": 3.7, "deployments": 1, "has_workload": True,
        }
        acc["sizing"]["estimators"]["search"]["status"] = "estimated"
        acc["sizing"]["estimators"]["search"]["store_scale"] = 0.6
        acc["sizing"]["ingest_basis"] = "compressioncalc_accounted_for"
        acc["consolidation"] = build_consolidation(acc, None)
        acc["consolidation"]["ingest_basis"] = "compressioncalc_accounted_for"
        acc["consolidation"]["compression_ratio"] = 0.6
        html = render_appendix_html(acc)
        appendix_b = html.split("<h2>Appendix B</h2>", 1)[1]
        self.assertIn("Σ Δsearch_total ÷ 86,400", appendix_b)
        self.assertIn("percent ÷ 100 × 730", appendix_b)
        self.assertIn("× 0.60", appendix_b)
        self.assertIn("primaries.bulk.total_size_in_bytes", appendix_b)
        self.assertIn("primaries.store.size_in_bytes and is not this figure", appendix_b)
        self.assertIn("not esdiag bulk.compression_ratio", appendix_b)
        self.assertIn("12.5 QPS", appendix_b)
        self.assertNotIn("Security bulk", appendix_b)
        self.assertNotIn("Security monthly", appendix_b)

    def test_default_lookback_writes_the_30_day_pace_into_the_estimate(self):
        acc = _quote_account(g26=10, e73=0)
        acc["ecu"]["last_30d_daily_avg"] = 0.2
        acc["ecu"]["daily_totals"] = [1.0] * 335 + [0.2] * 30
        factor = apply_quote_lookback(acc, 30)
        expected = 0.2 / (10 / 30.416)
        self.assertAlmostEqual(factor, expected, places=4)
        self.assertAlmostEqual(
            acc["sizing"]["consolidated"]["observability"]["daily_raw_gb"],
            round(10 * expected, 3),
            places=3,
        )
        again = apply_quote_lookback(acc, 30)
        self.assertAlmostEqual(again, factor, places=4)
        self.assertAlmostEqual(
            acc["sizing"]["consolidated"]["observability"]["daily_raw_gb"],
            round(10 * expected, 3),
            places=3,
        )
        with self.assertRaises(ValueError):
            apply_quote_lookback(acc, 366)
        acc["consolidation"] = build_consolidation(acc, None)
        self.assertAlmostEqual(acc["consolidation"]["basis"]["ech_monthly_ecu"], round(0.2 * 30.416, 2))
        html = render_consolidation_html(acc)
        header = html.split("</header>", 1)[0]
        self.assertIn("30-day lookback", header)
        self.assertNotIn("complete-month", header)
        self.assertIn("30-day lookback", html)
        self.assertIn("Complete-month average", html)
        self.assertIn("Last 30 days of billed usage", html)
        self.assertIn("ECH, last 30 billed days", html)
        self.assertIn("Serverless, last 30 billed days", html)
        build = html.split("What you pay for today", 1)[1].split("<h2>", 1)[0]
        self.assertIn("Full lookback", build)
        self.assertIn("30-day lookback", build)
        self.assertIn("30-day usage", build)
        self.assertNotIn("ECH today", build)

    def test_contract_rate_is_drawn_beside_the_list_price(self):
        acc = _quote_account(g26=10, e73=0)
        acc["meta"]["org"]["ARR"] = 1825
        acc["ecu"]["contract"] = {"commit_pace_ecu_per_day": 10}
        acc["consolidation"] = build_consolidation(acc, None)
        html = render_consolidation_html(acc)
        cover = html.split("What you pay for today", 1)[0]
        self.assertIn("Serverless at list", cover)
        self.assertIn("Serverless at contract rate", cover)
        self.assertIn("ECH at contract rate", cover)
        self.assertIn("Contract rate $0.5 per ECU", cover)
        self.assertIn("minus serverless at the contract rate", cover)
        self.assertNotIn("The contract rate is not applied to serverless.", cover)
        self.assertNotIn("minus serverless at list", cover)

    def test_cover_stays_on_the_complete_month_average_without_a_30_day_rate(self):
        acc = _quote_account(g26=10, e73=0)
        acc["consolidation"] = build_consolidation(acc, None)
        html = render_consolidation_html(acc)
        header = html.split("</header>", 1)[0]
        self.assertIn("complete-month avg", header)
        self.assertIn("ECH cost today vs. the serverless estimate", html)
        self.assertNotIn("last 30 billed days", html)
        build = html.split("What you pay for today", 1)[1].split("<h2>", 1)[0]
        self.assertIn("Full lookback", build)
        self.assertNotIn("30-day lookback", build)
        self.assertNotIn("ECH today", build)


class ChromeFallbackTests(unittest.TestCase):
    def test_missing_chrome_keeps_the_html_report(self):
        from unittest import mock

        import render_pdf

        account = {"meta": {"run_key": "Acme_2026-11-09", "customer": "Acme"}}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(render_pdf, "render_account_html", return_value="<html></html>"):
                with mock.patch.object(render_pdf, "find_chrome", return_value=None):
                    out = render_pdf.export_documents([(account, "account")], Path(tmp))
            entry = out["account"]
            self.assertTrue(entry["html"].endswith("Acme_2026-11-09_Account_analysis.html"))
            self.assertTrue(Path(entry["html"]).is_file())
            self.assertNotIn("pdf", entry)
            self.assertIn("HTML file is the report", entry["error"])


class SkillPathTests(unittest.TestCase):
    def test_sibling_org_skill_is_found_from_this_repo(self):
        found = find_skill("k360-org-report", HERE)
        self.assertTrue((found / "scripts" / "fetch_org.py").is_file())

    def test_artifacts_land_beside_the_skills(self):
        from skill_paths import artifacts_root

        saved = {
            name: os.environ.pop(name, None)
            for name in ("SERVERLESS_ANALYZER_ARTIFACTS", "CLOUD_TRACKER_ARTIFACTS_ROOT")
        }
        try:
            root = artifacts_root()
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value
        self.assertEqual(root.name, "diag_artifacts")
        self.assertTrue((root.parent / "k360-org-report" / "SKILL.md").is_file())

    def test_env_override_wins_over_the_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["K360_ORG_SKILL_DIR"] = tmp
            try:
                self.assertEqual(find_skill("k360-org-report", HERE), Path(tmp))
            finally:
                os.environ.pop("K360_ORG_SKILL_DIR", None)


if __name__ == "__main__":
    unittest.main()
