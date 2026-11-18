"""Integrity tests for the diagnostic quote. No Google and no sheets."""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(HERE))
from skill_paths import find_skill, scripts_dir  # noqa: E402

K360 = scripts_dir(find_skill("k360-account-analysis", HERE))
sys.path.insert(0, str(K360))

from analyze import active_deployment_set  # noqa: E402
from consolidation import apply_quote_lookback, build_consolidation  # noqa: E402
from diag_account import apply_diagnostics  # noqa: E402
from run_consolidation import _apply_diagnostic_report_view, method_stamp  # noqa: E402
from render_pdf import consolidation_pdf_stem, render_consolidation_html  # noqa: E402
from size_serverless import estimate_title  # noqa: E402

from _test_support import (  # noqa: E402
    HERE,
    K360,
    _quote_account,
    _stamp_reconciled_billing,
    _stamp_search_eis,
    scope_account,
)


class EstimatorMonotoneTests(unittest.TestCase):
    def test_a_rising_step_fails_the_quote_and_leaves_the_readback(self):
        from quote_checks import _plan_row
        flat = [{"label": "Ceiling", "projects": 8, "monthly": 1000}, {"label": "-25%", "projects": 6, "monthly": 900}, {"label": "Floor", "projects": 1, "monthly": 700}]
        row = _plan_row(flat)
        self.assertEqual((row["score"], row["passed"]), (1.0, True))
        rising = [{"label": "Ceiling", "projects": 8, "monthly": 1000}, {"label": "-25%", "projects": 3, "monthly": 700}, {"label": "Floor", "projects": 2, "monthly": 900}]
        row = _plan_row(rising)
        self.assertFalse(row["passed"])
        self.assertEqual(row["score"], 0.5)
        self.assertIn("Floor (2 projects) $900 is above -25% (3 projects) $700", row["detail"])
        # Small rises inside 2% plus $5 are rounding.
        self.assertTrue(_plan_row([{"label": "a", "projects": 2, "monthly": 100}, {"label": "b", "projects": 1, "monthly": 106}])["passed"])
        self.assertIsNone(_plan_row([{"label": "a", "projects": 1, "monthly": 100}])["score"])

    def test_the_check_is_in_the_integrity_score(self):
        acc = _quote_account(g28=10, e73=0)
        _stamp_reconciled_billing(acc)
        acc["sizing"]["quote_options"] = {"ech_scope": "all", "keep_serverless_spend": True, "min_priced_ecu_pct": 90, "unmeasured": "exclude"}
        acc["sizing"]["deployment_plan"] = {"bars": [
            {"label": "Ceiling", "projects": 4, "monthly": 100},
            {"label": "Floor", "projects": 1, "monthly": 400},
        ]}
        cons = build_consolidation(acc, None)
        row = next(r for r in cons["integrity"]["checks"] if r["check"] == "Estimator monotone")
        self.assertFalse(row["passed"])
        self.assertFalse(cons["integrity"]["passed"])
        self.assertEqual(cons["savings"]["serverless_mo"], 180)     # the read-back is still the dollar


class RegionAliasTests(unittest.TestCase):
    """An alias may respell a region name. It may not move the place or the cloud."""

    def test_a_respelled_name_is_the_same_region(self):
        from quote_checks import _alias_same_number
        for region, recorded in (
            ("us-east-1", "AWS: us-east-1 (N. Virginia) "),
            ("us-east1", "GCP: us-east-1 (S. Carolina) "),
            ("useast1", "AZURE: us-east-1 (Virginia) "),
        ):
            self.assertTrue(_alias_same_number(region, recorded), f"{region} vs {recorded}")

    def test_a_different_place_with_the_same_number_is_not_an_alias(self):
        from quote_checks import _alias_same_number
        self.assertFalse(_alias_same_number("ap-southeast-1", "AWS: ap-northeast-1 (Tokyo) "))
        self.assertFalse(_alias_same_number("eu-west-1", "AWS: us-west-1 "))

    def test_a_different_number_is_not_an_alias(self):
        from quote_checks import _alias_same_number
        self.assertFalse(_alias_same_number("ap-southeast-2", "AWS: ap-southeast-1 (Singapore) "))

    def test_a_sheet_on_another_cloud_is_flagged_first(self):
        from quote_checks import _region_fidelity_row
        row = _region_fidelity_row([{
            "solution": "security", "region": "us-east-2", "provider": "aws",
            "region_match": "AZURE: us-east-2 (Virginia) (alias)",
        }])
        self.assertTrue(row["passed"])       # a mismatch flags, it does not withhold the quote
        self.assertTrue(row["warning"])
        self.assertIn("another cloud", row["detail"])

    def test_the_same_cloud_passes(self):
        from quote_checks import _region_fidelity_row
        row = _region_fidelity_row([{
            "solution": "security", "region": "us-east-2", "provider": "aws",
            "region_match": "AWS: us-east-2 (Ohio) (alias)",
        }])
        self.assertTrue(row["passed"])


class FixtureCrossCheckTests(unittest.TestCase):
    def _run(self, name):
        from fixture_loader import load_case
        account, infra = load_case(name)
        cons = build_consolidation(account, infra)
        account["consolidation"] = cons
        html = render_consolidation_html(account)
        return cons, html

    def test_fixtures_render_offline(self):
        from fixture_loader import CASES, load_case
        for name in CASES:
            account, infra = load_case(name)
            cons = build_consolidation(account, infra)
            account["consolidation"] = cons
            html = render_consolidation_html(account)
            self.assertIn("<html", html.lower())
            self.assertNotIn("flatiron", html.lower())
            self.assertNotIn("millipore", html.lower())
            self.assertNotIn("pepsi", html.lower())

    def test_floor_matches_the_headline_when_the_floor_is_one_project_per_region(self):
        from fixture_loader import load_case
        for name in ("ref-a", "ref-b", "ref-c"):
            account, infra = load_case(name)
            cons = build_consolidation(account, infra)
            plan = (account.get("sizing") or {}).get("deployment_plan") or {}
            if not isinstance(plan, dict):
                continue
            bars = plan.get("bars") or []
            floor = next((bar for bar in bars if isinstance(bar, dict) and bar.get("label") == "Floor"), None)
            if floor is None or floor.get("monthly") is None or floor.get("projects") is None:
                continue
            headline = (cons.get("savings") or {}).get("serverless_mo")
            if headline is None:
                continue
            regional = plan.get("floor")
            if regional is not None and int(floor["projects"]) != int(regional):
                continue
            gap = abs(float(floor["monthly"]) - float(headline))
            self.assertLessEqual(gap, abs(float(headline)) * 0.01 + 5, name)

    def test_quoted_regions_are_exact_or_alias_on_the_same_provider(self):
        for name in ("ref-a", "ref-b", "ref-c"):
            cons, _html = self._run(name)
            for row in cons["integrity"]["checks"]:
                if row["check"] != "Region fidelity":
                    continue
                # Never a failure. A substitute region is named instead.
                self.assertIn(row["passed"], (True, None))
                if "provider_default" in (row.get("detail") or ""):
                    self.assertTrue(row.get("warning"), name)

    def test_page_one_ech_and_serverless_cover_the_same_deployments(self):
        from consolidation import unpriced_reason
        from fixture_loader import load_case
        for name in ("ref-a", "ref-b", "ref-c"):
            account, _infra = load_case(name)
            cons, _html = self._run(name)
            priced = cons.get("ech_recent_months_priced") or {}
            self.assertEqual(priced.get("basis"), "monthly_by_deployment")
            by_solution = (cons.get("serverless") or {}).get("by_solution") or {}
            regions = {}
            for dep in ((account.get("cloud") or {}).get("by_deployment") or []):
                if isinstance(dep, dict) and dep.get("name"):
                    regions[str(dep["name"])] = dep.get("region")
            diagnostic = (account.get("sizing") or {}).get("ingest_source") == "diagnostics"
            for dep in (account.get("solutions") or {}).get("by_deployment") or []:
                if not isinstance(dep, dict) or not dep.get("name") or dep.get("platform") == "serverless":
                    continue
                if unpriced_reason(dep, by_solution, diagnostic) is not None:
                    continue
                blk = by_solution.get(dep.get("solution") or "search") or {}
                self.assertIn("quotable", str(blk.get("pricing") or ""), dep["name"])
                rows = blk.get("by_region") or []
                if not rows:
                    continue
                quoted_regions = {row.get("region") for row in rows if row.get("monthly") is not None}
                self.assertIn(regions.get(str(dep["name"])), quoted_regions, dep["name"])
            for sol, blk in by_solution.items():
                rows = blk.get("by_region") or []
                if any(str(row.get("pricing") or "").startswith("rejected") or row.get("monthly") is None and "not quoted" in str(row.get("pricing") or "") for row in rows):
                    self.assertNotIn("quotable", str(blk.get("pricing") or ""), sol)

    def test_an_unverified_comparison_is_not_called_a_saving(self):
        for name in ("ref-a", "ref-b", "ref-c"):
            cons, html = self._run(name)
            scope = cons.get("ech_scope") or {}
            if cons.get("integrity", {}).get("passed") is False and scope.get("status") == "unavailable":
                self.assertNotIn("estimated savings from", html.lower())
                self.assertNotIn("estimated monthly reduction", html.lower())
                self.assertIn("serverless estimate for", html)

    def test_fixtures_have_no_live_identifiers(self):
        from pathlib import Path
        import re
        root = Path(__file__).resolve().parent / "tests" / "fixtures"
        banned = re.compile(r"docs\.google\.com|drive\.google\.com|/Users/|acct-[abc](?:_[A-Za-z0-9]|[A-Za-z]|[, ])|\"ARR\"")
        hex_id = re.compile(r"\b[0-9a-f]{32}\b")
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".json", ".html", ".md"}:
                continue
            text = path.read_text()
            self.assertIsNone(banned.search(text), path.name)
            self.assertIsNone(hex_id.search(text), path.name)


class ReviewRegressionTests(unittest.TestCase):
    def test_diagnostic_fields_override_conflicting_tracker_measurements_and_record_sources(self):
        acc = {
            "solutions": {"by_deployment": [{
                "name": "measured", "platform": "ech", "solution": "observability",
                "gb": 999, "daily_raw_gb": 888, "retention_days": 777,
                "logs_gb_day": 300, "metrics_gb_day": 400, "ecu": 120,
            }], "totals": {"observability": {}, "security": {}, "search": {}}},
            "sizing": {"consolidated": {"observability": {"stored_gb": 999, "daily_raw_gb": 888}}},
            "storage": {"data_stored_gb": 999, "by_deployment": [{"name": "measured", "data_stored_gb": 999, "docs_count": 123}]},
            "ecu": {"total_lookback": 456},
            "workload": {"ingest": {"docs_per_sec": 7}},
        }
        row = {"name": "measured", "store_gb": 12, "bulk_gb_day": 2, "retention_days": 30,
               "retention_days_metrics": 10, "retention_index_count": 40, "retention_index_days_sum": 1200.0,
               "bulk_mix": {"logs": 1.0, "k8s_logs": 0.2, "metrics": 0.5, "traces": 0.5}}
        out = apply_diagnostics(acc, [row])
        dep = out["solutions"]["by_deployment"][0]
        self.assertEqual((dep["gb"], dep["daily_raw_gb"], dep["retention_days"]), (12, 2, 30))
        self.assertEqual((dep["logs_gb_day"], dep["metrics_gb_day"], dep["retention_days_metrics"]), (1.0, 0.5, 10))
        self.assertEqual(dep["field_sources"]["gb"], "diagnostics")
        self.assertEqual(dep["field_sources"]["ecu"], "cloud_tracker_billing")
        self.assertEqual(out["storage"]["data_stored_gb"], 12)
        self.assertEqual(out["storage"]["by_deployment"][0]["data_stored_gb"], 12)
        self.assertEqual(out["storage"]["by_deployment"][0]["docs_count"], 123)
        self.assertEqual(out["ecu"]["total_lookback"], 456)
        self.assertEqual(out["workload"]["ingest"]["docs_per_sec"], 7)
        self.assertIn("diagnostic_authoritative", out["sizing"]["source_precedence"])
        self.assertEqual(out["sizing"]["diagnostics"]["estimation_limits"], [])

    def test_diagnostic_report_view_overrides_measurements_but_keeps_tracker_billing_and_workload(self):
        account = {
            "storage": {"data_stored_gb": 900},
            "solutions": {"totals": {"search": {"gb": 900}}},
            "ecu": {"total_lookback": 123},
            "workload": {"search": {"peak": {"qps": 42}}},
            "sizing": {},
        }
        diagnostic = {
            "storage": {"data_stored_gb": 12},
            "solutions": {"totals": {"search": {"gb": 12}}},
            "sizing": {"source_precedence": {"diagnostic_authoritative": ["store"]}},
            "ecu": {"total_lookback": 999},
            "workload": {"search": {"peak": {"qps": 999}}},
        }
        _apply_diagnostic_report_view(account, diagnostic)
        self.assertEqual(account["storage"]["data_stored_gb"], 12)
        self.assertEqual(account["solutions"]["totals"]["search"]["gb"], 12)
        self.assertEqual(account["ecu"]["total_lookback"], 123)
        self.assertEqual(account["workload"]["search"]["peak"]["qps"], 42)
        self.assertEqual(account["sizing"]["source_precedence"]["diagnostic_authoritative"], ["store"])

    """Defects a review found in the window, scope, and unmeasured-deployment changes."""

    def test_search_ingest_share_excludes_k360_sizes_from_the_denominator(self):
        from size_serverless import org_store_gb
        acc = {
            "deployments": {"active": {"month": "2026-10", "names": ["search", "o11y", "nobundle"]}},
            "solutions": {"by_deployment": [
                {"name": "search", "solution": "search", "platform": "ech", "gb": 100, "ecu": 1},
                {"name": "o11y", "solution": "observability", "platform": "ech", "gb": 900, "ecu": 1},
                {"name": "nobundle", "solution": "observability", "platform": "ech", "gb": 9000, "ecu": 1},
            ], "totals": {}},
            "sizing": {"consolidated": {}}, "storage": {},
        }
        rows = [{"name": "search", "bulk_gb_day": 0, "store_gb": 100.0, "retention_days": 30},
                {"name": "o11y", "bulk_gb_day": 5.0, "store_gb": 900.0, "retention_days": 30}]
        out = apply_diagnostics(acc, rows, {"search", "o11y", "nobundle"})
        # The unsized K360-only 9,000 GB is context, not an estimator input.
        self.assertEqual(out["storage"]["data_stored_gb"], 1000.0)
        self.assertEqual(out["storage"]["ingest_share_store_gb"], 1000.0)
        self.assertEqual(out["storage"]["field_sources"]["ingest_share_store_gb"], "diagnostic_local_primary_store_sum")
        self.assertEqual(org_store_gb(out), 1000.0)
        self.assertEqual(org_store_gb({"storage": {"data_stored_gb": 42}}), 42.0)

    def test_a_young_org_is_priced_on_the_days_it_has_billed(self):
        acc = _quote_account(g28=10, e73=0)
        acc["ecu"]["daily_totals"] = [0.0] * 319 + [1500.0] * 45 + [0.0]
        apply_quote_lookback(acc, 90)
        self.assertEqual(acc["ecu"]["quote_lookback_requested"], 90)
        self.assertEqual(acc["ecu"]["quote_lookback_days"], 45)
        self.assertAlmostEqual(acc["ecu"]["quote_monthly_ecu"], round(1500 * 30.416, 2))
        notes = " ".join(build_consolidation(acc, None)["notes"])
        self.assertIn("The org has 45 billed days in the fetch", notes)

    def test_no_daily_series_is_no_window(self):
        acc = _quote_account(g28=10, e73=0)
        apply_quote_lookback(acc, 90)
        self.assertEqual(acc["ecu"]["quote_lookback_requested"], 90)
        self.assertIsNone(acc["ecu"]["quote_lookback_days"])
        cons = build_consolidation(acc, None)
        self.assertIsNone(cons["basis"]["quote_lookback_days"])
        self.assertIn("has no daily billing series", " ".join(cons["notes"]))
        self.assertNotIn("last 90 billed days", render_consolidation_html({**acc, "consolidation": cons}))
        # Asking again for the same window does not scale anything a second time.
        self.assertEqual(apply_quote_lookback(acc, 90), 1.0)

    def test_the_roster_follows_a_short_billing_life(self):
        from datetime import date, timedelta
        first = date(2026, 6, 1)
        days = [(first + timedelta(days=i)).isoformat() for i in range(121)]
        total = [0.0] * 75 + [1.0] * 45 + [0.0]
        org = {"range": {"start": "2025-11-11"},
               "chart": {"grain": "day", "days": days, "total": total, "series_names": ["a"], "ecu_series": [total]},
               "monthly": {"months": ["2026-08", "2026-09"], "by_deployment": {"a": {"ecu": [17, 28]}}}}
        active = active_deployment_set(org)
        self.assertEqual(active["window_days"], 45)
        self.assertEqual((active["window_start"], active["window_end"]), (days[75], days[119]))
        self.assertEqual(active["names"], ["a"])

    def test_a_solution_with_only_unsized_deployments_has_no_workload(self):
        from consolidation import _has_workload
        self.assertFalse(_has_workload({"stored_gb": 0, "daily_raw_gb": 0, "deployments": 2, "sized_deployments": 0}))
        self.assertTrue(_has_workload({"stored_gb": 0, "daily_raw_gb": 0, "deployments": 2, "sized_deployments": 1}))
        self.assertTrue(_has_workload({"stored_gb": 0, "daily_raw_gb": 0, "deployments": 2}))     # an older account

    def test_eis_from_an_unsized_search_deployment_is_not_added(self):
        from consolidation import _search_eis
        acc = {
            "ecu": {"quote_lookback_days": 1, "daily_days": ["2026-10-01"], "daily_totals": [1.0]},
            "ai_ml": {
                "billing_by_deployment_complete": True,
                "billing_by_deployment": {"sized": {"eis_ecu": 10, "million_tokens": 0, "ml_node_ecu": 0}},
                "daily_usage_complete": True,
                "daily_usage_by_deployment": {"sized": {
                    "days": ["2026-10-01"], "eis_ecu": [10], "million_tokens": [0],
                    "ml_node_ecu": [0], "models": [["jina-embeddings-v3"]],
                }},
                "by_deployment": [
                    {"name": "sized", "models": ["jina-embeddings-v3"], "model_labels": ["Jina"]},
                    {"name": "retired", "models": ["jina-embeddings-v3"]},
                ],
            },
        }
        eis = _search_eis(acc, 12.0, eligible_names={"sized"})
        self.assertEqual(eis["status"], "complete")
        self.assertEqual(eis["deployments"], 1)
        self.assertAlmostEqual(eis["monthly"], 10 * 30.416)

    def test_search_eis_scales_to_quote_window_and_only_includes_eligible_deployments(self):
        from consolidation import _search_eis
        acc = {
            "ecu": {"quote_lookback_days": 2, "daily_days": ["2026-10-01", "2026-10-02"], "daily_totals": [1.0, 1.0]},
            "cloud": {"by_deployment": [
                {"name": "quoted", "region": "us-east-1"},
                {"name": "existing", "region": "us-west-2"},
            ]},
            "ai_ml": {
                "billing_by_deployment_complete": True,
                "billing_by_deployment": {"quoted": {"eis_ecu": 20, "million_tokens": 2, "ml_node_ecu": 0}},
                "daily_usage_complete": True,
                "daily_usage_by_deployment": {"quoted": {
                    "days": ["2026-10-01", "2026-10-02"], "eis_ecu": [10, 10],
                    "million_tokens": [1, 1], "ml_node_ecu": [0, 0], "models": [[], []],
                }},
                "by_deployment": [
                    {"name": "quoted", "models": ["jina-embeddings-v3"], "model_labels": ["Jina"]},
                    {"name": "existing", "models": ["jina-embeddings-v3"], "model_labels": ["Jina"]},
                ],
            },
        }
        eis = _search_eis(acc, 12.0, eligible_names={"quoted"})
        monthly = 20 * 30.416 / 2
        self.assertEqual(eis["deployments"], 1)
        self.assertAlmostEqual(eis["monthly"], monthly)
        self.assertAlmostEqual(eis["by_region"]["us-east-1"], monthly)
        self.assertAlmostEqual(sum(row["monthly"] for row in eis["workloads"]), monthly)
        self.assertNotIn("us-west-2", eis["by_region"])

    def test_search_eis_uses_exact_quote_window_daily_usage_when_complete(self):
        from consolidation import _search_eis
        acc = {
            "ecu": {
                "quote_lookback_days": 2,
                "daily_days": ["2026-10-01", "2026-10-02", "2026-10-03"],
                "daily_totals": [10, 10, 10],
            },
            "ai_ml": {
                "billing_by_deployment_complete": True,
                "billing_by_deployment": {"quoted": {"eis_ecu": 160, "million_tokens": 16, "ml_node_ecu": 100}},
                "daily_usage_complete": True,
                "daily_usage_by_deployment": {"quoted": {
                    "days": ["2026-10-01", "2026-10-02", "2026-10-03"],
                    "eis_ecu": [100, 20, 40],
                    "million_tokens": [10, 2, 4],
                    "ml_node_ecu": [50, 20, 30],
                    "jina_ml_node_ecu": [0, 20, 0],
                    "models": [[], ["jina-embeddings-v3"], []],
                }},
                # Fetched-period totals reconcile with all three daily buckets;
                # the quote window uses only the last two days.
                "by_deployment": [{"name": "quoted", "eis_inference_ecu": 160,
                                   "ml_node_ecu": 100, "million_tokens": 16,
                                   "models": ["jina-embeddings-v3"], "model_labels": ["Jina"]}],
            },
            "cloud": {"by_deployment": [{"name": "quoted", "region": "us-east-1"}]},
        }
        eis = _search_eis(acc, 12, usage_factor=0.25, eligible_names={"quoted"})
        self.assertEqual(eis["usage_basis"], "quote_window_daily")
        self.assertEqual(eis["usage_days"], 2)
        self.assertAlmostEqual(eis["monthly"], (20 + 40) * 30.416 / 2)
        self.assertAlmostEqual(sum(row["monthly"] for row in eis["workloads"]), eis["monthly"])

    def test_eis_eligible_names_follow_quoteable_active_ech_search_scope(self):
        from consolidation import _priced_search_names
        acc = {
            "deployments": {"active": {"names": ["sized", "no-bundle", "existing"]}},
            "sizing": {"ingest_source": "diagnostics"},
            "solutions": {"by_deployment": [
                {"name": "sized", "solution": "search", "platform": "ech", "gb": 10, "ingest_source": "diagnostics"},
                {"name": "no-bundle", "solution": "search", "platform": "ech", "gb": 0, "k360_gb": 20, "ingest_source": "active_no_bundle"},
                {"name": "existing", "solution": "search", "platform": "serverless", "gb": 0},
                {"name": "inactive", "solution": "search", "platform": "ech", "gb": 10, "ingest_source": "diagnostics"},
            ]},
        }
        by_solution = {"search": {"pricing": "es3-estimator performant (quotable)"}}
        scope = {"unpriced": [{"name": "no-bundle", "reason": "no diagnostic bundle"}]}
        self.assertEqual(_priced_search_names(acc, by_solution, scope), {"sized"})

    def test_eis_eligibility_excludes_search_regions_omitted_from_regional_quote(self):
        from consolidation import _priced_search_names
        acc = {
            "deployments": {"active": {"names": ["quoted-east", "omitted-west"]}},
            "cloud": {"by_deployment": [
                {"name": "quoted-east", "region": "us-east-1"},
                {"name": "omitted-west", "region": "us-west-2"},
            ]},
            "solutions": {"by_deployment": [
                {"name": "quoted-east", "solution": "search", "platform": "ech", "gb": 50},
                {"name": "omitted-west", "solution": "search", "platform": "ech", "gb": 1},
            ]},
        }
        by_solution = {"search": {
            "pricing": "es3-estimator performant (quotable)",
            "regional_mode": "per_region",
            "by_region": [{"region": "us-east-1", "monthly": 50.0}],
        }}
        self.assertEqual(_priced_search_names(acc, by_solution, {"unpriced": []}), {"quoted-east"})

    def test_search_quote_prices_window_eis_and_excludes_existing_serverless_project(self):
        acc = _quote_account(g28=10, e73=0)
        acc["ecu"]["quote_usage_factor"] = 0.5
        acc["sizing"]["consolidated"]["search"] = {"stored_gb": 50, "daily_raw_gb": 0, "deployments": 1}
        acc["sizing"]["estimators"]["search"] = {"readback": {
            "monthly_steady_state": 50.0, "annual_year1": 600.0, "annual_steady_state": 600.0,
            "search_monthly": 30.0, "ingest_monthly": 10.0, "storage_monthly": 3.48, "support_monthly": 6.52,
        }}
        acc["solutions"]["by_deployment"] = [
            {"name": "ech-search", "solution": "search", "platform": "ech", "ecu": 10, "gb": 50, "ingest_source": "diagnostics"},
            {"name": "existing-search", "solution": "search", "platform": "serverless", "ecu": 10, "gb": 0},
        ]
        _stamp_search_eis(acc, "ech-search", eis=10, tokens=1, ml=40)
        acc["ai_ml"]["by_deployment"].append(
            {"name": "existing-search", "ml_node_ecu": 12000, "models": ["jina-embeddings-v3"]}
        )

        cons = build_consolidation(acc, None)
        line = next(q for q in cons["reconciliation"]["quotes"] if q["solution"] == "search")
        search = cons["serverless"]["by_solution"]["search"]
        # Window EIS is 10 ECU on one day, written as a month. The 0.5 billing
        # pace does not scale it. Support follows the read-back's own ratio.
        # The existing project's ML nodes stay out of both figures.
        window_eis = round(10 * 30.416, 2)
        rate = 6.52 / (30 + 10 + 3.48)
        self.assertAlmostEqual(line["monthly"], round(50 + window_eis * (1 + rate), 2))
        self.assertAlmostEqual(search["ml_node_ecu"], 40)
        self.assertNotAlmostEqual(search["ml_node_ecu"], 12040)
        self.assertAlmostEqual(line["annual_steady_state"], round(line["monthly"] * 12, 2))

    def test_incomplete_eis_keeps_the_notebook_readback(self):
        acc = _quote_account(g28=10, e73=0)
        acc["workload"] = {"quote_window_telemetry": {"status": "complete"}}
        acc["sizing"]["consolidated"]["search"] = {"stored_gb": 50, "daily_raw_gb": 0, "deployments": 1}
        acc["sizing"]["estimators"]["search"] = {"readback": {
            "monthly_steady_state": 76.0, "annual_year1": 912.0, "annual_steady_state": 912.0,
            "search_monthly": 60.0, "ingest_monthly": 5.0, "storage_monthly": 4.0, "support_monthly": 7.0,
        }}
        acc["solutions"]["by_deployment"] = [
            {"name": "ech-search", "solution": "search", "platform": "ech", "ecu": 10, "gb": 50, "ingest_source": "diagnostics"},
        ]
        acc["ai_ml"] = {
            "billing_by_deployment_complete": False,
            "billing_by_deployment_status": {"reason": "totals missing"},
        }
        _stamp_reconciled_billing(acc)
        cons = build_consolidation(acc, None)
        search = cons["serverless"]["by_solution"]["search"]
        self.assertIn("quotable", search["pricing"])
        self.assertAlmostEqual(search["total_mo"], 76.0)
        line = next(q for q in cons["reconciliation"]["quotes"] if q["solution"] == "search")
        self.assertAlmostEqual(line["monthly"], 76.0)
        self.assertAlmostEqual(cons["savings"]["serverless_mo"], 100 + 80 + 76)
        self.assertTrue(any("Search EIS was not added" in note for note in cons["notes"]))
        failed = [row["check"] for row in cons["integrity"]["checks"] if row.get("passed") is False]
        self.assertIn("Search EIS data", failed)
        self.assertFalse(cons["integrity"]["passed"])

    def test_unavailable_scope_without_priced_months_has_no_difference(self):
        acc = _quote_account(g28=10, e73=0)
        acc["ecu"]["quote_lookback_days"] = 30
        acc["ecu"]["daily_days"] = ["2026-11-01"]
        acc["ecu"]["daily_totals"] = [10.0]
        acc["ecu"]["monthly"] = {"months": ["2026-08", "2026-09", "2026-10"], "totals": [100, 200, 300]}
        cons = build_consolidation(acc, None)
        self.assertEqual(cons["ech_scope"]["status"], "unavailable")
        self.assertIsNone(cons["savings"]["list_mo"])
        self.assertIsNone(cons["savings"]["ech_list_mo"])
        self.assertEqual(cons["savings"]["serverless_mo"], 180)
        self.assertIsNone((cons.get("ech_recent_months_priced") or {}).get("average"))
        html = render_consolidation_html({**acc, "consolidation": cons, "cloud": {"summary": {}}})
        self.assertIn("$180", html)
        self.assertIn("serverless estimate for", html)
        self.assertNotIn("3-month average", html)
        self.assertIsNone(cons["savings"]["list_pct"])

    def test_bundle_coverage_counts_ech_deployments_only(self):
        acc = {
            "deployments": {"active": {"month": "2026-10", "names": ["ech", "named-project"]}},
            "solutions": {"by_deployment": [
                {"name": "ech", "solution": "observability", "platform": "ech", "gb": 10, "ecu": 1},
                {"name": "named-project", "solution": "search", "platform": "serverless", "ecu": 1},
            ], "totals": {}},
            "sizing": {"consolidated": {}}, "storage": {},
        }
        out = apply_diagnostics(acc, [{"name": "ech", "bulk_gb_day": 5.0, "store_gb": 10.0, "retention_days": 30}], {"ech"})
        cov = out["sizing"]["diagnostics"]
        self.assertEqual((cov["matched"], cov["active_deployments"]), (1, 1))

    def test_a_serverless_project_in_both_series_is_counted_once(self):
        acc = scope_account(ech_scope="all", keep_serverless_spend=False)
        # The infra series already holds every serverless SKU. Give the project its own row as well.
        acc["ecu"]["monthly_by_deployment"]["proj"] = [31, 31, 30, 31]
        scope = build_consolidation(acc, None)["ech_scope"]
        self.assertAlmostEqual(scope["serverless_mo"], 30.416, places=1)
        self.assertAlmostEqual(scope["priced_mo"], 6 * 30.416, places=1)

    def test_a_search_line_carries_eis_in_its_annual_figures(self):
        acc = _quote_account(g28=10, e73=0)
        acc["sizing"]["consolidated"]["search"] = {"stored_gb": 50, "daily_raw_gb": 0, "deployments": 1}
        acc["sizing"]["estimators"]["search"] = {"readback": {
            "monthly_steady_state": 50.0, "annual_year1": 600.0, "annual_steady_state": 600.0,
            "search_monthly": 30.0, "ingest_monthly": 10.0, "storage_monthly": 3.48, "support_monthly": 6.52,
        }}
        acc["solutions"]["by_deployment"] = [{"name": "s", "solution": "search", "platform": "ech", "ecu": 10, "gb": 50, "ingest_source": "diagnostics"}]
        _stamp_search_eis(acc, "s", eis=10, tokens=1, ml=40)
        cons = build_consolidation(acc, None)
        line = next(q for q in cons["reconciliation"]["quotes"] if q["solution"] == "search")
        self.assertGreater(line["monthly"], 50.0)
        self.assertAlmostEqual(line["annual_steady_state"], round(line["monthly"] * 12, 2), places=2)
        self.assertAlmostEqual(line["annual_year1"], round(line["monthly"] * 12, 2), places=2)

    def test_an_increase_is_not_ranked_as_a_cost_saving(self):
        acc = _quote_account(g28=10, e73=0)
        _stamp_reconciled_billing(acc)
        acc["sizing"]["consolidated"]["search"] = {"stored_gb": 10, "daily_raw_gb": 0, "deployments": 1}
        acc["sizing"]["estimators"]["search"]["readback"] = {
            "monthly_steady_state": 500, "annual_year1": 6000, "annual_steady_state": 6000,
        }
        acc["workload"] = {"quote_window_telemetry": {"status": "complete"}}
        acc["analysis"] = {"cost": [{"title": "Move workloads to Serverless (ES3)", "severity": "high", "evidence": "ballpark", "action": "x", "impact": "y"}]}
        build_consolidation(acc, None)
        rec = acc["analysis"]["cost"][0]
        self.assertEqual(rec["severity"], "low")
        self.assertIn("cost more", rec["title"])
        self.assertIn("above the ECH billed cost", rec["impact"])

    def test_documented_flags_parse_and_bad_windows_stop(self):
        import run_account_analysis as ra
        self.assertTrue(ra.parse_args(["--org", "X", "--allow-missing-diags"]).allow_missing_diags)
        for bad in (["--lookback-days", "0"], ["--lookback-days", "366"], ["--min-priced-ecu-pct", "101"]):
            with self.assertRaises(SystemExit):
                ra.parse_args(["--org", "X", *bad])

    def test_the_stamp_says_which_ingest_was_priced(self):
        self.assertEqual(method_stamp(90)["ingest_basis"], "metered diagnostic bulk")
        backup = method_stamp(90, diagnostic=False)
        self.assertEqual(backup["ingest_basis"], "K360 stored GB ÷ assumed retention")
        self.assertEqual(backup["retention"], "assumed month")

    def test_the_cover_names_the_whole_bill_and_the_compared_part(self):
        acc = scope_account(ech_scope="priced", keep_serverless_spend=False)
        cons = build_consolidation(acc, None)
        html = render_consolidation_html({**acc, "consolidation": cons})
        header = html.split("</header>", 1)[0]
        self.assertIn("304 ECU/mo", header)                        # the whole window, 10 a day
        self.assertNotIn("182 ECU/mo", header)                     # not the 6 a day that is compared
        cover = html.split("<section class='sec newpage'>", 1)[0]
        self.assertIn(">2 deployments<", cover)                    # the ECH bar bills for the two priced ones
        self.assertIn("The ECH bar bills for the priced ones only.", cover)
        # Each deployment's row is its own part of the window, the same figure the scope note uses.
        rows = {r["name"]: r["ech_ecu_mo"] for r in cons["by_deployment"]}
        self.assertAlmostEqual(rows["o11y"], 5 * 30.416, places=1)
        self.assertAlmostEqual(rows["nobundle"], cons["ech_scope"]["unpriced"][0]["ecu_mo"], places=2)
        self.assertEqual(cons["by_deployment_basis"], "daily_deployment_billing")


class ReviewV4Tests(unittest.TestCase):
    def test_a_failed_quote_is_not_announced_as_a_price(self):
        from run_consolidation import chat_lines

        brief = {
            "savings": {"serverless_mo": 76608},
            "integrity": {"passed": False, "failed": [{"check": "Rated ingest", "detail": "33%"}]},
            "limits": [{"code": "unrated_ingest", "detail": "unrated store is 33% of local store", "severity": "high"}],
            "draft": False,
        }
        lines = chat_lines(brief)
        self.assertEqual(lines[0], "not verified")
        self.assertIn("Rated ingest", lines[1])
        self.assertIn("unrated store is 33%", lines[2])
        warned = chat_lines({
            "savings": {"serverless_mo": 250},
            "integrity": {
                "passed": True,
                "failed": [],
                "warnings": [
                    {"check": "Rate window", "detail": "60% rated over a short window"},
                    {"check": "Counter age", "detail": "40% from a restarted bulk counter"},
                ],
            },
            "draft": False,
        })
        self.assertEqual(warned[0], "Serverless $250/mo")
        self.assertIn("Rate window", warned[1])
        self.assertIn("Counter age", warned[1])
        self.assertNotIn("not verified", warned[0])

    def test_skill_paths_are_the_same_file_in_each_skill(self):
        root = HERE.parents[1]
        paths = [
            root / "diag-serverless-consolidation" / "scripts" / "skill_paths.py",
            root / "k360-account-analysis" / "scripts" / "skill_paths.py",
            root / "k360-org-report" / "scripts" / "skill_paths.py",
        ]
        text = paths[0].read_bytes()
        for path in paths[1:]:
            self.assertEqual(path.read_bytes(), text)

    def test_the_documented_title_is_the_one_the_code_writes(self):
        from estimate_paths import estimate_title

        from render_pdf import consolidation_pdf_stem

        title = estimate_title("Acme_2026-11-16", "observability", "", "us-east-1")
        pdf = consolidation_pdf_stem("Acme_2026-11-16", "consolidation")
        doc = (HERE.parent / "references" / "sizing-cells.md").read_text()
        skill = (HERE.parent / "SKILL.md").read_text()
        self.assertTrue(title.endswith("_observability_full retention_bulk_us-east-1_serverless_estimate"))
        self.assertIn("_full retention_<bulk>_<region>_serverless_estimate", doc)
        self.assertTrue(pdf.endswith("_full retention_bulk_serverless_consolidation_plan"))
        self.assertIn("_full retention_bulk_serverless_consolidation_plan", doc)
        self.assertIn("~/serverless_analyzer_artifacts/", skill)
        self.assertIn("~/serverless_analyzer_artifacts/", doc)

    def test_a_name_suffix_after_the_account_token_is_scrubbed(self):
        from strip_fixture import scrub_fixture

        cleaned = scrub_fixture({"customer": "acct-c, Inc.", "cluster": "acct-c Non-Prod"}, "ref-c")
        self.assertNotIn("acct-c", json.dumps(cleaned))

    def test_an_admin_redirect_is_refused(self):
        import admin_diags

        with self.assertRaises(RuntimeError):
            admin_diags._RefuseRedirect().redirect_request(None, None, 302, "found", {}, "https://other.example/")

    def test_manifests_from_three_orgs_merge_by_deployment_id(self):
        import admin_diags

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            admin_diags._merge_manifest(folder, [{"id": "a", "name": "one"}, {"id": "b", "name": "two"}])
            merged = admin_diags._merge_manifest(folder, [{"id": "b", "name": "two-b"}, {"id": "c", "name": "three"}])
        self.assertEqual(sorted(row["id"] for row in merged), ["a", "b", "c"])
        self.assertEqual(next(row for row in merged if row["id"] == "b")["name"], "two-b")

    def test_a_row_without_a_solution_stops_the_pack(self):
        import diag_inputs

        with self.assertRaises(SystemExit):
            diag_inputs._join_solution_region([{"name": "cluster", "bulk_gb_day": 1}], None)

    def test_a_measured_age_does_not_add_a_second_sheet(self):
        """The measured-retention copy is retired: nothing writes one, and nothing prices it."""
        import pathlib
        import plan_sizing
        self.assertFalse(hasattr(plan_sizing, "fleet_measured_copy"))
        here = pathlib.Path(plan_sizing.__file__).parent
        writers = [
            path.name for path in here.glob("*.py")
            if not path.name.startswith("test_") and '"role": "measured_retention"' in path.read_text()
        ]
        self.assertEqual(writers, [])
        # A legacy account carrying one is not given an "At measured retention" row either.
        acc = _quote_account(g28=10, e73=0)
        acc["sizing"]["estimators"]["projects"] = [{
            "solution": "observability", "region": "us-east-1", "role": "measured_retention",
            "readback": {"monthly_steady_state": 123.0},
        }]
        rows = build_consolidation(acc, None)["sensitivity"]["retention"]
        self.assertEqual([r for r in rows if r.get("label") == "At measured retention"], [])

    def test_zero_days_and_effective_retention_are_measured(self):
        import admin_diags

        self.assertEqual(admin_diags._parse_age("0d"), 0.0)
        self.assertEqual(admin_diags._parse_age("10d"), 10.0)


if __name__ == "__main__":
    unittest.main()


class ConfidenceTests(unittest.TestCase):
    """How much of the price was measured, as a question of its own."""

    @staticmethod
    def _conf(**scores):
        from quote_checks import confidence
        return confidence([
            {"check": name, "score": value, "passed": True} for name, value in scores.items()
        ])

    def test_everything_measured_is_high(self):
        row = self._conf(**{"Retention month": 1.0, "Bundle coverage": 1.0, "Rated ingest": 1.0})
        self.assertEqual((row["score"], row["band"]), (1.0, "High"))
        self.assertEqual(row["drivers"], [])

    def test_weight_follows_price_leverage(self):
        """Retention drives the ladder; a counter-age flag sits beside a measured rate."""
        heavy = self._conf(**{"Retention month": 0.0, "Counter age": 1.0})
        light = self._conf(**{"Retention month": 1.0, "Counter age": 0.0})
        self.assertLess(heavy["score"], light["score"])
        self.assertEqual(heavy["score"], 0.25)   # weights 3 and 1
        self.assertEqual(light["score"], 0.75)

    def test_an_unmeasured_retention_month_drops_the_band(self):
        """The case the unweighted mean hid: correct arithmetic on an assumed month."""
        row = self._conf(**{
            "Retention month": 0.0, "Retention basis": 1.0, "Bundle coverage": 1.0,
            "Priced share": 1.0, "Rated ingest": 1.0, "Counter age": 1.0,
        })
        self.assertEqual(row["band"], "Moderate")
        self.assertEqual(row["drivers"][0]["check"], "Retention month")

    def test_drivers_are_ordered_by_what_they_cost(self):
        row = self._conf(**{"Counter age": 0.0, "Priced share": 0.5, "Rated ingest": 0.9})
        self.assertEqual([d["check"] for d in row["drivers"]], ["Priced share", "Counter age", "Rated ingest"])
        self.assertGreater(row["drivers"][0]["cost"], row["drivers"][-1]["cost"])

    def test_a_check_that_does_not_apply_is_left_out(self):
        """A fleet with no Search is not marked down for having no EIS."""
        from quote_checks import confidence
        row = confidence([
            {"check": "Retention month", "score": 1.0, "passed": True},
            {"check": "Search EIS data", "score": None, "passed": None},
        ])
        self.assertEqual(row["checks_counted"], 1)
        self.assertEqual(row["band"], "High")

    def test_correctness_checks_are_not_in_the_confidence(self):
        from quote_checks import confidence
        row = confidence([{"check": "Line agreement", "score": 0.0, "passed": False}])
        self.assertIsNone(row["score"])
        self.assertIsNone(row["band"])

    def test_a_quote_can_pass_every_check_and_still_not_be_high(self):
        """Nothing failed, and none of the retention was measured. Both are true at once."""
        from quote_checks import integrity_score
        recorded = integrity_score({"coverage": {
            "bulk_gb_day": 10, "retention_policy_bulk_gb_day": 0,
            "unrated": {"indices": 0, "store_gb": 0, "share_of_store": 0.0, "deployments": []},
        }})
        self.assertNotIn(False, [r["passed"] for r in recorded["checks"]])
        self.assertEqual(recorded["confidence"]["band"], "Low")
        self.assertEqual(recorded["confidence"]["drivers"][0]["check"], "Retention basis")
