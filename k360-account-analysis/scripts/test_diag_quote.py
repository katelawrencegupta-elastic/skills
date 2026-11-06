#!/usr/bin/env python3
"""Offline checks for the diagnostic quote. No Google and no sheets."""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "diag-serverless-consolidation" / "scripts"))

from admin_diags import _user_index, analyze_bundle, quoted_index_bytes  # noqa: E402
from consolidation import build_consolidation  # noqa: E402
from diag_account import apply_diagnostics, compressioncalc_account  # noqa: E402
from diag_inputs import deployment_count_plans, project_count, summarize  # noqa: E402
from quote_checks import addon_off_value, e73_blocks_quote, g26_gate, money  # noqa: E402
from run_account_analysis import _compression_totals  # noqa: E402
from render_pdf import pdf_folder_name, stat  # noqa: E402
from size_serverless import PLAN_FOLDER_NAME, estimate_folder_name, estimator_skip  # noqa: E402


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
        self.assertFalse(e73_blocks_quote(0))

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
        bare = _quote_account(g26=10, e73=0)
        bare["sizing"]["estimators"]["search"] = {"status": "skipped"}
        bare["storage"]["data_stored_gb"] = 100
        unpriced = build_consolidation(bare, None)
        self.assertIsNone(unpriced["search_power"])
        self.assertEqual(unpriced["savings"]["serverless_mo"], 180)
        self.assertEqual([row["monthly"] for row in quoted["deployment_plan"]], [40, 100])

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
        self.assertTrue(any("E73" in note for note in blocked["notes"]))

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
    def test_project_counts_keep_one_project_at_100_percent(self):
        self.assertEqual([project_count(4, pct) for pct in (0, 25, 50, 75, 100)], [4, 3, 2, 1, 1])
        self.assertEqual(project_count(1, 100), 1)
        self.assertEqual(project_count(1, 50), 0)
        rows = [
            {"name": str(i), "bulk_gb_day": i + 1, "store_gb": i + 1, "retention_days": 30}
            for i in range(4)
        ]
        plans = {plan["cut_pct"]: plan for plan in deployment_count_plans(rows)}
        self.assertEqual([plans[pct]["projects"] for pct in (0, 25, 50, 75, 100)], [4, 3, 2, 1, 1])
        self.assertEqual(len(plans[100]["groups"][0]), 4)
        single = deployment_count_plans([{"name": "only", "bulk_gb_day": 1, "store_gb": 1}])
        self.assertEqual(single[-1]["cut_pct"], 100)
        self.assertEqual(single[-1]["projects"], 1)
        self.assertEqual(len(single[-1]["groups"]), 1)


class EstimateFolderTests(unittest.TestCase):
    def test_estimates_and_plans_share_generated_estimates(self):
        for suffix in (
            "", None, "_30d", "30d", "from_k360",
            "compressioncalc_accounted_for", "_compressioncalc_accounted_for",
            "compressioncalc_accounted_for_30d",
        ):
            self.assertEqual(estimate_folder_name(suffix), "generated_estimates")
        self.assertEqual(PLAN_FOLDER_NAME, "plan_sheets")

    def test_pdfs_split_by_retention(self):
        self.assertEqual(pdf_folder_name("consolidation"), "full_retention")
        self.assertEqual(pdf_folder_name("consolidation_compressioncalc"), "full_retention")
        self.assertEqual(pdf_folder_name("consolidation_k360"), "full_retention")
        self.assertEqual(pdf_folder_name("consolidation_30d"), "30d")
        self.assertEqual(pdf_folder_name("consolidation_compressioncalc_30d"), "30d")
        self.assertIsNone(pdf_folder_name("account"))

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
        self.assertEqual(row["compressioncalc_accounted_for"], 22.5)
        self.assertEqual(row["compression_ratio"], 0.214)
        self.assertLessEqual(row["compression_ratio"], 1)
        self.assertAlmostEqual(row["bulk_gb_day"] * (22.5 / 105), row["compressioncalc_accounted_for"], places=3)

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
        self.assertEqual(row["compressioncalc_accounted_for"], 30.0)
        self.assertEqual(row["compression_ratio"], 0.2)

    def test_ratio_above_one_uses_the_median(self):
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
        self.assertLessEqual(row["compression_ratio"], 1)
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
        self.assertEqual(observability["compressioncalc_accounted_for"], 25.0)
        self.assertEqual(observability["compression_ratio"], 0.227)
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
        self.assertEqual(swapped["daily_raw_gb"], 25.0)
        self.assertEqual(swapped["logs_gb_day"], 22.5)
        self.assertEqual(swapped["metrics_gb_day"], 2.5)
        self.assertEqual(swapped["bulk_gb_day"], 110.0)
        self.assertEqual(swapped["compression_ratio"], 0.227)
        self.assertEqual(swapped["retention_months"], 2)
        self.assertEqual(security["daily_raw_gb"], 0.0)
        self.assertEqual(security["bulk_gb_day"], 10.0)
        self.assertIsNone(security["compression_ratio"])
        self.assertNotIn("retention_months", security)
        self.assertEqual(search["daily_raw_gb"], 8.0)
        self.assertEqual(priced["sizing"]["estimators"]["search"]["readback"]["monthly_steady_state"], 12)
        self.assertNotIn("observability", priced["sizing"]["estimators"])
        self.assertEqual([p["solution"] for p in priced["sizing"]["estimators"]["projects"]], ["search"])
        self.assertNotIn("consolidation", priced)
        self.assertTrue(priced["sizing"]["notes"][0].startswith("compressioncalc_accounted_for"))
        _bulk, ratio, accounted = _compression_totals(priced)
        self.assertEqual(accounted, 25.0)
        self.assertEqual(ratio, 0.227)
        self.assertLessEqual(ratio, 1)


if __name__ == "__main__":
    unittest.main()
