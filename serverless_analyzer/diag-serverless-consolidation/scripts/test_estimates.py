"""Estimates tests for the diagnostic quote. No Google and no sheets."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(HERE))
from skill_paths import find_skill, scripts_dir  # noqa: E402

K360 = scripts_dir(find_skill("k360-account-analysis", HERE))
sys.path.insert(0, str(K360))

from consolidation import (  # noqa: E402
    _deployment_groups,
    _replicate_by_region,
    build_consolidation,
    regional_quote,
)
from diag_account import apply_diagnostics  # noqa: E402
from quote_checks import (  # noqa: E402
    TEMPLATE_ANCHORS, _search_regions_row, addon_off_value, e73_blocks_quote, echo_gate, integrity_score, money,
    template_check, template_version,
)
from render_pdf import (  # noqa: E402
    consolidation_pdf_stem,
    export_documents,
    retire_legacy_pdf_folders,
    search_power_section,
    stat,
)
from size_serverless import (  # noqa: E402
    COMPRESSION_FOLDER_NAME,
    PLAN_FOLDER_NAME,
    _search_volume_scale,
    architecture_regions,
    assemble_plan_bars,
    assert_not_template,
    copy_template,
    estimate_folder_name,
    estimate_subfolder,
    estimate_title,
    estimator_skip,
    fleet_sheet_region,
    plan_sheet_jobs,
    prefer_regional_estimators,
    relocate_local_estimates,
)

from _test_support import HERE, K360, _quote_account, _stamp_reconciled_billing  # noqa: E402


class GateTests(unittest.TestCase):
    def test_the_echo_tolerance_is_a_share_of_the_rate_with_a_floor(self):
        from quote_checks import echo_tolerance
        self.assertEqual(echo_tolerance(10), 0.02)                 # the floor
        self.assertEqual(echo_tolerance(0), 0.02)
        self.assertAlmostEqual(echo_tolerance(50_000), 50.0)       # 0.1% of a large rate
        self.assertTrue(echo_gate(10, 10.02)["accepted"])
        self.assertFalse(echo_gate(10, 10.03)["accepted"])         # was accepted up to 10.51
        # A sheet under half a GB/day can now fail. It could not before.
        self.assertFalse(echo_gate(0.3, 0.6)["accepted"])
        self.assertTrue(echo_gate(50_000, 50_040)["accepted"])
        self.assertFalse(echo_gate(50_000, 50_060)["accepted"])
        self.assertEqual(echo_gate(10, 10.5)["tolerance"], 0.02)
        self.assertEqual(money("($1,234)"), -1234.0)
        self.assertTrue(e73_blocks_quote("($1,234)"))
        self.assertTrue(e73_blocks_quote(None))
        self.assertTrue(e73_blocks_quote(""))
        self.assertTrue(e73_blocks_quote("n/a"))
        self.assertFalse(e73_blocks_quote(0))
        self.assertFalse(e73_blocks_quote("0"))
        self.assertFalse(e73_blocks_quote(0.0))

    def test_overhead_callout_uses_the_active_deployments(self):
        acc = _quote_account(g28=10, e73=0)
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
        account = _quote_account(g28=10.01, e73=0)
        _stamp_reconciled_billing(account)
        quoted = build_consolidation(account, None)
        observability = quoted["serverless"]["by_solution"]["observability"]
        security = quoted["serverless"]["by_solution"]["security"]
        self.assertIn("quotable", observability["pricing"])
        self.assertEqual(observability["daily_raw_gb"], 10)
        self.assertEqual(observability["sheet_raw_gb_day"], 10.01)
        self.assertIsNone(observability["ingest_mo"])
        self.assertIsNone(observability["retention_mo"])
        self.assertIsNone(quoted["serverless"]["eff_ingest_usd_per_gb"])
        self.assertIsNone(quoted["serverless"]["eff_retain_usd_per_gb_mo"])
        self.assertIn("quotable", security["pricing"])
        self.assertEqual(quoted["search_power"]["profiles"][0]["Total $/month"], 999)
        self.assertEqual(quoted["savings"]["serverless_mo"], 180)
        integrity = quoted["integrity"]
        sheet = next(row for row in integrity["checks"] if row["check"] == "Sheet echo")
        self.assertEqual(sheet["score"], 0.833)
        self.assertTrue(sheet["passed"])
        self.assertEqual(integrity["score"], 0.967)
        self.assertTrue(integrity["passed"])
        page = search_power_section({"consolidation": quoted})
        self.assertIn("Quote integrity 0.967", page)
        self.assertIn(">Pass<", page)
        bare = _quote_account(g28=10, e73=0)
        _stamp_reconciled_billing(bare)
        bare["sizing"]["estimators"]["search"] = {"status": "skipped"}
        bare["storage"]["data_stored_gb"] = 100
        unpriced = build_consolidation(bare, None)
        self.assertIsNone(unpriced["search_power"])
        self.assertEqual(unpriced["savings"]["serverless_mo"], 180)
        self.assertEqual([row["monthly"] for row in quoted["deployment_plan"]], [40, 100])
        skipped = _quote_account(g28=10, e73=0)
        _stamp_reconciled_billing(skipped)
        skipped["sizing"]["estimators"]["search"] = {"status": "skipped — no search workload detected"}
        skipped["storage"]["data_stored_gb"] = 100
        skipped["workload"] = {"search": {"peak": {"qps": 50}}}
        skipped["sizing"]["search_power"] = {
            "profiles": [{"Search Power Setting": "performant", "Total $/month": 4734}],
            "inputs": {"stored_gb": 100},
        }
        no_search = build_consolidation(skipped, None)
        self.assertIsNone(no_search["search_power"])
        page = search_power_section({"consolidation": {**no_search, "search_power": skipped["sizing"]["search_power"]}, "sizing": skipped["sizing"]})
        self.assertNotIn("Monthly search-power estimate", page)

    def test_security_echo_counts_in_sheet_echo_check(self):
        from quote_checks import integrity_score
        facts = {
            "observability": {"has_workload": False},
            "security": {"has_workload": True, "sheet_read": True, "written_bulk": 10, "sheet_raw": 11, "e73": 0},
        }
        row = next(r for r in integrity_score(facts)["checks"] if r["check"] == "Sheet echo")
        self.assertEqual(row["score"], 0.5)
        self.assertFalse(row["passed"])
        facts["security"]["sheet_raw"] = None
        row = next(r for r in integrity_score(facts)["checks"] if r["check"] == "Sheet echo")
        self.assertEqual(row["score"], 0.5)
        self.assertFalse(row["passed"])

    def test_g28_miss_and_nonzero_e73_leave_the_solution_unquoted(self):
        missed_account = _quote_account(g28=10.03, e73=0)
        _stamp_reconciled_billing(missed_account)
        missed = build_consolidation(missed_account, None)
        observability = missed["serverless"]["by_solution"]["observability"]
        self.assertTrue(observability["pricing"].startswith("rejected"))
        self.assertEqual(observability["total_mo"], 0)
        self.assertTrue(any("10.03" in note and "10" in note for note in missed["notes"]))
        blocked = build_consolidation(_quote_account(g28=10, e73="($25)"), None)
        security = blocked["serverless"]["by_solution"]["security"]
        self.assertTrue(security["pricing"].startswith("rejected"))
        self.assertEqual(security["total_mo"], 0)
        for blank in (None, ""):
            blanked = build_consolidation(_quote_account(g28=10, e73=blank), None)
            blank_security = blanked["serverless"]["by_solution"]["security"]
            self.assertTrue(blank_security["pricing"].startswith("rejected"))
            self.assertEqual(blank_security["total_mo"], 0)
        kept_account = _quote_account(g28=10, e73=0)
        _stamp_reconciled_billing(kept_account)
        kept = build_consolidation(kept_account, None)
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
        self.assertIsNone(by_name["Retention basis"]["passed"])
        self.assertFalse(by_name["Line agreement"]["passed"])
        self.assertTrue(by_name["Year agreement"]["passed"])
        # A high mean does not pass the quote. One failed check fails it.
        self.assertGreaterEqual(scored["score"], 0.70)
        self.assertFalse(scored["passed"])
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
        self.assertEqual(coverage["missing_diagnostics"], 1)
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


class EstimateFolderTests(unittest.TestCase):
    def test_estimates_sit_directly_in_generated_estimates(self):
        for suffix in ("", None, "from_k360", "_30d", "30d"):
            self.assertEqual(estimate_subfolder(suffix), "")
        self.assertEqual(estimate_subfolder("compressioncalc_accounted_for"), "compression_applied")
        self.assertEqual(estimate_subfolder("_compressioncalc_accounted_for"), "compression_applied")
        self.assertEqual(estimate_subfolder("compressioncalc_accounted_for_30d"), "compression_applied")
        for suffix in ("", None, "_30d", "30d", "from_k360", "compressioncalc_accounted_for", "compressioncalc_accounted_for_30d"):
            self.assertEqual(estimate_folder_name(suffix), "generated_estimates")
        self.assertEqual(PLAN_FOLDER_NAME, "plan_sheets")
        self.assertEqual(COMPRESSION_FOLDER_NAME, "compression_applied")

    def test_search_disk_is_never_scaled_by_a_retention_month(self):
        self.assertEqual(_search_volume_scale(""), 1.0)
        self.assertEqual(_search_volume_scale(None), 1.0)
        self.assertEqual(_search_volume_scale("_30d"), 1.0)
        self.assertEqual(_search_volume_scale("from_k360"), 1.0)
        self.assertEqual(_search_volume_scale("compressioncalc_accounted_for"), 1.0)

    def test_one_project_ceiling_is_labeled_ceiling(self):
        rows = [{"name": "o", "solution": "observability", "region": "east", "provider": "aws", "bulk_gb_day": 10, "store_gb": 100, "retention_days": 30}]
        specs, jobs, err, _floor = plan_sheet_jobs(
            rows, run_key="Acme", name_suffix="", force_months=None, o11y_id="o11y", security_id="sec",
            parent_id="parent", reuse=True, provider="aws", region_name="east", support="Premium", feature_tier="Complete",
        )
        self.assertIsNone(err)
        self.assertEqual(jobs, [])
        self.assertEqual(specs[0]["label"], "Ceiling")
        self.assertTrue(specs[0]["reused_headline"])

    def test_unverified_search_telemetry_leaves_search_out_and_keeps_the_plan(self):
        rows = [
            {"name": "s1", "solution": "search", "region": "us-east-1", "provider": "aws", "bulk_gb_day": 0, "store_gb": 100, "retention_days": 30},
            {"name": "s2", "solution": "search", "region": "us-east-1", "provider": "aws", "bulk_gb_day": 0, "store_gb": 50, "retention_days": 30},
            {"name": "o1", "solution": "observability", "region": "us-east-1", "provider": "aws", "bulk_gb_day": 10, "store_gb": 100, "retention_days": 30, "logs_gb_day": 10},
            {"name": "o2", "solution": "observability", "region": "us-east-1", "provider": "aws", "bulk_gb_day": 6, "store_gb": 60, "retention_days": 30, "logs_gb_day": 6},
        ]
        workload = {"quote_window_telemetry": {"status": "incomplete", "reason": "fetch exceeded 180s"}}
        specs, jobs, err, _floor = plan_sheet_jobs(
            rows, run_key="Acme", name_suffix="", force_months=None, o11y_id="o11y", security_id="sec",
            parent_id="parent", reuse=True, provider="aws", region_name="us-east-1", support="Premium", feature_tier="Complete",
            workload=workload, search_notebook=True,
        )
        self.assertIsNone(err)
        self.assertFalse([j for j in jobs if j["solution"] == "search"])
        left_out = [g for sp in specs for g in sp["groups"] if g.get("search_left_out")]
        self.assertTrue(left_out)
        self.assertTrue(all(g["monthly"] == 0.0 for g in left_out))

    def test_security_plan_uses_the_security_template(self):
        rows = [
            {"name": "a", "solution": "security", "region": "east", "provider": "aws", "bulk_gb_day": 4, "store_gb": 40, "retention_days": 30},
            {"name": "b", "solution": "security", "region": "east", "provider": "aws", "bulk_gb_day": 2, "store_gb": 20, "retention_days": 30},
        ]
        _specs, jobs, err, _floor = plan_sheet_jobs(
            rows, run_key="Acme", name_suffix="", force_months=None, o11y_id="o11y", security_id="sec",
            parent_id="parent", reuse=True, provider="aws", region_name="east", support="Premium", feature_tier="Complete",
        )
        self.assertIsNone(err)
        self.assertTrue(jobs)
        self.assertTrue(all(job["solution"] == "security" for job in jobs))
        self.assertTrue(all(job["template_id"] == "sec" for job in jobs))
        self.assertTrue(all("_Security_plan_" in job["title"] for job in jobs))

    def test_estimates_move_out_of_retention_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "generated_estimates"
            for sub in ("plan_sheets", "full_retention", "30d", "compression_applied/full_retention", "compression_applied/30d"):
                (parent / sub).mkdir(parents=True)
            bulk = parent / "full_retention" / "Acme_search_full retention_bulk_serverless_estimate.csv"
            day = parent / "30d" / "Acme_search_30d_bulk_serverless_estimate.csv"
            comp = parent / "compression_applied" / "full_retention" / "Acme_search_full retention_compression-applied_serverless_estimate.csv"
            comp_30 = parent / "compression_applied" / "30d" / "Acme_search_30d_compression-applied_serverless_estimate.csv"
            plan = parent / "plan_sheets" / "Acme_Observability_plan_0_serverless_estimate.csv"
            other = parent / "30d" / "notes.txt"
            for path, text in (
                (bulk, "bulk"), (day, "30"), (comp, "comp"), (comp_30, "comp30"), (plan, "plan"), (other, "note"),
            ):
                path.write_text(text)
            names = relocate_local_estimates(parent)
            self.assertEqual(sorted(names), sorted([bulk.name, day.name, comp.name, comp_30.name]))
            self.assertEqual((parent / bulk.name).read_text(), "bulk")
            self.assertEqual((parent / day.name).read_text(), "30")
            self.assertEqual((parent / "compression_applied" / comp.name).read_text(), "comp")
            self.assertEqual((parent / "compression_applied" / comp_30.name).read_text(), "comp30")
            self.assertEqual(plan.read_text(), "plan")
            # An emptied retention folder is removed. One that still holds another file stays.
            self.assertFalse((parent / "full_retention").exists())
            self.assertFalse((parent / "compression_applied" / "full_retention").exists())
            self.assertFalse((parent / "compression_applied" / "30d").exists())
            self.assertEqual(other.read_text(), "note")
            self.assertEqual(relocate_local_estimates(parent), [])

    def test_a_newer_estimate_already_in_place_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "generated_estimates"
            (parent / "full_retention").mkdir(parents=True)
            name = "Acme_search_full retention_bulk_serverless_estimate.csv"
            stale = parent / "full_retention" / name
            stale.write_text("stale")
            os.utime(stale, (1_000, 1_000))
            (parent / name).write_text("current")
            self.assertEqual(relocate_local_estimates(parent), [])
            self.assertEqual((parent / name).read_text(), "current")
            self.assertFalse((parent / "full_retention").exists())

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

    def test_every_report_is_written_to_the_run_folder(self):
        acc = {"meta": {"run_key": "Acme_2026-11-13"}, "consolidation": {"savings": {}}}
        kinds = ("consolidation", "consolidation_30d", "consolidation_k360", "appendix")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("render_pdf.render_consolidation_html", return_value="<html></html>"), \
                mock.patch("render_pdf.render_appendix_html", return_value="<html></html>"):
            folder = Path(tmp) / "Acme_2026-11-13"
            out = export_documents([(acc, kind) for kind in kinds], folder, skip_pdf=True)
            for kind in kinds:
                self.assertEqual(Path(out[kind]["html"]).parent, folder, kind)
            self.assertEqual(sorted(p.name for p in folder.iterdir() if p.is_dir()), [])

    def test_reports_move_out_of_retention_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for sub in ("full_retention", "30d", "generated_estimates/full_retention"):
                (folder / sub).mkdir(parents=True)
            full = "Acme_full retention_bulk_serverless_consolidation_plan.pdf"
            day = "Acme_30d_bulk_serverless_consolidation_plan.pdf"
            (folder / "full_retention" / full).write_text("older")
            os.utime(folder / "full_retention" / full, (1_000, 1_000))
            (folder / full).write_text("newer")
            (folder / "30d" / day).write_text("only copy")
            (folder / "30d" / "keep.txt").write_text("not a report")
            sheet = folder / "generated_estimates" / "full_retention" / "Acme_search_full retention_bulk_serverless_estimate.csv"
            sheet.write_text("estimate")
            self.assertEqual(retire_legacy_pdf_folders(folder), [day])
            self.assertEqual((folder / full).read_text(), "newer")
            self.assertEqual((folder / day).read_text(), "only copy")
            self.assertFalse((folder / "full_retention").exists())
            # A folder that still holds something else is left alone, and so are the estimates.
            self.assertEqual((folder / "30d" / "keep.txt").read_text(), "not a report")
            self.assertEqual(sheet.read_text(), "estimate")
            self.assertEqual(retire_legacy_pdf_folders(folder), [])

    def test_consolidation_pdf_names(self):
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


class RegionalQuoteTests(unittest.TestCase):
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
                {"region": "east", "stored_gb": 10, "kind": "own", "own_monthly": 180, "own_annual": 2160, "own_pricing": "re-estimated performant"},
                {"region": "west", "stored_gb": 10, "kind": "own", "own_monthly": 90, "own_annual": 1080, "own_pricing": "re-estimated performant"},
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
                {"region": "west", "stored_gb": 10, "kind": "rejected", "own_pricing": "rejected — the sheet echo differs from the written bulk"},
            ],
            300,
            solution="observability",
        )
        self.assertEqual(quote["mode"], "rejected")
        self.assertEqual(quote["total_mo"], 0.0)
        self.assertTrue(all(row["monthly"] is None for row in quote["rows"]))

    def test_a_missing_region_without_a_fleet_readback_zeros_the_solution(self):
        acc = {
            "cloud": {"summary": {}},
            "storage": {},
            "sizing": {"consolidated": {}, "estimators": {"projects": [
                {
                    "solution": "observability", "region": "east", "status": "filled",
                    "readback": {"monthly_steady_state": 180, "total_raw_gb_day": 10, "annual_year1": 2160, "annual_steady_state": 2160},
                    "echo_gate": {"accepted": True},
                },
            ]}},
            "workload": {},
        }
        by_solution = {"observability": {
            "pricing": "Observability Complete ladders",
            "total_mo": 300, "total_yr": 3600, "stored_gb": 20,
        }}
        groups = [
            {"solution": "observability", "region": "east", "stored_gb": 10, "daily_raw_gb": 10, "retention_months": 1},
            {"solution": "observability", "region": "west", "stored_gb": 10, "daily_raw_gb": 10, "retention_months": 1},
        ]
        notes: list[str] = []
        _replicate_by_region(acc, by_solution, groups, notes)
        self.assertEqual(by_solution["observability"]["total_mo"], 0.0)
        self.assertEqual(by_solution["observability"]["regional_mode"], "missing")
        self.assertTrue(any("no read-back" in note for note in notes))

    def test_an_unquoted_search_region_is_left_out_and_the_rest_price(self):
        quote = regional_quote(
            [
                {"region": "east", "stored_gb": 10, "kind": "own", "own_monthly": 180, "own_annual": 2160, "own_pricing": "Search sheet performant (G110)"},
                {"region": "west", "stored_gb": 10, "kind": "own", "own_monthly": 90, "own_annual": 1080, "own_pricing": "Search sheet performant (G110)"},
                {"region": "north", "stored_gb": 30, "kind": "unquoted", "own_pricing": "not quoted — no Search sheet read-back"},
            ],
            1000,
            solution="search",
            consolidated_annual=12000,
        )
        self.assertEqual(quote["mode"], "per_region")
        self.assertEqual(quote["total_mo"], 270.0)  # not a stored-GB share of the 1000 consolidated total
        by = {row["region"]: row for row in quote["rows"]}
        self.assertIsNone(by["north"]["monthly"])
        self.assertEqual(by["east"]["monthly"], 180)

    def test_every_search_region_unquoted_omits_search(self):
        quote = regional_quote(
            [{"region": "east", "stored_gb": 10, "kind": "unquoted"}, {"region": "west", "stored_gb": 10, "kind": "unquoted"}],
            1000, solution="search",
        )
        self.assertEqual(quote["mode"], "unquoted")
        self.assertEqual(quote["total_mo"], 0.0)

    def test_a_search_region_without_a_sheet_readback_stays_off_the_ech_side(self):
        sheet = lambda region, monthly: {
            "solution": "search", "region": region, "status": "filled",
            "readback": {"monthly_steady_state": monthly, "annual_year1": monthly * 12, "annual_steady_state": monthly * 12},
        }
        acc = {
            "cloud": {"summary": {}, "by_deployment": [
                {"name": "s-east", "region": "east"}, {"name": "s-west", "region": "west"}, {"name": "s-north", "region": "north"},
            ]},
            "storage": {},
            "solutions": {"by_deployment": [
                {"name": "s-east", "solution": "search", "gb": 10}, {"name": "s-west", "solution": "search", "gb": 10},
                {"name": "s-north", "solution": "search", "gb": 30},
            ]},
            "sizing": {"consolidated": {}, "estimators": {"projects": [sheet("east", 180), sheet("west", 90)]}},
            "workload": {},
        }
        by_solution = {"search": {"pricing": "es3-estimator performant (quotable)", "total_mo": 1000, "total_yr": 12000, "stored_gb": 50}}
        groups = [
            {"solution": "search", "region": "east", "stored_gb": 10, "daily_raw_gb": 0, "deployments": ["s-east"]},
            {"solution": "search", "region": "west", "stored_gb": 10, "daily_raw_gb": 0, "deployments": ["s-west"]},
            {"solution": "search", "region": "north", "stored_gb": 30, "daily_raw_gb": 0, "deployments": ["s-north"]},
        ]
        notes: list[str] = []
        _replicate_by_region(acc, by_solution, groups, notes)
        search = by_solution["search"]
        self.assertEqual(search["regional_mode"], "per_region")
        self.assertEqual(search["total_mo"], 270.0)
        flags = {d["name"]: d.get("search_unquoted") for d in acc["solutions"]["by_deployment"]}
        self.assertEqual(flags, {"s-east": None, "s-west": None, "s-north": True})
        from consolidation_scope import unpriced_reason
        self.assertIsNone(unpriced_reason(acc["solutions"]["by_deployment"][0], by_solution, True))
        self.assertIn("no estimator read-back", unpriced_reason(acc["solutions"]["by_deployment"][2], by_solution, True))
        row = _search_regions_row(search["by_region"])
        self.assertTrue(row["passed"])
        self.assertTrue(row["warning"])
        self.assertAlmostEqual(row["score"], 20 / 50, places=3)
        self.assertIn("north", row["detail"])

    def test_security_readback_under_ten_dollars_stays(self):
        acc = {
            "cloud": {"summary": {}},
            "storage": {},
            "sizing": {"consolidated": {"security": {"retention_months": 1}}, "estimators": {"projects": [
                {"solution": "security", "region": "east", "status": "filled", "readback": {"monthly_steady_state": 80, "cps_addon_monthly": 0, "annual_year1": 960, "annual_steady_state": 960}},
                {"solution": "security", "region": "west", "status": "filled", "readback": {"monthly_steady_state": 8, "cps_addon_monthly": 0, "annual_year1": 96, "annual_steady_state": 96}},
            ]}},
            "workload": {},
        }
        by_solution = {"security": {"pricing": "Security sheet read-back (quotable)", "total_mo": 80, "total_yr": 960, "stored_gb": 20}}
        groups = [
            {"solution": "security", "region": "east", "stored_gb": 10, "daily_raw_gb": 4, "retention_months": 1},
            {"solution": "security", "region": "west", "stored_gb": 10, "daily_raw_gb": 0.2, "retention_months": 1},
        ]
        notes: list[str] = []
        _replicate_by_region(acc, by_solution, groups, notes)
        self.assertEqual(by_solution["security"]["regional_mode"], "per_region")
        self.assertEqual(by_solution["security"]["total_mo"], 88.0)
        self.assertEqual([row["region"] for row in by_solution["security"]["by_region"]], ["east", "west"])

    def test_regional_month_keeps_the_fraction_the_fleet_rounds_up(self):
        days = 12.004 * 30.416
        acc = {"solutions": {"by_deployment": [
            {"name": "a", "solution": "observability", "platform": "ech", "gb": 10, "ecu": 0, "daily_raw_gb": 10, "retention_days": days},
        ]}}
        groups = _deployment_groups(acc, None, 1)
        self.assertEqual(groups[0]["retention_months"], 12)   # one-year estimate
        self.assertAlmostEqual(groups[0]["retention_months_measured"], 12.004, places=3)


class _FakeDrive:
    """Just enough of the Drive client for the estimate dedupe: a folder tree of named sheets."""

    def __init__(self, files):
        self.files_ = files
        self.trashed: list[str] = []

    def files(self):
        return self

    def list(self, q="", **_kw):
        import re as _re
        parent = _re.search(r"'([^']+)' in parents", q).group(1)
        want = _re.search(r"name = '([^']+)'", q)
        rows = [f for f in self.files_ if f["parent"] == parent and f["id"] not in self.trashed and (not want or f["name"] == want.group(1))]
        rows.sort(key=lambda f: f["createdTime"], reverse=True)
        return _Req({"files": rows})

    def update(self, fileId, body=None, addParents=None, removeParents=None, **_kw):
        if (body or {}).get("trashed"):
            self.trashed.append(fileId)
        if addParents:
            for f in self.files_:
                if f["id"] == fileId and f["parent"] == removeParents:
                    f["parent"] = addParents
        return _Req({})

    def parent_of(self, file_id):
        return next(f["parent"] for f in self.files_ if f["id"] == file_id)


class _Req:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class DriveFolderLayoutTests(unittest.TestCase):
    """Estimates sit in generated_estimates and PDFs in the customer folder. The old retention folders go."""

    FOLDER = "application/vnd.google-apps.folder"
    SHEET = "application/vnd.google-apps.spreadsheet"
    PDF = "application/pdf"

    def test_drive_estimates_move_up_and_empty_folders_are_trashed(self):
        from size_serverless import relocate_drive_estimates
        full = "Acme_observability_full retention_bulk_us-east-1_serverless_estimate"
        day = "Acme_observability_30d_bulk_us-east-1_serverless_estimate"
        comp = "Acme_observability_30d_compression-applied_serverless_estimate"
        drive = _FakeDrive([
            {"id": "fr", "name": "full_retention", "mimeType": self.FOLDER, "parent": "est", "createdTime": "1"},
            {"id": "d30", "name": "30d", "mimeType": self.FOLDER, "parent": "est", "createdTime": "1"},
            {"id": "plans", "name": "plan_sheets", "mimeType": self.FOLDER, "parent": "est", "createdTime": "1"},
            {"id": "comp", "name": "compression_applied", "mimeType": self.FOLDER, "parent": "est", "createdTime": "1"},
            {"id": "comp30", "name": "30d", "mimeType": self.FOLDER, "parent": "comp", "createdTime": "1"},
            {"id": "s1", "name": full, "mimeType": self.SHEET, "parent": "fr", "createdTime": "2"},
            {"id": "s2", "name": day, "mimeType": self.SHEET, "parent": "d30", "createdTime": "2"},
            {"id": "s3", "name": "notes", "mimeType": self.SHEET, "parent": "d30", "createdTime": "2"},
            {"id": "s4", "name": comp, "mimeType": self.SHEET, "parent": "comp30", "createdTime": "2"},
            {"id": "s5", "name": "Acme_Observability_plan_0pct_3projects_1", "mimeType": self.SHEET, "parent": "plans", "createdTime": "2"},
        ])
        moved = relocate_drive_estimates(drive, "est")
        self.assertEqual(sorted(moved), sorted([full, day, comp]))
        self.assertEqual(drive.parent_of("s1"), "est")
        self.assertEqual(drive.parent_of("s2"), "est")
        self.assertEqual(drive.parent_of("s4"), "comp")
        self.assertEqual(drive.parent_of("s5"), "plans")
        # full_retention and compression_applied/30d are empty and go. 30d still holds another sheet and stays.
        self.assertEqual(sorted(drive.trashed), ["comp30", "fr"])
        self.assertEqual(relocate_drive_estimates(drive, "est"), [])

    def test_drive_pdfs_move_up_and_empty_folders_are_trashed(self):
        from upload_pdfs import retire_legacy_pdf_folders as retire_drive_pdf_folders
        full = "Acme_full retention_bulk_serverless_consolidation_plan.pdf"
        day = "Acme_30d_bulk_serverless_consolidation_plan.pdf"
        drive = _FakeDrive([
            {"id": "fr", "name": "full_retention", "mimeType": self.FOLDER, "parent": "root", "createdTime": "1"},
            {"id": "d30", "name": "30d", "mimeType": self.FOLDER, "parent": "root", "createdTime": "1"},
            {"id": "est", "name": "generated_estimates", "mimeType": self.FOLDER, "parent": "root", "createdTime": "1"},
            {"id": "est_fr", "name": "full_retention", "mimeType": self.FOLDER, "parent": "est", "createdTime": "1"},
            {"id": "p1", "name": full, "mimeType": self.PDF, "parent": "fr", "createdTime": "2"},
            {"id": "p1_root", "name": full, "mimeType": self.PDF, "parent": "root", "createdTime": "3"},
            {"id": "p2", "name": day, "mimeType": self.PDF, "parent": "d30", "createdTime": "2"},
            {"id": "s1", "name": "Acme_search_full retention_bulk_serverless_estimate", "mimeType": self.SHEET, "parent": "est_fr", "createdTime": "2"},
        ])
        self.assertEqual(retire_drive_pdf_folders(drive, "root"), [day])
        # The 30-day PDF had no copy in the root, so it moves and keeps its link. The other one is a duplicate.
        self.assertEqual(drive.parent_of("p2"), "root")
        self.assertEqual(drive.parent_of("p1_root"), "root")
        self.assertEqual(sorted(drive.trashed), ["d30", "fr", "p1"])
        self.assertEqual(drive.parent_of("s1"), "est_fr")


class EstimateDedupeTests(unittest.TestCase):
    def test_one_sheet_per_title_keeps_the_newest_across_folders(self):
        import size_serverless as z
        sheet, folder = z.GSHEET_MIME, z.FOLDER_MIME
        files = [
            {"id": "sub", "name": "full_retention", "mimeType": folder, "parent": "root", "createdTime": "1"},
            {"id": "a1", "name": "X_search_full retention_bulk_eastus_serverless_estimate", "mimeType": sheet, "parent": "sub", "createdTime": "2026-11-12T15"},
            {"id": "a2", "name": "X_search_full retention_bulk_eastus_serverless_estimate", "mimeType": sheet, "parent": "sub", "createdTime": "2026-11-12T16"},
            {"id": "a3", "name": "X_search_full retention_bulk_eastus_serverless_estimate", "mimeType": sheet, "parent": "root", "createdTime": "2026-11-12T10"},
            {"id": "b1", "name": "X_search_30d_bulk_eastus_serverless_estimate", "mimeType": sheet, "parent": "sub", "createdTime": "2026-11-12T16"},
        ]
        drive = _FakeDrive(files)
        orig_exec, orig_assert = z._execute, z.assert_not_template
        z._execute = lambda build, attempts=6: build().execute()
        z.assert_not_template = lambda _id: None
        try:
            dry = z.dedupe_estimates(drive, "root", dry_run=True)
            self.assertEqual({r["id"] for r in dry}, {"a1", "a3"})
            self.assertEqual(drive.trashed, [])
            removed = z.dedupe_estimates(drive, "root")
        finally:
            z._execute, z.assert_not_template = orig_exec, orig_assert
        self.assertEqual({r["id"] for r in removed}, {"a1", "a3"})
        self.assertEqual(sorted(drive.trashed), ["a1", "a3"])
        self.assertTrue(all(r["kept"] == "a2" for r in removed))

    def test_a_measured_retention_copy_is_trashed(self):
        import size_serverless as z
        sheet = z.GSHEET_MIME
        quote = "X_observability_full retention_bulk_us-central1_serverless_estimate"
        files = [
            {"id": "q", "name": quote, "mimeType": sheet, "parent": "root", "createdTime": "2026-11-18T15"},
            {"id": "m", "name": quote + " measured retention", "mimeType": sheet, "parent": "root", "createdTime": "2026-11-18T15"},
        ]
        drive = _FakeDrive(files)
        orig_exec, orig_assert = z._execute, z.assert_not_template
        z._execute = lambda build, attempts=6: build().execute()
        z.assert_not_template = lambda _id: None
        try:
            removed = z.dedupe_estimates(drive, "root")
        finally:
            z._execute, z.assert_not_template = orig_exec, orig_assert
        self.assertEqual([r["id"] for r in removed], ["m"])
        self.assertEqual(drive.trashed, ["m"])


class TemplateGuardTests(unittest.TestCase):
    @staticmethod
    def _labels(kind: str) -> dict:
        return {cell: f"What is the {text.upper()}?\n\nNote: more text" for cell, text in TEMPLATE_ANCHORS[kind]["labels"].items()}

    def test_matching_layout_passes_for_both_estimators(self):
        for kind, version in (("observability", "1.22"), ("security", "2.6")):
            check = template_check(kind, self._labels(kind), version)
            self.assertEqual(check["problems"], [], kind)
            self.assertFalse(check["newer"])

    def test_a_moved_row_is_refused_and_names_the_cell(self):
        # The v1.19 layout had total raw GB/day two rows higher, where traces per minute is now.
        labels = self._labels("observability")
        labels["D26"], labels["D28"] = labels["D28"], "Cross-Project Search Add-on"
        problems = template_check("observability", labels, "1.22")["problems"]
        self.assertEqual(len(problems), 2)
        self.assertTrue(problems[0].startswith("D26 should read 'traces per minute'"))
        self.assertTrue(problems[1].startswith("D28 should read 'total raw ingest in gb/day'"))

    def test_blank_label_is_refused(self):
        labels = self._labels("security")
        labels["D74"] = None
        problems = template_check("security", labels, "2.6")["problems"]
        self.assertEqual(problems, ["D74 should read 'total monthly charges' and is 'blank'"])

    def test_version_older_or_unreadable_is_refused_and_newer_is_marked(self):
        labels = self._labels("observability")
        self.assertIn("older than 1.22", template_check("observability", labels, "1.19")["problems"][0])
        # The sheet prints 1.20 as 1.2, which is still older than 1.22.
        self.assertIn("older than 1.22", template_check("observability", labels, "1.2")["problems"][0])
        self.assertIn("no readable version", template_check("observability", labels, None)["problems"][0])
        self.assertIn("no readable version", template_check("observability", labels, "Version")["problems"][0])
        newer = template_check("observability", labels, "1.23")
        self.assertEqual(newer["problems"], [])
        self.assertTrue(newer["newer"])
        self.assertTrue(template_check("nope", {}, "1.0")["problems"])

    def test_version_is_the_last_filled_cell(self):
        self.assertEqual(template_version(["Version", "0.1", "1.21", "1.22", "", None]), "1.22")
        self.assertIsNone(template_version([]))

    def test_a_fresh_copy_is_trashed_when_its_layout_does_not_match(self):
        import sheets_io
        import size_serverless as ss

        trashed = []
        copied = {"id": "copy-1", "name": "t", "url": "u"}
        for reused, expect in ((False, ["copy-1"]), (True, [])):
            trashed.clear()
            with mock.patch.object(sheets_io, "copy_template", return_value=(copied, reused)), \
                 mock.patch.object(sheets_io, "verify_template", side_effect=ss.TemplateMismatch("bad layout")), \
                 mock.patch.object(sheets_io, "trash_file", side_effect=lambda _drive, fid: trashed.append(fid)):
                with self.assertRaises(ss.TemplateMismatch):
                    ss.copy_verified(None, None, "tpl", "t", "parent", reuse=True, kind="observability", tab=ss.O11Y_TAB)
            self.assertEqual(trashed, expect)

    def test_template_mismatch_stops_the_run(self):
        import subprocess as sp
        import run_consolidation as rc

        with mock.patch.object(rc, "_run_command", side_effect=sp.CalledProcessError(rc.TEMPLATE_MISMATCH_EXIT, ["x"])):
            with self.assertRaises(SystemExit):
                rc._run(["x"])
        with mock.patch.object(rc, "_run_command", side_effect=sp.CalledProcessError(1, ["x"])):
            with self.assertRaises(sp.CalledProcessError):
                rc._run(["x"])




class SecuritySteadyStateTests(unittest.TestCase):
    RB = {"monthly_steady_state": 1150.0, "cps_addon_monthly": 0, "ingest_monthly": 700.0, "retention_monthly": 300.0,
          "support_monthly": 150.0, "steady_total": 1495.0, "annual_steady_state": 13800.0}

    def test_month_12_is_the_quote_up_to_twelve_months(self):
        from quote_checks import security_readback
        rb = security_readback(self.RB, 12)
        self.assertEqual((rb["monthly_steady_state"], rb["annual_steady_state"], rb["other_addons_monthly"]), (1150.0, 13800.0, 0.0))
        self.assertEqual(rb["steady_basis"], "month 12 (E74)")
        self.assertFalse(e73_blocks_quote(rb["cps_addon_monthly"]))

    def test_e74_stays_the_quote_above_twelve_months(self):
        from quote_checks import security_readback
        rb = security_readback(self.RB, 24)
        self.assertEqual(rb["monthly_steady_state"], 1150.0)
        self.assertEqual(rb["steady_basis"], "month 12 (E74)")
        unread = security_readback({**self.RB, "steady_total": None}, 24)
        self.assertEqual(unread["monthly_steady_state"], 1150.0)

    def test_any_other_line_in_e74_blocks_the_quote(self):
        from quote_checks import security_readback
        rb = security_readback({**self.RB, "monthly_steady_state": 1650.0}, 12)   # an LLM or SOAR line of 500
        self.assertEqual((rb["other_addons_monthly"], rb["cps_addon_monthly"], rb["cps_addon_e73"]), (500.0, 500.0, 0))
        self.assertTrue(e73_blocks_quote(rb["cps_addon_monthly"]))
        self.assertIn("E63 to E72", rb["addon_note"])
        # A read-back saved before the lines were read still gates on E73 alone.
        old = security_readback({"monthly_steady_state": 1150.0, "cps_addon_monthly": 0}, 12)
        self.assertNotIn("other_addons_monthly", old)
        self.assertFalse(e73_blocks_quote(old["cps_addon_monthly"]))


class RegionalEchoTests(unittest.TestCase):
    def test_every_regional_sheet_is_scored_and_one_bad_region_fails(self):
        from quote_checks import integrity_score
        def sheet(region, written, echo):
            return {"solution": "observability", "region": region, "written_bulk": written, "sheet_raw": echo}
        good = integrity_score({"regional_sheets": [sheet("east", 40.0, 40.0), sheet("west", 10.0, 10.01)]})
        row = next(r for r in good["checks"] if r["check"] == "Sheet echo")
        self.assertTrue(row["passed"])
        self.assertIn("Observability east", row["detail"])
        bad = integrity_score({"regional_sheets": [sheet("east", 40.0, 40.0), sheet("west", 10.0, 10.5)]})
        row = next(r for r in bad["checks"] if r["check"] == "Sheet echo")
        self.assertFalse(row["passed"])
        self.assertFalse(bad["passed"])
        blank = integrity_score({"regional_sheets": [sheet("east", 40.0, None)]})
        self.assertFalse(next(r for r in blank["checks"] if r["check"] == "Sheet echo")["passed"])

    def test_a_security_region_must_also_have_a_zero_e73(self):
        from quote_checks import integrity_score
        sec = {"solution": "security", "region": "east", "written_bulk": 5.0, "sheet_raw": 5.0, "e73": 40}
        row = next(r for r in integrity_score({"regional_sheets": [sec]})["checks"] if r["check"] == "Sheet echo")
        self.assertFalse(row["passed"])
        self.assertIn("Security east E73 is 40", row["detail"])

    def test_a_multi_region_run_reads_its_regional_sheets_into_the_quote(self):
        acc = _quote_account(g28=10, e73=0)
        est = acc["sizing"]["estimators"]
        est["observability"] = {"kind": "observability", "status": "sized per region"}
        est["projects"] = [{
            "solution": "observability", "region": "east", "daily_raw_gb": 10.0, "echo_gate": {"written_bulk": 10.0, "accepted": True},
            "readback": {"echo_gb_day": 10.4, "monthly_steady_state": 100.0},
        }]
        cons = build_consolidation(acc, None)
        row = next(r for r in cons["integrity"]["checks"] if r["check"] == "Sheet echo")
        self.assertIn("Observability east", row["detail"])
        self.assertFalse(row["passed"])


class SearchTelemetryShareTests(unittest.TestCase):
    """Docs/s and QPS take one ratio: project search store / diagnostic org store."""

    WORKLOAD = {
        "quote_window_telemetry": {"status": "complete"},
        "search": {
            "peak": {"qps": 40, "complexity_ms": 25, "pct_hours_mo": 30},
            "nonpeak": {"qps": 10, "complexity_ms": 25, "pct_hours_mo": 70},
        },
        "ingest": {"docs_per_sec": 100, "avg_doc_bytes": 500, "active_hours_mo": 400},
    }

    def test_two_projects_sum_to_the_fleet_share(self):
        from es3_estimate import build_user_info, scale_search_workload, workload_share
        self.assertEqual(workload_share(20, 100), 0.2)
        self.assertEqual(workload_share(40, 100), 0.4)
        self.assertEqual(workload_share(20, 0), 1.0)
        east = scale_search_workload(self.WORKLOAD, workload_share(20, 100))
        west = scale_search_workload(self.WORKLOAD, workload_share(20, 100))
        fleet = scale_search_workload(self.WORKLOAD, workload_share(40, 100))
        self.assertEqual(self.WORKLOAD["ingest"]["docs_per_sec"], 100)
        self.assertEqual(self.WORKLOAD["search"]["peak"]["qps"], 40)
        for project in (east, west):
            self.assertEqual(project["ingest"]["docs_per_sec"], 20.0)
            self.assertEqual(project["search"]["peak"]["qps"], 8.0)
            self.assertEqual(project["search"]["nonpeak"]["qps"], 2.0)
        self.assertEqual(fleet["ingest"]["docs_per_sec"], 40.0)
        self.assertEqual(fleet["search"]["peak"]["qps"], 16.0)
        self.assertEqual(
            east["ingest"]["docs_per_sec"] + west["ingest"]["docs_per_sec"],
            fleet["ingest"]["docs_per_sec"],
        )
        info, _match, assumptions = build_user_info(
            {
                "stored_gb": 20, "org_stored_gb": 100, "provider": "aws", "region": "us-east-1",
                "support_tier": "Premium", "workload": self.WORKLOAD,
            },
            {"AWS": {"us-east-1": {}}},
            {"Enterprise": {}},
        )
        search = info["SEARCH_CONFIG"]["workloads"]
        ingest = info["INGEST_CONFIG"]["workloads"]
        self.assertEqual(ingest[0]["docs_per_sec"], 20.0)
        self.assertEqual([row["QPS"] for row in search], [8.0, 2.0])
        self.assertTrue(any("Docs/s and QPS scaled" in note for note in assumptions))
        self.assertFalse(any("QPS is not scaled" in note for note in assumptions))

    def _account(self) -> dict:
        return {
            "meta": {"run_key": "Acme", "customer": "Acme", "date": "2026-11-16"},
            "sizing": {"ingest_source": "diagnostics", "feature_tier": "Complete", "region": "east", "consolidated": {"search": {}}},
            "storage": {"ingest_share_store_gb": 100},
            "workload": json.loads(json.dumps(self.WORKLOAD)),
            "solutions": {"by_deployment": [
                {"name": "east", "solution": "search", "platform": "ech", "gb": 20, "ingest_source": "diagnostics"},
                {"name": "west", "solution": "search", "platform": "ech", "gb": 20, "ingest_source": "diagnostics"},
                {"name": "logs", "solution": "observability", "platform": "ech", "gb": 60, "daily_raw_gb": 0, "ingest_source": "diagnostics"},
            ]},
            "cloud": {"summary": {"provider": "aws"}, "by_deployment": [
                {"name": "east", "region": "east", "provider": "aws", "platform": "ech"},
                {"name": "west", "region": "west", "provider": "aws", "platform": "ech"},
                {"name": "logs", "region": "east", "provider": "aws", "platform": "ech"},
            ]},
        }

    def test_the_plan_and_regional_sheets_pass_org_store_and_unscaled_telemetry(self):
        import plan_sizing
        import size_serverless as ss
        seen = []

        def fake_es3(payload):
            seen.append(payload)
            return {"readback": {"monthly_steady_state": 10}}

        acc = self._account()
        # A workload with no regional blocks (the K360 backup) keeps the org-store share, on the notebook path.
        with mock.patch.object(plan_sizing, "run_es3", side_effect=fake_es3):
            plan = ss.size_deployment_plan(
                acc, None, None, {"id": "folder"}, o11y_id="o", security_id="s", support="Premium",
                provider="aws", force_months=12, name_suffix="", fresh=False, search_notebook=True,
            )
        self.assertFalse(plan.get("error"))
        self.assertEqual(len(seen), 2)
        for payload in seen:
            self.assertEqual(payload["stored_gb"], 20)
            self.assertEqual(payload["org_stored_gb"], 100)
            self.assertEqual(payload["workload"]["ingest"]["docs_per_sec"], 100)
            self.assertEqual(payload["workload"]["search"]["peak"]["qps"], 40)
        seen.clear()
        acc["consolidation"] = {"architecture": {"groups": [
            {"solution": "search", "region": "east", "stored_gb": 20, "daily_raw_gb": 0},
            {"solution": "search", "region": "west", "stored_gb": 20, "daily_raw_gb": 0},
        ]}}
        import sheets_io as _sheets_io
        import search_sheet
        import sizing_shared
        with mock.patch.object(search_sheet, "run_es3", side_effect=fake_es3), \
             mock.patch.object(sizing_shared, "_SEARCH_NOTEBOOK", True), \
             mock.patch.object(plan_sizing, "load_sheet_creds", return_value=None), \
             mock.patch.object(plan_sizing, "services_from", return_value=(None, None)), \
             mock.patch.object(_sheets_io, "sheet_workers", return_value=1):
            ss.size_regional_projects(
                acc, None, None, {"id": "folder"}, o11y_id="o", security_id="s", support="Premium",
                provider="aws", skip_xlsx=True, fresh=False, local_dir=HERE, customer="Acme", when="2026-11-16",
                force_months=12,
            )
        self.assertEqual(len(seen), 2)
        for payload in seen:
            self.assertEqual(payload["stored_gb"], 20)
            self.assertEqual(payload["org_stored_gb"], 100)
            self.assertEqual(payload["workload"]["search"]["nonpeak"]["qps"], 10)
        captured = []

        def fake_region(payload, failures=None):
            captured.append(payload)
            return {"readback": {"monthly_steady_state": 10, "annual_year1": 120, "annual_steady_state": 120}}

        groups = [
            {"solution": "search", "region": "east", "stored_gb": 20, "daily_raw_gb": 0, "deployments": ["east"]},
            {"solution": "search", "region": "west", "stored_gb": 20, "daily_raw_gb": 0, "deployments": ["west"]},
        ]
        by_solution = {"search": {"pricing": "es3-estimator performant (quotable)", "total_mo": 50, "stored_gb": 40}}
        # On the default sheet path a region with no sheet read-back is unquoted. The notebook never runs.
        sheet_solution = json.loads(json.dumps(by_solution))
        sheet_notes: list[str] = []
        with mock.patch("consolidation_regional._es3_region", side_effect=fake_region):
            _replicate_by_region(acc, sheet_solution, groups, sheet_notes)
        self.assertEqual(captured, [])
        self.assertTrue(any("no read-back from its Search estimator sheet" in note for note in sheet_notes))
        acc["sizing"].setdefault("estimators", {})["search"] = {"search_path": "notebook"}
        with mock.patch("consolidation_regional._es3_region", side_effect=fake_region):
            _replicate_by_region(acc, by_solution, groups, [])
        self.assertEqual(len(captured), 2)
        for payload in captured:
            self.assertEqual(payload["org_stored_gb"], 100)
            self.assertEqual(payload["stored_gb"], 20)
            self.assertEqual(payload["workload"]["ingest"]["docs_per_sec"], 100)
            self.assertEqual(payload["workload"]["search"]["peak"]["qps"], 40)


class SearchPlanMatchesHeadlineTests(unittest.TestCase):
    """The plan floor prices Search on the inputs the headline sheet got: the region's own workload, share 1."""

    REGIONS = {
        "east": {"search": {"peak": {"qps": 40, "complexity_ms": 120, "pct_hours_mo": 30},
                            "nonpeak": {"qps": 10, "complexity_ms": 120, "pct_hours_mo": 70}},
                 "ingest": {"docs_per_sec": 100, "avg_doc_bytes": 900, "active_hours_mo": 730}},
        "west": {"search": {"peak": {"qps": 4, "complexity_ms": 600, "pct_hours_mo": 30},
                            "nonpeak": {"qps": 1, "complexity_ms": 600, "pct_hours_mo": 70}},
                 "ingest": {"docs_per_sec": 5, "avg_doc_bytes": 900, "active_hours_mo": 730}},
    }

    def _rows(self):
        return [
            {"name": "e1", "solution": "search", "region": "east", "provider": "aws", "store_gb": 30, "bulk_gb_day": 1},
            {"name": "e2", "solution": "search", "region": "east", "provider": "aws", "store_gb": 10, "bulk_gb_day": 1},
            {"name": "w1", "solution": "search", "region": "west", "provider": "aws", "store_gb": 20, "bulk_gb_day": 1},
            {"name": "logs", "solution": "observability", "region": "east", "provider": "aws", "store_gb": 940,
             "bulk_gb_day": 0},
        ]

    def _jobs(self, workload):
        return plan_sheet_jobs(
            self._rows(), run_key="Acme", name_suffix="", force_months=1, o11y_id="o", security_id="s",
            parent_id="p", reuse=True, provider="aws", region_name="east", support="Premium",
            feature_tier="Complete", workload=workload, org_gb=1000, search_id="t",
        )

    def test_the_floor_takes_each_regions_own_workload_unscaled(self):
        from search_inputs import exact_inputs
        workload = {"regions": json.loads(json.dumps(self.REGIONS))}
        specs, jobs, err, _floor = self._jobs(workload)
        self.assertIsNone(err)
        floor = specs[-1]
        for group in floor["groups"]:
            if group.get("sheet") is None:
                continue
            payload = jobs[group["sheet"]]["payload"]
            region = payload["region"]
            headline_workload, headline_org = exact_inputs(workload, region, payload["stored_gb"], 1000)
            # Same workload block and a share of 1, so the sheet gets the headline's inputs exactly.
            self.assertEqual(payload["workload"], headline_workload)
            self.assertEqual(payload["org_stored_gb"], payload["stored_gb"])
            self.assertEqual(headline_org, payload["stored_gb"])
            # Never the other solutions' store.
            self.assertLess(payload["org_stored_gb"], 1000)

    def test_a_packed_project_is_its_share_of_its_own_region(self):
        workload = {"regions": json.loads(json.dumps(self.REGIONS))}
        specs, jobs, err, _floor = self._jobs(workload)
        self.assertIsNone(err)
        for job in jobs:
            payload = job["payload"]
            region_store = 40 if payload["region"] == "east" else 20
            self.assertEqual(payload["org_stored_gb"], region_store)
            self.assertEqual(payload["workload"], self.REGIONS[payload["region"]])

    def test_a_region_with_no_proxy_workload_is_left_out_like_the_headline(self):
        workload = {"regions": {"east": json.loads(json.dumps(self.REGIONS["east"]))}}
        specs, jobs, err, _floor = self._jobs(workload)
        self.assertIsNone(err)
        self.assertEqual({job["payload"]["region"] for job in jobs}, {"east"})
        left_out = [g for sp in specs for g in sp["groups"] if g.get("search_left_out")]
        self.assertTrue(left_out)
        self.assertTrue(all("west has no measured proxy-log workload" in g["search_left_out"] and g["monthly"] == 0.0 for g in left_out))


class SearchPlanSeriesTests(unittest.TestCase):
    """A Search project is priced on its measured store. No month, forced or measured, changes its disk."""

    ROWS = [
        # name, store GB, bulk GB/day, retention days
        ("monitoring", 4034, 282.0, 15), ("big-a", 18986, 0, 2086), ("big-b", 9007, 0.1, 2073),
        ("small-1", 493, 0.1, 2102), ("small-2", 104, 0.07, 2519), ("small-3", 65, 0.08, 2152),
        ("small-4", 7, 0.1, 1434), ("small-5", 1, 0.0, 1918), ("small-6", 0.1, 0.0, 1707),
    ]

    def _rows(self):
        return [{"name": n, "solution": "search", "region": "us-east-1", "provider": "aws", "store_gb": st,
                 "bulk_gb_day": b, "retention_days": d} for n, st, b, d in self.ROWS]

    @staticmethod
    def _price(payload):
        # Linear in disk plus a fixed per-project ingest floor, like the es3 model at this size.
        return 620 + 2.3 * payload["stored_gb"]

    def _bars(self, force, notebook=False):
        import plan_sizing
        seen = []

        def fake_es3(payload):
            seen.append(payload["stored_gb"])
            return {"readback": {"monthly_steady_state": self._price(payload)}}

        with mock.patch.object(plan_sizing, "run_es3", side_effect=fake_es3):
            specs, jobs, err, _floor = plan_sheet_jobs(
                self._rows(), run_key="Acme", name_suffix="", force_months=force, o11y_id="o", security_id="s",
                parent_id="p", reuse=True, provider="aws", region_name="us-east-1", support="Premium",
                feature_tier="Complete", workload={"search": {"peak": {"qps": 5, "complexity_ms": 10, "pct_hours_mo": 50}}},
                org_gb=sum(r[1] for r in self.ROWS), search_id="search-template", search_notebook=notebook,
            )
        self.assertIsNone(err)
        if notebook:
            self.assertEqual(jobs, [])
            fills = []
        else:
            # Every Search project is a Search estimator sheet job. Nothing ran the notebook.
            self.assertEqual(seen, [])
            self.assertTrue(jobs)
            self.assertTrue(all(job["solution"] == "search" and job["template_id"] == "search-template" for job in jobs))
            seen = [job["payload"]["stored_gb"] for job in jobs]
            fills = [{"monthly": round(self._price(job["payload"]), 2)} for job in jobs]
        self.seen = seen
        return assemble_plan_bars(specs, fills)["bars"]

    def test_every_project_is_priced_on_its_whole_store(self):
        total = sum(r[1] for r in self.ROWS)
        for notebook in (False, True):
            with self.subTest(notebook=notebook):
                bars = self._bars(None, notebook)
                # The floor is one project holding every cluster's store, unscaled by any cluster's age.
                self.assertAlmostEqual(bars[-1]["monthly"], 620 + 2.3 * total, delta=1)
                self.assertAlmostEqual(max(self.seen), total, places=3)
                monthly = [b["monthly"] for b in bars]
                for later, earlier in zip(monthly[1:], monthly):
                    self.assertLessEqual(later, earlier * 1.01)

    def test_a_forced_month_does_not_change_search_disk(self):
        for notebook in (False, True):
            with self.subTest(notebook=notebook):
                self.assertEqual(
                    [b["monthly"] for b in self._bars(None, notebook)], [b["monthly"] for b in self._bars(1, notebook)],
                )

    def test_a_search_project_that_recurs_across_bars_is_one_sheet(self):
        specs, jobs, err, _floor = plan_sheet_jobs(
            self._rows(), run_key="Acme", name_suffix="", force_months=None, o11y_id="o", security_id="s",
            parent_id="p", reuse=True, provider="aws", region_name="us-east-1", support="Premium",
            feature_tier="Complete", workload={}, org_gb=0, search_id="t",
        )
        self.assertIsNone(err)
        distinct = {
            tuple(sorted(g["names"])) for spec in specs for g in spec.get("groups") or [] if g.get("sheet") is not None
        }
        self.assertEqual(len(jobs), len(distinct))
        self.assertEqual(len({job["title"] for job in jobs}), len(jobs))


class OrgReportLayoutTests(unittest.TestCase):
    def test_v119_cells_move_to_the_v122_addresses(self):
        from estimator_layout import OBSERVABILITY_INPUT_CELLS, cell_range, upgrade_observability_cells
        old = {"sheet_tab": "Observability Estimator", "cells": {
            "G4": "Complete", "G5": "Premium", "G8": "r", "G11": 3, "G14": "No", "G18": 0,
            "G21": 90, "G22": "0%", "G23": 10, "G24": 7, "G25": "100%",
        }}
        new = upgrade_observability_cells(old)
        cells = new["cells"]
        self.assertEqual((cells["G23"], cells["G24"], cells["G25"], cells["G26"], cells["G27"]), (90, "0%", 10, 7, "100%"))
        self.assertEqual((cells["G12"], cells["G15"], cells["G19"], cells["G20"], cells["G33"]), (3, "No", 0, 0, "No"))
        self.assertLessEqual(set(cells), OBSERVABILITY_INPUT_CELLS)
        self.assertEqual((new["layout"], new["upgraded_from"]), ("1.22", "1.19"))
        self.assertEqual(upgrade_observability_cells(new), new)       # a tagged block is left alone
        with self.assertRaises(ValueError):
            upgrade_observability_cells({"cells": {"G99": 1}})
        with self.assertRaises(ValueError):
            upgrade_observability_cells({"layout": "1.22", "cells": {"G21": 1}})
        self.assertEqual(cell_range("Security Estimator", "E74"), "'Security Estimator'!E74")
        self.assertEqual(cell_range("Security Estimator", "Estimator_Reference!CB254"), "'Estimator_Reference'!CB254")


if __name__ == "__main__":
    unittest.main()


class SearchYearAgreementTests(unittest.TestCase):
    """The Search read-back's steady annual is 12 x its monthly, on both Search paths."""

    TOTALS = (1234.567, 12.345, 99999.995, 0.005, 1000.0, 3333.333)

    @staticmethod
    def _lines(rb, *, eis=0.0):
        from consolidation import _quote_lines
        by = {"search": {"pricing": "es3-estimator performant (quotable)", "total_mo": rb["monthly_steady_state"],
                         "by_region": [], "eis_with_support_mo": eis}}
        return _quote_lines(by, {"estimators": {"search": {"readback": rb}}})

    def _readbacks(self, raw):
        import es3_estimate
        import search_sheet
        yield "notebook", es3_estimate.readback_from([{"Search Power Setting": "performant", "Total $/month": raw}])
        yield "sheet", search_sheet.readback_from(
            [{"Search Power Setting": "performant", "Total $/month": round(raw, 2)}])

    def test_the_annual_is_twelve_times_the_rounded_monthly_on_both_paths(self):
        for raw in self.TOTALS:
            for path, rb in self._readbacks(raw):
                with self.subTest(raw=raw, path=path):
                    self.assertEqual(rb["monthly_steady_state"], round(raw, 2))
                    self.assertEqual(rb["annual_steady_state"], round(rb["monthly_steady_state"] * 12, 2))
                    self.assertEqual(rb["annual_year1"], rb["annual_steady_state"])

    def test_year_agreement_passes_on_both_paths_without_eis(self):
        from quote_checks import _year_row
        for raw in self.TOTALS:
            for path, rb in self._readbacks(raw):
                with self.subTest(raw=raw, path=path):
                    row = _year_row(self._lines(rb))
                    self.assertTrue(row["passed"], row["detail"])
                    self.assertEqual(row["score"], 1.0)

    def test_year_agreement_passes_when_eis_is_added_to_the_line(self):
        from quote_checks import _year_row
        for raw in self.TOTALS:
            for path, rb in self._readbacks(raw):
                with self.subTest(raw=raw, path=path):
                    self.assertTrue(_year_row(self._lines(rb, eis=17.0))["passed"])

    def test_a_year_that_really_disagrees_still_fails(self):
        # The bar is unchanged: only the producer was inconsistent.
        from quote_checks import _year_row
        import es3_estimate
        rb = es3_estimate.readback_from([{"Search Power Setting": "performant", "Total $/month": 1234.567}])
        rb["annual_steady_state"] = round(rb["annual_steady_state"] + 0.05, 2)
        self.assertFalse(_year_row(self._lines(rb))["passed"])
