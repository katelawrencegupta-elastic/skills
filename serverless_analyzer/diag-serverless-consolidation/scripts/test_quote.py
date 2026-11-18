"""Quote tests for the diagnostic quote. No Google and no sheets."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(HERE))
from skill_paths import find_skill, scripts_dir  # noqa: E402

K360 = scripts_dir(find_skill("k360-account-analysis", HERE))
sys.path.insert(0, str(K360))

from analyze import active_deployment_set  # noqa: E402
from admin_diags import (  # noqa: E402
    _parse_zip, _user_index, admin_base, analyze_bundle, capture_target, capture_url,
    parse_diagnostics, pull_diagnostics, quoted_index_bytes, safe_id,
)
from consolidation import _deployment_groups, _replicate_by_region, apply_quote_lookback, build_consolidation, regional_quote  # noqa: E402
from diag_account import _sheet_months, apply_diagnostics  # noqa: E402
from diag_inputs import index_retention_months, summarize, whole_months  # noqa: E402
from k360_infra import es_base_and_key  # noqa: E402
from privacy import seal_dir, seal_text, unignored_secret_warnings  # noqa: E402
from quote_checks import (  # noqa: E402
    TEMPLATE_ANCHORS, addon_off_value, e73_blocks_quote, echo_gate, integrity_score, money,
    template_check, template_version,
)
from run_consolidation import _apply_diagnostic_report_view, _estimator_flags, consolidation_brief, method_stamp, quote_pdf_jobs  # noqa: E402
from render_pdf import consolidation_pdf_stem, deployment_plan_section, export_documents, render_appendix_html, render_consolidation_html, retire_legacy_pdf_folders, search_power_section, stat  # noqa: E402
from size_serverless import (  # noqa: E402
    COMPRESSION_FOLDER_NAME, DEFAULT_O11Y, DEFAULT_SECURITY, PLAN_FOLDER_NAME, _sheet_retention,
    plan_sheet_jobs,
    assemble_plan_bars, assert_not_template, cell_input_mode, copy_template, estimate_folder_name,
    architecture_regions, estimate_subfolder, estimate_title, estimator_skip, fleet_sheet_region,
    prefer_regional_estimators,
    map_in_order, partition_cells, push_cells,
    _search_volume_scale,
    relocate_local_estimates, require_customer_parent,
)
from vpn_check import tunnel_reaches  # noqa: E402

from _test_support import (  # noqa: F401  shared fixtures
    COLLECTION_MS, HERE, K360, _analyze, _guard_args, _quote_account, _stamp_reconciled_billing,
    _stamp_search_eis, _write_phase, _SheetCall, scope_account,
)


class DeploymentCountTests(unittest.TestCase):
    def test_plan_counts_every_deployment_down_to_region_and_feature(self):
        from diag_inputs import region_feature_plans
        rows = []
        for i in range(3):
            rows.append({"name": f"o{i}", "solution": "observability", "region": "east", "bulk_gb_day": 10, "store_gb": 10})
        for i in range(2):
            rows.append({"name": f"s{i}", "solution": "search", "region": "east", "bulk_gb_day": 1, "store_gb": 5})
        rows.append({"name": "west", "solution": "search", "region": "west", "bulk_gb_day": 1, "store_gb": 1})
        plans = region_feature_plans(rows)
        self.assertEqual([plan["projects"] for plan in plans], [6, 4, 3])
        self.assertEqual([plan["label"] for plan in plans], ["Ceiling", "−25%", "Floor"])
        mixed = [set(row.get("solution") for row in group) for group in plans[-1]["groups"]]
        self.assertTrue(all(len(sols) == 1 for sols in mixed))
        one = [
            {"name": str(i), "solution": "observability", "region": "eastus2", "bulk_gb_day": 1, "store_gb": 1}
            for i in range(43)
        ]
        self.assertEqual(
            [plan["projects"] for plan in region_feature_plans(one)],
            [43, 32, 24, 18, 14, 10, 8, 6, 4, 3, 2, 1],
        )
        from diag_inputs import reduction_counts
        self.assertEqual(reduction_counts(35, 14), [35, 26, 20, 15, 14])

    def test_ceiling_roster_matches_the_cover_count(self):
        from size_serverless import plan_roster
        acc = {
            "deployments": {"active": {"names": ["a", "b"], "window_days": 30}},
            "cloud": {"by_deployment": [
                {"name": "a", "platform": "ech", "region": "east"},
                {"name": "b", "platform": "ech", "region": "west"},
                {"name": "old", "platform": "ech", "region": "east"},
            ]},
            "solutions": {"by_deployment": [
                {"name": "a", "platform": "ech"},
                {"name": "b", "platform": "ech"},
                {"name": "old", "platform": "ech", "ingest_source": "inactive"},
            ]},
        }
        self.assertEqual([row["name"] for row in plan_roster(acc)], ["a", "b"])

    def test_floor_section_has_no_monthly_price_cutoff(self):
        html = deployment_plan_section({
            "deployment_plan": [
                {"label": "Ceiling", "projects": 3, "monthly": 120},
                {"label": "Floor", "projects": 2, "monthly": 80},
            ],
            "deployment_plan_floor": 2,
            "basis": {"quote_lookback_days": 90},
        }, {})
        self.assertIn("Monthly estimate as deployments become projects", html)
        self.assertIn("The floor is one project per feature per region, 2 projects, and the series stops there.", html)
        self.assertIn("90 days of complete billing", html)
        self.assertNotIn("30 days", html)
        self.assertIn("The cover count and this ceiling are the same deployments.", html)
        self.assertNotIn("$10/mo", html)


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
        missed = assemble_plan_bars(specs, [fills[0], {"error": "G28 is 11 GB/day against 10 written."}, fills[2]])
        self.assertIsNone(missed["bars"][0]["monthly"])
        self.assertEqual(missed["bars"][0]["unpriced_reason"], "G28 is 11 GB/day against 10 written.")
        self.assertEqual(missed["bars"][1]["monthly"], 22)
        self.assertTrue(missed["bars"][0].get("gap"))

    def test_sheet_pool_keeps_input_order(self):
        os.environ["SHEET_WORKERS"] = "4"

        def slow(item):
            if item == 0:
                time.sleep(0.05)
            return item

        self.assertEqual(map_in_order(slow, [0, 1, 2]), [0, 1, 2])

    def test_consolidation_brief_is_the_quote_slice(self):
        acc = _quote_account(g28=10, e73=0)
        acc["analysis"] = {"burndown": {"level": "ok", "series": {"months": []}}}
        acc["platform"] = {"label": "ECH"}
        acc["sizing"]["diagnostics"] = {"inactive_excluded": 2, "snapshot_gb": 0, "k360_fallback": 1}
        acc["sizing"]["estimators"]["observability"]["echo_gate"] = {
            "accepted": True, "written_bulk": 10, "sheet_gb_day": 10.1,
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
        cons["search_power"] = {"profiles": [{"Search Power Setting": "performant", "Total $/month": 12}]}
        acc["consolidation"] = cons
        brief = consolidation_brief(acc)
        self.assertNotIn("burndown", brief)
        self.assertNotIn("platform", brief)
        self.assertEqual(brief["ingest_source"], "diagnostics")
        self.assertEqual(brief["deployment_plan"], [
            {"label": "0%", "monthly": 40, "projects": 4},
            {"label": "100%", "monthly": 100, "projects": 1},
        ])
        # The 30-day read and the retired compression-applied set are not part of the brief.
        self.assertNotIn("thirty_day", brief)
        self.assertNotIn("compressioncalc_accounted_for", brief)
        self.assertNotIn("drive_30d", brief["links"])
        self.assertTrue(brief["estimators"]["observability"]["echo_gate"]["accepted"])
        self.assertEqual(brief["estimators"]["observability"]["cells"]["G21"], 9)
        self.assertNotIn("url", brief["deployment_plan"][0])
        self.assertTrue(brief["metered_rates"][0]["at_half_dollar"])
        self.assertEqual(brief["search_power"]["profiles"][0]["monthly"], 12)
        self.assertIn("score", brief["integrity"])
        self.assertIn("passed", brief["integrity"])
        self.assertIn("failed", brief["integrity"])
        self.assertEqual(brief["diagnostics_coverage"]["inactive_excluded"], 2)

    def test_quote_pdf_jobs_batch_the_finished_scenarios(self):
        account = {"meta": {"run_key": "a"}, "consolidation": {"source": "k360"}}
        diag = {"meta": {"run_key": "a"}, "consolidation": {"source": "diagnostics"}}
        ok = {"meta": {"run_key": "a"}, "consolidation": {"source": "diagnostics"}}
        bad = {"meta": {"run_key": "a"}, "consolidation": {"error": "sheet failed"}}
        jobs = quote_pdf_jobs(account, diag, [
            (bad, "consolidation_compressioncalc"),
            (ok, "consolidation_k360"),
        ])
        self.assertEqual(
            [kind for _doc, kind in jobs],
            ["account", "consolidation", "consolidation_k360", "appendix"],
        )

    def test_every_run_is_stamped_with_its_method(self):
        stamp = method_stamp(90)
        self.assertEqual(stamp["quote_window_days"], 90)
        self.assertEqual(stamp["ech_dollars"], "k360_cost")
        self.assertEqual(stamp["ingest_basis"], "metered diagnostic bulk")
        self.assertEqual(stamp["integrity_pass"], "every check")
        # In this checkout the commit resolves. Outside one both fields are None, never a guess.
        self.assertIn("commit", stamp)
        self.assertIn(stamp["uncommitted_changes"], (True, False, None))
        acc = _quote_account(g28=10, e73=0)
        acc["meta"]["method"] = {"commit": "abc1234", "uncommitted_changes": True}
        acc["consolidation"] = build_consolidation(acc, None)
        self.assertEqual(consolidation_brief(acc)["method"]["commit"], "abc1234")
        self.assertIn("method abc1234 + uncommitted changes", render_consolidation_html(acc))

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
                archive.writestr("diag/nodes_stats.json", "{}")

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


class ConsolidationLayoutTests(unittest.TestCase):
    def test_body_flows_and_the_appendix_is_its_own_document(self):
        acc = _quote_account(g28=10, e73=0)
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
        self.assertIn("class='cover'", html)
        self.assertLess(html.find("class='cover'"), html.find("<section"))
        self.assertEqual(html.count("sec newpage"), html.count("<h2>"))
        self.assertNotIn("<section class='sec'>", html)
        # architecture map: more than four region pairs use two columns
        from render_pdf import architecture_svg
        groups = [{"solution": "search", "region": f"r{i}", "n": 1, "ech_ecu_mo": 1, "stored_gb": 1} for i in range(5)]
        projects = [{"solution": "search", "region": f"r{i}", "stored_gb": 1, "serverless_mo": 10} for i in range(5)]
        svg = architecture_svg(groups, projects)
        self.assertIn("viewBox='0 0 940 ", svg)
        # two columns → three rows for five pairs; height under a one-column layout
        h = float(re.search(r"viewBox='0 0 940 ([0-9.]+)'", svg).group(1))
        self.assertLess(h, 200)
        self.assertIn("<section class='sec'><h2>Appendix A</h2>", appendix)
        self.assertIn("<section class='sec newpage'><h2>Appendix B</h2>", appendix)
        self.assertIn("class='tbl'", html)
        self.assertIn("class='consolidation'", html)
        self.assertLess(appendix.find("Data used, and how the headline is calculated"), appendix.find("Quote integrity"))

    def test_raw_bulk_ingest_section_lists_each_deployment_with_its_calculation(self):
        from render_pdf import raw_ingest_by_deployment, raw_ingest_section
        acc = {
            "ecu": {"quote_usage_factor": 1.25},
            "sizing": {"consolidated": {"observability": {"bulk_gb_day": 125.0, "logs_gb_day": 112.5, "metrics_gb_day": 12.5}}},
            "solutions": {"by_deployment": [
                {"name": "big", "solution": "observability", "daily_raw_gb": 100.0, "gb": 60.0, "retention_days": 10,
                 "uptime_days_min": 30, "ingest_source": "diagnostics"},
                {"name": "fresh", "solution": "observability", "daily_raw_gb": 25.0, "gb": 5.0, "retention_days": 3,
                 "uptime_days_min": 0.5, "ingest_source": "diagnostics"},
                {"name": "nobundle", "solution": "search", "daily_raw_gb": 0.0, "ingest_source": "active_no_bundle"},
                {"name": "a-serverless-project", "solution": "observability", "platform": "serverless"},
            ]},
        }
        data = raw_ingest_by_deployment(acc)
        # The serverless project has no bundle and no ECH bill, so it is not a row and
        # is not counted in the by-solution deployment count.
        self.assertEqual([r["name"] for r in data["rows"]], ["big", "fresh", "nobundle"])
        self.assertEqual(data["by_solution"]["observability"]["n"], 2)
        self.assertEqual(data["by_solution"]["search"]["n"], 1)
        self.assertAlmostEqual(data["rows"][0]["measured"], 80.0)   # 100 ÷ 1.25
        self.assertAlmostEqual(data["total_quoted"], 125.0)
        self.assertAlmostEqual(data["total_measured"], 100.0)
        self.assertAlmostEqual(data["rows"][0]["hot_days"], 60.0 / 80.0)
        html = raw_ingest_section(acc)
        self.assertIn("<h2>Raw bulk ingest by deployment</h2>", html)
        self.assertIn("1.250000", html)
        # The active deployment with no bundle is a row of its own, not only a count.
        self.assertIn("'>nobundle</td>", html)
        self.assertIn("'>no bundle</td>", html)
        self.assertNotIn("a-serverless-project", html)
        self.assertIn("Fleet total 125.0 GB/day", html)
        self.assertIn("background:#FFF8E6", html)   # fresh: node up under 2 days
        self.assertIn("× 1.595", html)
        # A diagnostic run writes bulk as measured: no pace factor, quoted equals measured.
        unscaled = json.loads(json.dumps(acc))
        unscaled["ecu"] = {"quote_usage_factor": 1.25, "quote_ingest_factor": 1.0, "quote_lookback_days": 30}
        flat = raw_ingest_by_deployment(unscaled)
        self.assertIsNone(flat["factor"])
        self.assertAlmostEqual(flat["rows"][0]["measured"], 100.0)
        self.assertAlmostEqual(flat["total_measured"], flat["total_quoted"])
        flat_html = raw_ingest_section(unscaled)
        self.assertIn("The billing window does not scale it.", flat_html)
        self.assertNotIn("1.250000", flat_html)
        comp = json.loads(json.dumps(acc))
        comp["consolidation"] = {"ingest_basis": "compressioncalc_accounted_for"}
        self.assertAlmostEqual(raw_ingest_by_deployment(comp)["total_quoted"], 125.0)
        self.assertNotIn("× 0.60", raw_ingest_section(comp))
        self.assertEqual(raw_ingest_section({"solutions": {"by_deployment": []}}), "")

    def test_appendix_b_prints_formulas_and_skips_an_unpriced_solution(self):
        acc = _quote_account(g28=10, e73=0)
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
        html = render_appendix_html(acc)
        appendix_b = html.split("<h2>Appendix B</h2>", 1)[1]
        self.assertIn("Σ Δsearch_total ÷ 86,400", appendix_b)
        self.assertIn("percent ÷ 100 × 730", appendix_b)
        self.assertNotIn("0.60", appendix_b)
        self.assertIn("primaries.bulk.total_size_in_bytes", appendix_b)
        self.assertIn("primaries.store.size_in_bytes and is not this figure", appendix_b)
        self.assertIn("12.5 QPS", appendix_b)
        self.assertNotIn("Security bulk", appendix_b)
        self.assertNotIn("Security monthly", appendix_b)

    def test_a_30_day_roster_is_the_last_30_billed_days(self):
        days = [f"2026-10-{d:02d}" for d in range(1, 32)] + [f"2026-11-{d:02d}" for d in range(1, 11)]
        total = [1.0] * 40 + [0.0]
        old = [1.0 if i <= 9 else 0.0 for i in range(41)]
        current = [1.0 if value else 0.0 for value in total]
        org = {
            "range": {"start": "2025-11-11"},
            "chart": {
                "grain": "day",
                "days": days,
                "total": total,
                "series_names": ["old-elastic", "new-search", "Other"],
                "ecu_series": [old, current, [0.0] * 41],
            },
            "monthly": {
                "months": ["2026-09", "2026-10", "2026-11"],
                "by_deployment": {
                    "old-elastic": {"ecu": [0, 100, 0]},
                    "new-search": {"ecu": [0, 50, 20]},
                    "october-only": {"ecu": [0, 40, 0]},
                    "september-only": {"ecu": [10, 0, 0]},
                    "november-only": {"ecu": [0, 0, 5]},
                },
            },
        }
        active = active_deployment_set(org, 30)
        self.assertFalse(active["partial"])
        self.assertEqual(active["window_start"], "2026-10-11")
        self.assertEqual(active["window_end"], "2026-11-09")
        self.assertEqual(active["window_days"], 30)
        self.assertEqual(active["names"], ["new-search", "november-only", "october-only"])
        saved = dict(org)
        saved["chart"] = {
            "billed_window": {"start": "2026-10-11", "end": "2026-11-09", "days": 30, "names": ["kept"]},
        }
        exact = active_deployment_set(saved, 30)
        self.assertEqual(exact["names"], ["kept"])
        self.assertFalse(exact["partial"])

    def test_page_one_roster_defaults_to_the_last_90_billed_days(self):
        from datetime import date, timedelta
        first = date(2026, 7, 1)
        days = [(first + timedelta(days=i)).isoformat() for i in range(121)]
        total = [1.0] * 120 + [0.0]                       # the last day has no bill and is not in the window
        early = [1.0 if i < 20 else 0.0 for i in range(121)]     # stopped 100 days before the end
        recent = [1.0 if 40 <= i < 60 else 0.0 for i in range(121)]  # billed 60 to 80 days back
        steady = [1.0 if i < 120 else 0.0 for i in range(121)]
        org = {
            "range": {"start": "2025-11-11"},
            "chart": {"grain": "day", "days": days, "total": total,
                      "series_names": ["early", "recent", "steady", "Other"],
                      "ecu_series": [early, recent, steady, [0.0] * 121]},
            "monthly": {"months": ["2026-07", "2026-08", "2026-09", "2026-10"],
                        "by_deployment": {"early": {"ecu": [20, 0, 0, 0]}, "recent": {"ecu": [0, 20, 0, 0]},
                                          "steady": {"ecu": [31, 31, 30, 28]}}},
        }
        active = active_deployment_set(org)
        self.assertEqual(active["window_days"], 90)
        self.assertEqual(active["window_end"], days[119])
        self.assertEqual(active["window_start"], days[30])
        # "recent" has no bill in the last 30 days and is on the 90-day roster. "early" is off both.
        self.assertEqual(active["names"], ["recent", "steady"])
        self.assertEqual(active_deployment_set(org, 30)["names"], ["steady"])
        # A roster saved for 90 days is used as is. One saved only for 30 days is not stretched to 90.
        saved = dict(org)
        saved["chart"] = dict(org["chart"], billed_windows={"90": {"start": days[30], "end": days[119], "days": 90, "names": ["kept"]}})
        self.assertEqual(active_deployment_set(saved)["names"], ["kept"])
        legacy = dict(org)
        legacy["chart"] = dict(org["chart"], billed_window={"start": days[90], "end": days[119], "days": 30, "names": ["thirty-only"]})
        self.assertEqual(active_deployment_set(legacy)["names"], ["recent", "steady"])
        self.assertEqual(active_deployment_set(legacy, 30)["names"], ["thirty-only"])

    def test_quote_window_defaults_to_90_days(self):
        acc = _quote_account(g28=10, e73=0)
        acc["ecu"]["daily_totals"] = [1.0] * 275 + [0.5] * 90 + [0.0]
        apply_quote_lookback(acc)
        self.assertEqual(acc["ecu"]["quote_lookback_days"], 90)
        self.assertAlmostEqual(acc["ecu"]["quote_monthly_ecu"], round(0.5 * 30.416, 2))

    def test_k360_backup_ingest_still_takes_the_billing_pace(self):
        acc = _quote_account(g28=10, e73=0)
        acc["sizing"]["ingest_source"] = "k360"
        acc["ecu"]["daily_totals"] = [1.0] * 335 + [0.2] * 30 + [0.0]
        factor = apply_quote_lookback(acc, 30)
        expected = 0.2 / (10 / 30.416)
        self.assertAlmostEqual(acc["ecu"]["quote_ingest_factor"], factor, places=6)
        self.assertAlmostEqual(
            acc["sizing"]["consolidated"]["observability"]["daily_raw_gb"], round(10 * expected, 3), places=3,
        )
        apply_quote_lookback(acc, 30)
        self.assertAlmostEqual(
            acc["sizing"]["consolidated"]["observability"]["daily_raw_gb"], round(10 * expected, 3), places=3,
        )

    def test_default_lookback_prices_ech_at_30_days_and_leaves_measured_bulk_alone(self):
        acc = _quote_account(g28=10, e73=0)
        acc["ecu"]["last_30d_daily_avg"] = 0.2
        acc["ecu"]["daily_totals"] = [1.0] * 335 + [0.2] * 30 + [0.0]
        acc["workload"] = {"ingest": {"docs_per_sec": 100.0}}
        acc["solutions"]["by_deployment"] = [{"name": "d", "daily_raw_gb": 10.0, "compressioncalc_accounted_for": 6.0}]
        factor = apply_quote_lookback(acc, 30)
        expected = 0.2 / (10 / 30.416)
        # The billing pace is still the ECH factor. It no longer touches measured bulk.
        self.assertAlmostEqual(factor, expected, places=4)
        self.assertEqual(acc["ecu"]["quote_ingest_factor"], 1.0)
        self.assertEqual(acc["sizing"]["consolidated"]["observability"]["daily_raw_gb"], 10)
        self.assertEqual(acc["sizing"]["consolidated"]["security"]["daily_raw_gb"], 4)
        self.assertEqual(acc["solutions"]["by_deployment"][0]["daily_raw_gb"], 10.0)
        self.assertEqual(acc["solutions"]["by_deployment"][0]["compressioncalc_accounted_for"], 6.0)
        # Billing pace scales ECH only. Search docs/s stays the measured rate.
        self.assertEqual(acc["workload"]["ingest"]["docs_per_sec"], 100)
        again = apply_quote_lookback(acc, 30)
        self.assertAlmostEqual(again, factor, places=4)
        self.assertEqual(acc["sizing"]["consolidated"]["observability"]["daily_raw_gb"], 10)
        self.assertEqual(acc["workload"]["ingest"]["docs_per_sec"], 100)
        with self.assertRaises(ValueError):
            apply_quote_lookback(acc, 366)
        acc["consolidation"] = build_consolidation(acc, None)
        self.assertAlmostEqual(acc["consolidation"]["basis"]["ech_monthly_ecu"], round(0.2 * 30.416, 2))
        html = render_consolidation_html(acc)
        header = html.split("</header>", 1)[0]
        self.assertIn("30-day lookback", header)
        self.assertNotIn("complete-month", header)
        self.assertIn("Each is 30 days of complete billing.", html)
        self.assertIn("30-day lookback", html)
        self.assertIn("ECH billing pace applied to the estimate (illustrative)", html)
        self.assertIn("Last 30 days of billed usage", html)
        self.assertIn("ECH, last 30 billed days", html)
        self.assertIn("Serverless, last 30 billed days", html)
        build = html.split("What you pay for today", 1)[1].split("<h2>", 1)[0]
        self.assertIn("Full lookback", build)
        self.assertNotIn("ECH today", build)

    def test_cover_flips_reduction_to_increase_when_serverless_costs_more(self):
        acc = _quote_account(g28=10, e73=0)
        _stamp_reconciled_billing(acc)
        acc["sizing"]["quote_options"] = {"ech_scope": "all", "keep_serverless_spend": True, "min_priced_ecu_pct": 90, "unmeasured": "exclude"}
        acc["consolidation"] = build_consolidation(acc, None)
        html = render_consolidation_html(acc)
        cover = html.split("<section class='sec newpage'>", 1)[0]
        sv = acc["consolidation"]["savings"]
        self.assertLess(sv["list_mo"], 0)
        self.assertIn("estimated cost increase from", cover)
        self.assertIn("Estimated monthly increase versus ECH billed cost", cover)
        self.assertIn("Estimated increase versus ECH billed cost", cover)
        self.assertNotIn("Estimated monthly reduction", cover)
        self.assertIn("class='up'", cover)
        self.assertNotIn("class='down'", cover.split("<style>")[-1].split("</style>")[-1])
        self.assertNotIn("-$", cover.split("</style>")[-1])

    def test_cover_keeps_reduction_when_serverless_costs_less(self):
        acc = _quote_account(g28=10, e73=0)
        _stamp_reconciled_billing(acc)
        acc["sizing"]["quote_options"] = {"ech_scope": "all", "keep_serverless_spend": True, "min_priced_ecu_pct": 90, "unmeasured": "exclude"}
        acc["consolidation"] = build_consolidation(acc, None)
        sv = acc["consolidation"]["savings"]
        sv["list_mo"], sv["list_pct"] = 50.0, 20.0
        cover = render_consolidation_html(acc).split("<section class='sec newpage'>", 1)[0]
        self.assertIn("estimated savings from", cover)
        self.assertIn("Estimated monthly reduction versus ECH billed cost", cover)
        self.assertIn("class='down'", cover.split("</style>")[-1])
        self.assertNotIn("class='up'", cover.split("</style>")[-1])

    def test_ech_dollars_are_the_k360_billed_cost_not_an_arr_rate(self):
        # ARR 1825 over a commit of 10 ECU/day is $0.50 per ECU. It must not touch the ECH dollars.
        acc = _quote_account(g28=10, e73=0)
        _stamp_reconciled_billing(acc)
        acc["sizing"]["quote_options"] = {"ech_scope": "all", "keep_serverless_spend": True, "min_priced_ecu_pct": 90, "unmeasured": "exclude"}
        acc["meta"]["org"]["ARR"] = 1825
        acc["ecu"]["contract"] = {"commit_pace_ecu_per_day": 10}
        cons = acc["consolidation"] = build_consolidation(acc, None)
        sv = cons["savings"]
        self.assertEqual(sv["ech_basis"], "k360_cost")
        self.assertEqual(sv["ech_list_mo"], cons["basis"]["ech_monthly_ecu"])
        self.assertAlmostEqual(sv["list_mo"], sv["ech_list_mo"] - sv["serverless_mo"], places=2)
        for key in ("ech_contract_mo", "cost_mo", "cost_yr", "cost_pct", "below_list_mo"):
            self.assertNotIn(key, sv)
        self.assertIsNone(cons["contract_rate"]["implied_usd_per_ecu"])
        self.assertEqual(cons["contract_rate"]["basis"], "k360_cost")
        self.assertTrue(all("ech_cost_mo" not in row for row in cons["by_deployment"]))
        self.assertTrue(all("ech_cost_mo" not in row and "save_vs_contract_mo" not in row for row in cons["sensitivity"]["fleet"]))
        html = render_consolidation_html(acc)
        self.assertIn("ECH billed cost", html)
        for gone in ("ECH at contract rate", "Contract rate $", "at the contract rate", "Savings vs. contract rate", "ECH at list", "$1 per ECU"):
            self.assertNotIn(gone, html)
        brief = consolidation_brief(acc)
        self.assertEqual(brief["savings"]["ech_list_mo"], sv["ech_list_mo"])
        self.assertEqual(brief["savings"]["list_mo"], sv["list_mo"])
        self.assertEqual(brief["savings"]["ech_basis"], "k360_cost")

    def test_cover_stays_on_the_complete_month_average_without_a_30_day_rate(self):
        acc = _quote_account(g28=10, e73=0)
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


class PerClusterPlanTests(unittest.TestCase):
    def _sz(self):
        def bar(label, groups):
            return {"label": label, "projects": len(groups), "monthly": sum(g["monthly"] for g in groups), "groups": groups}
        return {"deployment_plan": {"bars": [
            bar("Ceiling", [{"names": ["a"], "monthly": 90.0}, {"names": ["b"], "monthly": 9.0}, {"names": ["c"], "monthly": 1.0}]),
            bar("Floor", [{"names": ["a", "b", "c"], "monthly": 80.0}]),
        ]}}

    def _acc(self):
        return {"solutions": {"by_deployment": [
            {"name": "a", "solution": "observability", "daily_raw_gb": 80},
            {"name": "b", "solution": "observability", "daily_raw_gb": 15},
            {"name": "c", "solution": "observability", "daily_raw_gb": 5},
        ]}, "cloud": {"by_deployment": [{"name": "a", "region": "eastus"}]}}

    def test_each_cluster_is_priced_alone_then_packed_to_the_floor(self):
        from consolidation import per_cluster_plan
        plan = per_cluster_plan(self._sz(), self._acc(), None)
        rows = {r["name"]: r for r in plan["rows"]}
        self.assertEqual([r["name"] for r in plan["rows"]], ["a", "b", "c"])
        self.assertEqual(rows["a"]["steps"][0]["monthly"], 90.0)
        self.assertEqual(rows["a"]["steps"][1]["monthly"], 64.0)
        self.assertEqual(rows["b"]["steps"][1]["pack"], 3)
        for i, step in enumerate(plan["steps"]):
            self.assertAlmostEqual(sum(r["steps"][i]["monthly"] for r in plan["rows"]), step["monthly"], places=1)

    def test_zero_bulk_with_a_bundle_is_not_sized_and_no_bundle_is_flagged(self):
        from consolidation import per_cluster_plan
        from render_pdf import per_cluster_section
        acc = self._acc()
        for d in acc["solutions"]["by_deployment"]:
            d["ingest_source"] = "diagnostics"
        acc["solutions"]["by_deployment"][1].update({"daily_raw_gb": 0, "ingest_source": "diagnostics"})
        acc["solutions"]["by_deployment"][2].update({"daily_raw_gb": 0, "ingest_source": "active_no_bundle"})
        plan = per_cluster_plan(self._sz(), acc, None)
        rows = {r["name"]: r for r in plan["rows"]}
        self.assertEqual((rows["b"]["status"], rows["c"]["status"]), ("no_bulk", "missing_diag"))
        self.assertEqual(rows["a"]["steps"][1]["monthly"], 80.0)
        self.assertIsNone(rows["b"]["steps"][1]["monthly"])
        self.assertEqual([r["name"] for r in plan["rows"]][0], "a")
        html = per_cluster_section({"per_cluster_plan": plan, "deployment_plan": [1]})
        self.assertIn("not sized", html)
        self.assertIn("missing diagnostic", html)

    def test_a_long_fleet_folds_its_smallest_clusters_into_one_row(self):
        from render_pdf import per_cluster_section, PER_CLUSTER_MAX_ROWS
        rows = [{"name": f"c{i}", "solution": "search", "region": "r", "bulk_gb_day": 1,
                 "steps": [{"monthly": 100.0 - i, "pack": 1}]} for i in range(PER_CLUSTER_MAX_ROWS + 10)]
        plan = {"steps": [{"label": "Ceiling", "projects": len(rows), "monthly": 1}], "rows": rows}
        html = per_cluster_section({"per_cluster_plan": plan, "deployment_plan": [1]})
        self.assertIn("11 smaller clusters", html)
        self.assertNotIn(">c45<", html)

    def test_unsized_clusters_are_never_folded_into_a_zero_dollar_row(self):
        from render_pdf import per_cluster_section, PER_CLUSTER_MAX_ROWS
        sized = [{"name": f"c{i}", "solution": "observability", "region": "r", "bulk_gb_day": 1, "status": "sized",
                  "steps": [{"monthly": 100.0 - i, "pack": 1}]} for i in range(PER_CLUSTER_MAX_ROWS)]
        missing = [{"name": f"m{i}", "solution": "observability", "region": "r", "bulk_gb_day": 0, "status": "missing_diag",
                    "steps": [{"monthly": None, "pack": 1}]} for i in range(20)]
        plan = {"steps": [{"label": "Ceiling", "projects": len(sized) + 20, "monthly": 1}], "rows": sized + missing}
        html = per_cluster_section({"per_cluster_plan": plan, "deployment_plan": [1]})
        self.assertIn("smaller clusters", html)
        self.assertIn("more clusters not sized", html)
        self.assertIn("Not sized and not in the table:", html)
        self.assertIn("m19 (missing diagnostic)", html)

    def test_a_search_project_is_shared_by_stored_gb(self):
        from consolidation import per_cluster_plan
        acc = {"solutions": {"by_deployment": [
            {"name": "big", "solution": "search", "gb": 900, "daily_raw_gb": 0, "ingest_source": "diagnostics"},
            {"name": "small", "solution": "search", "gb": 100, "daily_raw_gb": 5, "ingest_source": "diagnostics"},
            {"name": "empty", "solution": "search", "gb": 0, "daily_raw_gb": 0, "ingest_source": "diagnostics"},
        ]}}
        sz = {"deployment_plan": {"bars": [
            {"label": "Floor", "projects": 1, "monthly": 1000, "groups": [{"names": ["big", "small", "empty"], "monthly": 1000}]},
        ]}}
        plan = per_cluster_plan(sz, acc, None)
        rows = {r["name"]: r for r in plan["rows"]}
        # The cluster with no write-index bulk holds 90% of the store and carries 90% of the project.
        self.assertEqual(rows["big"]["steps"][0]["monthly"], 900.0)
        self.assertEqual(rows["small"]["steps"][0]["monthly"], 100.0)
        self.assertEqual((rows["big"]["status"], rows["big"]["share_basis"]), ("sized", "stored GB"))
        self.assertEqual(rows["empty"]["status"], "no_bulk")
        self.assertEqual(plan["unallocated"], [0.0])

    def test_no_plan_when_the_bars_have_no_groups(self):
        from consolidation import per_cluster_plan
        sz = self._sz()
        sz["deployment_plan"]["bars"][0].pop("groups")
        self.assertIsNone(per_cluster_plan(sz, self._acc(), None))

    def test_page_lists_clusters_with_grey_rows_and_totals(self):
        from consolidation import per_cluster_plan
        from render_pdf import per_cluster_section
        plan = per_cluster_plan(self._sz(), self._acc(), None)
        html = per_cluster_section({"per_cluster_plan": plan, "deployment_plan": [1]})
        self.assertIn("Serverless cost per cluster", html)
        self.assertIn("Floor, 1 project", html)
        self.assertIn("(of 3)", html)
        self.assertEqual(html.count("background:#EEF0F3"), 0)
        self.assertIn("Total", html)
        self.assertEqual(per_cluster_section({"per_cluster_plan": plan}), "")


class EchScopeTests(unittest.TestCase):
    """Which billed cost is the ECH side of the comparison, and how much of it has a serverless price."""

    def _account(self, **options) -> dict:
        return scope_account(**options)

    def test_priced_scope_compares_only_deployments_with_a_serverless_price(self):
        acc = self._account(ech_scope="priced", keep_serverless_spend=False, min_priced_ecu_pct=90)
        cons = build_consolidation(acc, None)
        scope = cons["ech_scope"]
        month = 30.416
        self.assertAlmostEqual(scope["window_mo"], 10 * month, places=1)
        self.assertAlmostEqual(scope["priced_mo"], 6 * month, places=1)            # o11y 5 + sec 1
        self.assertAlmostEqual(scope["unpriced_mo"], 2 * month, places=1)
        self.assertAlmostEqual(scope["serverless_mo"], 1 * month, places=1)
        self.assertAlmostEqual(scope["other_mo"], 1 * month, places=1)             # off-roster 0.5 + unnamed 0.5
        self.assertAlmostEqual(scope["in_comparison_mo"], 6 * month, places=1)
        self.assertEqual([row["name"] for row in scope["unpriced"]], ["nobundle"])
        self.assertEqual(scope["unpriced"][0]["reason"], "no diagnostic bundle")
        self.assertEqual((scope["priced_deployments"], scope["unpriced_deployments"]), (2, 1))
        self.assertAlmostEqual(scope["priced_share_pct"], 75.0, places=1)          # 6 of the 8 a day that ECH deployments bill
        sv = cons["savings"]
        self.assertAlmostEqual(sv["ech_list_mo"], 6 * month, places=1)
        self.assertAlmostEqual(sv["list_mo"], sv["ech_list_mo"] - sv["serverless_mo"], places=2)
        self.assertAlmostEqual(cons["basis"]["ech_window_ecu"], 10 * month, places=1)
        # 75% is under the 90% bar: the check fails, and one failed check fails the quote.
        row = next(r for r in cons["integrity"]["checks"] if r["check"] == "Priced share")
        self.assertEqual(row["score"], 0.75)
        self.assertFalse(row["passed"])
        self.assertFalse(cons["integrity"]["passed"])
        html = render_consolidation_html({**acc, "consolidation": cons})
        self.assertIn("2 deployments that got a serverless price", html)
        self.assertIn("Not compared:", html)
        self.assertIn("nobundle (no diagnostic bundle", html)
        self.assertEqual(consolidation_brief({**acc, "consolidation": cons})["ech_scope"]["scope"], "priced")

    def test_all_scope_drops_existing_serverless_spend_unless_it_is_kept(self):
        month = 30.416
        dropped = build_consolidation(self._account(ech_scope="all", keep_serverless_spend=False), None)
        self.assertAlmostEqual(dropped["savings"]["ech_list_mo"], 9 * month, places=1)
        self.assertIn("so the saving is overstated by their bill", " ".join(dropped["notes"]))
        kept = build_consolidation(self._account(ech_scope="all", keep_serverless_spend=True), None)
        self.assertAlmostEqual(kept["savings"]["ech_list_mo"], 10 * month, places=1)
        # An account with no options is one written before they existed: the whole window, as it was priced then.
        legacy = build_consolidation(self._account(), None)
        self.assertAlmostEqual(legacy["savings"]["ech_list_mo"], 10 * month, places=1)
        self.assertEqual(legacy["ech_scope"]["scope"], "all")

    def test_the_priced_share_bar_is_a_parameter(self):
        loose = build_consolidation(self._account(ech_scope="priced", min_priced_ecu_pct=70), None)
        row = next(r for r in loose["integrity"]["checks"] if r["check"] == "Priced share")
        self.assertTrue(row["passed"])
        self.assertIn("The bar is 70%", row["detail"])

    def test_a_rejected_solution_is_not_priced(self):
        acc = self._account(ech_scope="priced")
        acc["sizing"]["estimators"]["security"]["readback"]["cps_addon_monthly"] = 25      # E73 blocks Security
        cons = build_consolidation(acc, None)
        reasons = {row["name"]: row["reason"] for row in cons["ech_scope"]["unpriced"]}
        self.assertEqual(reasons["sec"], "Security has no accepted estimator read-back")
        self.assertAlmostEqual(cons["ech_scope"]["priced_mo"], 5 * 30.416, places=1)

    def test_no_scope_without_per_deployment_months(self):
        acc = _quote_account(g28=10, e73=0)
        cons = build_consolidation(acc, None)
        self.assertEqual(cons["ech_scope"]["status"], "unavailable")
        row = next(r for r in cons["integrity"]["checks"] if r["check"] == "Priced share")
        self.assertFalse(row["passed"])

    def test_flag_defaults(self):
        import run_account_analysis as ra
        from run_consolidation import quote_options_from
        args = ra.parse_args(["--org", "X"])
        self.assertEqual(quote_options_from(args), {
            "ech_scope": "priced", "unmeasured": "exclude", "min_priced_ecu_pct": 90.0, "keep_serverless_spend": False,
        })
        self.assertEqual(args.lookback_days, 90)
        with self.assertRaises(SystemExit):
            ra.parse_args(["--org", "X", "--unmeasured", "guess"])


class ExcludeDeploymentTests(unittest.TestCase):
    def _acc(self):
        return {
            "meta": {"run_key": "Acme_2026-11-12"},
            "ecu": {
                "monthly": {"months": ["2026-09", "2026-10", "2026-11"], "totals": [100.0, 100.0, 50.0], "partial_months": ["2026-11"]},
                "monthly_avg_complete_months": 100.0, "monthly_avg_all": 83.33, "total_lookback": 250.0, "total_lookback_ech_named": 250.0,
                "daily_days": ["2026-10-30", "2026-10-31", "2026-11-01"], "daily_totals": [4.0, 4.0, 2.0],
                "daily_avg": 3.33, "daily_avg_active_days": 3.33, "last_30d_total": 10.0, "last_30d_daily_avg": 3.33,
                "monthly_peak": {"month": "2026-09", "ecu": 100.0}, "monthly_low": {"month": "2026-11", "ecu": 50.0},
                "daily_peak": {"day": "2026-10-30", "ecu": 4.0},
            },
            "deployments": {"count": 3, "ech_count": 3, "top": [{"name": "keep"}, {"name": "drop"}], "active": {"names": ["keep", "drop"], "count": 2}},
            "solutions": {"by_deployment": [{"name": "keep", "platform": "ech"}, {"name": "drop", "platform": "ech"}]},
            "storage": {"by_deployment": [{"name": "keep"}, {"name": "drop"}], "size_gaps": [{"name": "drop"}]},
        }

    def test_ids_must_be_deployment_ids(self):
        from exclusions import parse_ids
        good = "A" * 32
        self.assertEqual(parse_ids([good, good.lower() + "," + "b" * 32]), ["a" * 32, "b" * 32])
        with self.assertRaises(SystemExit):
            parse_ids(["not-an-id"])

    def test_unknown_id_stops_the_run(self):
        from exclusions import resolve_names
        with self.assertRaises(SystemExit):
            resolve_names(["a" * 32], {"b" * 32: "other"})
        self.assertEqual(resolve_names(["a" * 32], {"a" * 32: "drop"}), [{"id": "a" * 32, "name": "drop"}])

    def test_excluded_deployment_leaves_billing_roster_and_counts(self):
        from exclusions import apply_exclusions
        acc = self._acc()
        org = {"monthly": {"by_deployment": {"drop": {"months": ["2026-09", "2026-10", "2026-11"], "ecu": [40.0, 25.0, 10.0]}}}}
        rec = apply_exclusions(acc, org, [{"id": "a" * 32, "name": "drop"}])
        e = acc["ecu"]
        self.assertEqual(e["monthly"]["totals"], [60.0, 75.0, 40.0])
        self.assertEqual(rec["ecu_removed_over_lookback"], 75.0)
        self.assertEqual(e["daily_totals"], [4.0, 4.0, 2.0])
        self.assertEqual(e["monthly_avg_complete_months"], 67.5)
        self.assertEqual(acc["deployments"]["count"], 2)
        self.assertEqual(acc["deployments"]["active"]["names"], ["keep"])
        self.assertEqual([d["name"] for d in acc["solutions"]["by_deployment"]], ["keep"])
        self.assertEqual(acc["storage"]["size_gaps"], [])
        self.assertEqual(acc["meta"]["excluded_deployments"]["deployments"][0]["name"], "drop")

    def test_diagnostics_rows_of_excluded_ids_are_not_parsed(self):
        import admin_diags
        self.assertIn("exclude_ids", admin_diags.parse_diagnostics.__code__.co_varnames)
        self.assertIn("exclude_ids", admin_diags.pull_diagnostics.__code__.co_varnames)

    def test_cover_names_excluded_deployments(self):
        from render_pdf import _excluded_notes
        self.assertEqual(_excluded_notes(None), [])
        note = _excluded_notes({"deployments": [{"id": "a" * 32, "name": "drop"}]})[0]
        self.assertIn("drop", note)
        self.assertIn("Excluded by id", note)


class MonitoringExclusionTests(unittest.TestCase):
    def test_monitoring_is_a_name_part(self):
        from exclusions import is_monitoring_name, monitoring_names
        for name in ("monitoring", "prod-monitoring", "ES_Monitoring_01", "stack monitoring"):
            self.assertTrue(is_monitoring_name(name), name)
        for name in ("monitor", "monitoringx", "demonitoring", "search-prod", "", None):
            self.assertFalse(is_monitoring_name(name), name)
        self.assertEqual(monitoring_names(["a", "monitoring", "monitoring", "b-monitoring"]), ["monitoring", "b-monitoring"])

    def test_ids_follow_names_and_report_the_unresolved(self):
        from exclusions import ids_for_names
        ids, missing = ids_for_names(["m1", "m2", "gone"], {"A" * 32: "m1", "b" * 32: "m1", "c" * 32: "m2", "d" * 32: "other"})
        self.assertEqual(ids, ["a" * 32, "b" * 32, "c" * 32])
        self.assertEqual(missing, ["gone"])

    def test_auto_exclusion_is_consolidation_only_and_can_be_kept(self):
        import run_account_analysis as ra
        acc = {"solutions": {"by_deployment": [{"name": "monitoring", "platform": "ech"}, {"name": "prod", "platform": "ech"}]}}
        args = type("A", (), {"diags_dir": None, "org_id": None, "exclude_ids": None})()
        with mock.patch.object(ra, "load_id_to_name", return_value={"a" * 32: "monitoring"}):
            self.assertEqual(ra.auto_exclude_monitoring(args, {}, {}, acc), [])           # account summary: off
            args.auto_exclude_monitoring = True
            self.assertEqual(ra.auto_exclude_monitoring(args, {}, {}, acc), ["monitoring"])
            self.assertEqual(args.exclude_ids, ["a" * 32])
        keep = ra.parse_args(["--org", "X", "--keep-monitoring"])
        self.assertTrue(keep.keep_monitoring)


class OverheadBucketTests(unittest.TestCase):
    def test_buckets_do_not_overlap_when_the_tail_line_is_low(self):
        """A 1% line under 5k used to put the last two buckets on top of the first two."""
        acc = _quote_account(g28=10, e73=0)
        acc["ecu"]["total_lookback"] = 200_000          # 1% = 2,000, between the 1k and 5k edges
        acc["solutions"]["by_deployment"] = [
            {"name": f"d{i}", "solution": "observability", "platform": "ech", "ecu": ecu, "gb": 1}
            for i, ecu in enumerate([500, 1_500, 3_000, 9_000, 60_000])
        ]
        buckets = build_consolidation(acc, None)["overhead_buckets"]
        counted = sum(b["deployments"] for b in buckets)
        self.assertEqual(counted, 5, f"deployments counted more than once: {buckets}")
        edges = [b["bucket"] for b in buckets]
        self.assertEqual(len(edges), len(set(edges)))


if __name__ == "__main__":
    unittest.main()
