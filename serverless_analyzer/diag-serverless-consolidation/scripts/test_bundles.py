"""Bundles tests for the diagnostic quote. No Google and no sheets."""
from __future__ import annotations

import json
import tempfile
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

from admin_diags import _user_index, analyze_bundle, quoted_index_bytes  # noqa: E402
from consolidation import build_consolidation  # noqa: E402
from diag_account import _sheet_months, apply_diagnostics  # noqa: E402
from diag_inputs import index_retention_months, summarize, whole_months  # noqa: E402
from quote_checks import echo_gate, integrity_score  # noqa: E402
from size_serverless import _sheet_retention, push_cells  # noqa: E402

from _test_support import (  # noqa: E402
    COLLECTION_MS,
    HERE,
    K360,
    _analyze,
    _quote_account,
    _stamp_search_eis,
    _write_phase,
)


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
            "diag/nodes_stats.json": {"nodes": {}},
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
        self.assertNotIn("compression_ratio", row)
        self.assertNotIn("compressioncalc_accounted_for", row)


class RetentionTests(unittest.TestCase):
    def test_unmeasured_days_stay_out_of_the_weight(self):
        mixed = summarize([
            {"name": "a", "bulk_gb_day": 10, "retention_days": 0, "store_gb": 1},
            {"name": "b", "bulk_gb_day": 10, "retention_days": 30.416, "store_gb": 1},
        ])
        self.assertEqual(mixed["months_written"], 12)
        self.assertEqual(mixed["retention_unmeasured"], [{"name": "a", "bulk_gb_day": 10.0}])
        empty = summarize([{"name": "a", "bulk_gb_day": 5, "retention_days": None, "store_gb": 1}])
        self.assertEqual(empty["months_written"], 12)
        self.assertEqual(empty["observability_cells"]["G11_retention_months"], 12)
        subday = summarize([{"name": "a", "bulk_gb_day": 2, "retention_days": 0.042, "store_gb": 1}])
        self.assertEqual(subday["months_written"], 12)
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

    def test_index_average_rounds_up_and_is_written(self):
        # 1,000 indexes at 15 days and 10 at 365 days. Across indexes that is
        # 18.5 days, which rounds up to 1 month. Averaging the two deployments
        # would be 190 days and 7 months.
        self.assertEqual(index_retention_months([
            {"retention_index_count": 1000, "retention_index_days_sum": 15 * 1000},
            {"retention_index_count": 10, "retention_index_days_sum": 365 * 10},
        ]), 1)
        self.assertEqual(index_retention_months([
            {"retention_index_count": 4, "retention_index_days_sum": 45 * 4},
        ]), 2)
        self.assertEqual(index_retention_months([
            {"retention_index_count": 6292, "retention_index_days_sum": 27.605 * 6292},
        ]), 1)
        self.assertIsNone(index_retention_months([{"retention_days": 45}]))
        acc = {
            "deployments": {"active": {"month": "2026-10", "names": ["alpha", "beta"], "partial": False}},
            "solutions": {"by_deployment": [
                {"name": "alpha", "platform": "ech", "solution": "observability", "gb": 100, "ecu": 1},
                {"name": "beta", "platform": "ech", "solution": "security", "gb": 20, "ecu": 1},
            ], "totals": {"observability": {}, "security": {}, "search": {}}},
            "sizing": {"consolidated": {"observability": {}, "security": {}, "search": {}}},
            "storage": {"by_deployment": []},
        }
        rows = [
            {"name": "alpha", "bulk_gb_day": 10, "retention_days": 15, "store_gb": 5,
             "retention_index_count": 1000, "retention_index_days_sum": 15 * 1000},
            {"name": "beta", "bulk_gb_day": 2, "retention_days": 365, "store_gb": 4,
             "retention_index_count": 10, "retention_index_days_sum": 365 * 10},
        ]
        out = apply_diagnostics(acc, rows)
        self.assertEqual(out["meta"]["estimate_months"], 1)
        self.assertEqual(out["meta"]["retention_index_count"], 1010)
        self.assertEqual(out["sizing"]["consolidated"]["observability"]["retention_months"], 1)
        self.assertEqual(out["sizing"]["consolidated"]["security"]["retention_months"], 1)
        self.assertTrue(any("prices 1 month" in note for note in out["sizing"]["notes"]))
        # An explicit --retention-days month stays, and the index average does not replace it.
        acc["meta"] = {"estimate_months": 2, "retention_days": 60}
        held = apply_diagnostics(acc, rows)
        self.assertEqual(held["meta"]["estimate_months"], 2)
        self.assertEqual(held["sizing"]["consolidated"]["observability"]["retention_months"], 2)
        self.assertIsNone(held["meta"].get("retention_index_count"))

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
        self.assertEqual(observability["retention_months"], 12)   # no index counts: stay at 12, not the measured age
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
        # A one-year estimate does not need a measured age: bulk with none is still written at 12 months.
        self.assertEqual(block["retention_months"], 12)
        self.assertNotIn("retention_months_measured", block)
        self.assertFalse(block["retention_unmeasured"])


class UnratedIngestTests(unittest.TestCase):
    @staticmethod
    def _ix(bulk: int, store: int = 1_000_000_000) -> dict:
        return {"primaries": {"bulk": {"total_size_in_bytes": bulk}, "store": {"size_in_bytes": store}}}

    def test_only_written_indices_with_no_rate_and_no_successor_are_listed(self):
        from admin_diags import unrated_ingest

        stats = {
            ".ds-logs-a-000002": self._ix(500),          # rated write index
            ".ds-logs-a-000001": self._ix(900),          # rolled generation of that stream
            "app-000007": self._ix(300),                 # alias write index, rated
            "app-000006": self._ix(800),                 # rolled under the same write alias
            "rolled-by-ilm": self._ix(700),              # ILM rollover, no alias left
            "daily-2026.11.12": self._ix(4_000_000_000, 6_000_000_000),   # written by name
            "daily-2026.11.01": self._ix(0),             # no bulk since its shard started
            "orders": self._ix(2_000_000_000, 3_000_000_000),             # write alias, not ILM managed
            ".kibana_1": self._ix(999),                  # system
            "partial-.ds-logs-a-000000": self._ix(999),  # searchable snapshot
        }
        streams = [{"indices": [{"index_name": ".ds-logs-a-000001"}, {"index_name": ".ds-logs-a-000002"}]}]
        alias = {
            "app-000007": {"aliases": {"app": {"is_write_index": True}}},
            "app-000006": {"aliases": {"app": {"is_write_index": False}}},
            "orders": {"aliases": {"orders-write": {"is_write_index": True}}},
        }
        explain = {"rolled-by-ilm": {"index_creation_date_millis": 1_000, "lifecycle_date_millis": 9_000}}
        rated = {".ds-logs-a-000002": 10, "app-000007": 10, "orders": 0}
        out = unrated_ingest(stats, streams, alias, explain, rated)
        self.assertEqual(out["unrated_indices"], 2)
        self.assertEqual([row["index"] for row in out["unrated_top"]], ["daily-2026.11.12", "orders"])
        self.assertEqual(out["unrated_top"][0]["reason"], "not a data stream, write alias, or rollover")
        self.assertEqual(out["unrated_top"][1]["reason"], "write index with no write phase")
        self.assertEqual(out["unrated_store_gb"], 9.0)
        self.assertEqual(out["unrated_bulk_gb"], 6.0)

    def test_coverage_note_and_integrity_check_carry_the_unrated_store(self):
        acc = {
            "solutions": {"by_deployment": [
                {"name": "a", "solution": "observability", "platform": "ech", "gb": 1},
                {"name": "b", "solution": "observability", "platform": "ech", "gb": 1},
            ], "totals": {}},
            "sizing": {"consolidated": {}},
        }
        rows = [
            {"name": "a", "bulk_gb_day": 10, "store_gb": 80, "retention_days": 30,
             "unrated_indices": 3, "unrated_store_gb": 20, "unrated_bulk_gb": 7,
             "unrated_top": [{"index": "daily-1", "reason": "x", "store_gb": 20, "bulk_gb": 7}]},
            {"name": "b", "bulk_gb_day": 5, "store_gb": 20, "retention_days": 30,
             "unrated_indices": 0, "unrated_store_gb": 0, "unrated_bulk_gb": 0, "unrated_top": []},
        ]
        out = apply_diagnostics(acc, rows)
        unrated = out["sizing"]["diagnostics"]["unrated"]
        self.assertEqual((unrated["indices"], unrated["store_gb"], unrated["share_of_store"]), (3, 20.0, 0.2))
        self.assertEqual([row["name"] for row in unrated["deployments"]], ["a"])
        self.assertTrue(any("have no ingest rate" in note and "20.0% of local store" in note for note in out["sizing"]["notes"]))
        # The measured bulk is unchanged: the unrated volume is reported, not added.
        self.assertEqual(out["sizing"]["diagnostics"]["bulk_gb_day"], 15)

        def rated(cov):
            result = integrity_score({"coverage": cov})
            return next(row for row in result["checks"] if row["check"] == "Rated ingest")

        flagged = rated({"unrated": unrated})
        self.assertEqual((flagged["score"], flagged["passed"]), (0.8, False))
        self.assertIn("a (20 GB)", flagged["detail"])
        small = rated({"unrated": {"indices": 1, "store_gb": 4, "share_of_store": 0.04, "deployments": []}})
        self.assertEqual((small["score"], small["passed"]), (0.96, True))
        clean = rated({"unrated": {"indices": 0, "store_gb": 0, "share_of_store": 0.0, "deployments": []}})
        self.assertEqual((clean["score"], clean["passed"]), (1.0, True))

    def test_rows_parsed_before_the_count_existed_leave_the_check_out(self):
        acc = {
            "solutions": {"by_deployment": [{"name": "a", "solution": "observability", "platform": "ech", "gb": 1}], "totals": {}},
            "sizing": {"consolidated": {}},
        }
        out = apply_diagnostics(acc, [{"name": "a", "bulk_gb_day": 10, "store_gb": 80, "retention_days": 30}])
        self.assertIsNone(out["sizing"]["diagnostics"]["unrated"])
        row = next(r for r in integrity_score({"coverage": {"unrated": None}})["checks"] if r["check"] == "Rated ingest")
        self.assertIsNone(row["score"])


class SeriesRateTests(unittest.TestCase):
    NOW = 1_800_000_000_000
    HOUR = 3_600_000

    @staticmethod
    def _ix(bulk: int, store: int = 1_000) -> dict:
        return {"primaries": {"bulk": {"total_size_in_bytes": bulk}, "store": {"size_in_bytes": store}}}

    def _rates(self, stats, streams=(), alias=None, explain=None, created=None, mode=None):
        from admin_diags import series_rates
        return series_rates(stats, list(streams), alias or {}, explain or {}, created or {}, mode or {}, self.NOW)

    def test_window_rate_keeps_esdiag_for_a_full_day_and_extends_a_short_one(self):
        from admin_diags import _window_rate
        day = 86_400
        # A write phase of a day or more is esdiag's own figure. Older generations are not read.
        self.assertEqual(_window_rate([(1_000, day, True), (9_000, day, True)]), (1_000, day, 1))
        # Two hours at a burst, then the 22 hours before it: the rate is the whole day's bytes.
        self.assertEqual(_window_rate([(600, 7_200, True), (1_800, 79_200, True)]), (2_400, day, 2))
        # It stops at the first generation that lost its counter, even if a later one is fine.
        self.assertEqual(_window_rate([(600, 7_200, True), (0, 79_200, False), (5_000, day, True)])[2], 1)
        self.assertEqual(_window_rate([(600, 0, True)]), (0, 0, 0))
        self.assertEqual(_window_rate([]), (0, 0, 0))

    def test_short_write_phase_takes_the_generation_before_it(self):
        h = self.HOUR
        streams = [{"name": "logs-app-default", "indices": [{"index_name": ".ds-logs-app-000001"}, {"index_name": ".ds-logs-app-000002"}]}]
        stats = {".ds-logs-app-000002": self._ix(600, 200), ".ds-logs-app-000001": self._ix(1_800, 600)}
        explain = {
            ".ds-logs-app-000002": {"index_creation_date_millis": self.NOW - 2 * h, "lifecycle_date_millis": self.NOW - 2 * h, "action": "rollover", "phase": "hot"},
            ".ds-logs-app-000001": {"index_creation_date_millis": self.NOW - 24 * h, "lifecycle_date_millis": self.NOW - 2 * h, "phase": "hot"},
        }
        out = self._rates(stats, streams, explain=explain)
        self.assertEqual(out["bytes_per_day"], 2_400)          # not 600 × 12
        self.assertEqual(out["short_window_bytes"], 0)
        self.assertEqual(out["mix"], {"logs": 2_400, "k8s_logs": 0, "metrics": 0, "traces": 0})
        # The older generation moved to warm, or its counter no longer matches its store: write index alone.
        for change in ({"phase": "warm"}, None):
            ex = json.loads(json.dumps(explain))
            st = json.loads(json.dumps(stats))
            if change:
                ex[".ds-logs-app-000001"].update(change)
            else:
                st[".ds-logs-app-000001"] = self._ix(100, 600)
            alone = self._rates(st, streams, explain=ex)
            self.assertEqual(alone["bytes_per_day"], 7_200)
            self.assertEqual(alone["short_window_bytes"], 7_200)

    def test_data_stream_lifecycle_and_dated_indices_are_rated_as_fallbacks(self):
        h = self.HOUR
        streams = [{"name": "metrics-system.cpu-default", "indices": [{"index_name": ".ds-metrics-system.cpu-000003"}]}]
        stats = {
            ".ds-metrics-system.cpu-000003": self._ix(4_800),
            "app-2026.11.12": self._ix(1_000, 500),
            "app-2026.11.11": self._ix(2_000, 1_000),
            "orders": self._ix(9_999),               # not dated: stays unrated
            "old-2020.01.01": self._ix(9_999),       # dated, but its newest index is years old
        }
        created = {
            ".ds-metrics-system.cpu-000003": self.NOW - 48 * h,
            "app-2026.11.12": self.NOW - 12 * h,
            "app-2026.11.11": self.NOW - 36 * h,
            "orders": self.NOW - 900 * 24 * h,
            "old-2020.01.01": self.NOW - 900 * 24 * h,
        }
        out = self._rates(stats, streams, created=created)
        # Data stream lifecycle: 4,800 bytes over the write index's 48 hours.
        self.assertEqual(out["rated"][".ds-metrics-system.cpu-000003"], 2_400)
        # Dated family: 12 h is short, so yesterday's index joins: 3,000 bytes over 36 h.
        self.assertEqual(out["rated"]["app-2026.11.12"], 2_000)
        self.assertEqual(out["bytes_per_day"], 4_400)
        self.assertEqual(out["fallback_bytes"], 4_400)
        self.assertEqual(out["mix"]["metrics"], 2_400)
        self.assertEqual(out["mix"]["logs"], 2_000)
        self.assertEqual(out["covered"], {"app-2026.11.12", "app-2026.11.11"})
        from admin_diags import unrated_ingest
        left = unrated_ingest(stats, streams, {}, {}, out["rated"], out["covered"])
        self.assertEqual(sorted(row["index"] for row in left["unrated_top"]), ["old-2020.01.01", "orders"])

    def test_kind_comes_from_the_series_name_or_the_index_mode(self):
        from admin_diags import ingest_kind
        self.assertEqual(ingest_kind("logs-nginx.access-default"), "logs")
        self.assertEqual(ingest_kind("logs-kubernetes.container_logs-default"), "k8s_logs")
        self.assertEqual(ingest_kind("metrics-system.cpu-default"), "metrics")
        self.assertEqual(ingest_kind(".ds-metrics-system.cpu-default-2026.11.01-000004"), "metrics")
        self.assertEqual(ingest_kind("custom-tsdb", "time_series"), "metrics")
        self.assertEqual(ingest_kind("traces-apm-default"), "traces")
        self.assertEqual(ingest_kind("orders-000007"), "logs")


class RetentionAndRestartTests(unittest.TestCase):
    """Store with no delete policy is priced at 30 days. A restarted shard is flagged, not corrected."""

    CAPTURED = datetime(2026, 11, 1, tzinfo=timezone.utc)
    DAY = 86_400_000

    def _bundle(self, tmp: Path, indices: dict, policies: dict, recovery: dict | None = None) -> Path:
        """indices: name -> (store bytes, age in days, policy or None, bulk bytes)."""
        now = int(self.CAPTURED.timestamp() * 1000)
        stats = {"indices": {n: {"primaries": {"store": {"size_in_bytes": st}, "bulk": {"total_size_in_bytes": bulk}}} for n, (st, _age, _pol, bulk) in indices.items()}}
        state = {"metadata": {"indices": {n: {"settings": {"index": {"creation_date": str(now - int(age * self.DAY))}}} for n, (_st, age, _pol, _b) in indices.items()}}}
        explain = {"indices": {n: {"managed": True, "policy": pol, "index_creation_date_millis": now - int(age * self.DAY), "lifecycle_date_millis": now - int(age * self.DAY), "action": "rollover"}
                               for n, (_st, age, pol, _b) in indices.items() if pol}}
        pol = {name: {"policy": {"phases": {"delete": {"min_age": f"{days}d"}}}} for name, days in policies.items()}
        path = tmp / "a__1.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("diag/indices_stats.json", json.dumps(stats))
            z.writestr("diag/cluster_state.json", json.dumps(state))
            z.writestr("diag/nodes_stats.json", json.dumps({"nodes": {"n1": {"timestamp": now, "jvm": {"uptime_in_millis": 30 * self.DAY}, "indices": {"bulk": {"total_size_in_bytes": 1}}}}}))
            z.writestr("diag/commercial/ilm_explain.json", json.dumps(explain))
            z.writestr("diag/commercial/ilm_policies.json", json.dumps(pol))
            if recovery is not None:
                z.writestr("diag/recovery.json", json.dumps(recovery))
        return path

    def test_store_with_no_policy_is_blended_in_at_its_own_age(self):
        from admin_diags import analyze_bundle
        with tempfile.TemporaryDirectory() as tmp:
            path = self._bundle(Path(tmp), {
                "kept": (1_000_000_000, 10, "p30", 0),
                "nopolicy": (3_000_000_000, 100, None, 0),
            }, {"p30": 30})
            row = analyze_bundle(path, captured=self.CAPTURED)
        # 1 GB at the 30-day policy and 3 GB at the 30-day default.
        self.assertAlmostEqual(row["retention_days"], 30.0, places=1)
        self.assertEqual(row["retention_index_count"], 2)
        self.assertAlmostEqual(row["retention_index_days_sum"], 60.0, places=3)
        self.assertEqual(row["retention_basis"], "blended")
        self.assertAlmostEqual(row["retention_known_share"], 0.25, places=3)
        self.assertIn("assumed", row["retention_label"])

    def test_with_no_policy_anywhere_retention_is_the_30_day_default(self):
        from admin_diags import analyze_bundle
        with tempfile.TemporaryDirectory() as tmp:
            path = self._bundle(Path(tmp), {
                "huge": (9_000_000_000, 10, None, 0),
                "tiny-old": (1_000_000, 2000, None, 0),
            }, {})
            row = analyze_bundle(path, captured=self.CAPTURED)
        self.assertEqual(row["retention_basis"], "assumed")
        self.assertAlmostEqual(row["retention_days"], 30.0, places=1)
        self.assertEqual(row["retention_known_share"], 0.0)
        self.assertIn("assumed", row["retention_label"])

    def test_a_restarted_primary_is_flagged_and_the_quoted_rate_is_unchanged(self):
        from admin_diags import series_rates
        now, h = 1_800_000_000_000, 3_600_000
        streams = [{"name": "logs-app-default", "indices": [{"index_name": ".ds-logs-app-000001"}]}]
        stats = {".ds-logs-app-000001": {"primaries": {"bulk": {"total_size_in_bytes": 4_800}, "store": {"size_in_bytes": 1_000}}}}
        explain = {".ds-logs-app-000001": {"index_creation_date_millis": now - 48 * h, "lifecycle_date_millis": now - 48 * h, "action": "rollover", "phase": "hot"}}
        plain = series_rates(stats, streams, {}, explain, {}, {}, now)
        restarted = series_rates(stats, streams, {}, explain, {}, {}, now, {".ds-logs-app-000001": now - 12 * h})
        # Both quote the esdiag figure: 4,800 bytes over a 2-day write phase.
        self.assertEqual(plain["bytes_per_day"], restarted["bytes_per_day"])
        self.assertEqual(plain["bytes_per_day"], 2_400)
        self.assertEqual((plain["restart_extra_bytes"], plain["restart_indices"]), (0, 0))
        # The counter has run 12 hours, not 48: the same bytes over half a day is 9,600 a day, 7,200 more.
        self.assertEqual((restarted["restart_extra_bytes"], restarted["restart_indices"]), (7_200, 1))
        # A primary that started inside 10% of the write phase is not flagged.
        near = series_rates(stats, streams, {}, explain, {}, {}, now, {".ds-logs-app-000001": now - 47 * h})
        self.assertEqual(near["restart_extra_bytes"], 0)

    def test_the_recovery_start_is_read_from_the_bundle(self):
        from admin_diags import analyze_bundle
        now = int(self.CAPTURED.timestamp() * 1000)
        recovery = {"kept": {"shards": [
            {"id": 0, "primary": True, "type": "EXISTING_STORE", "start_time_in_millis": now - 1 * self.DAY},
            {"id": 0, "primary": False, "type": "PEER", "start_time_in_millis": now - 5 * 60_000},
        ]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = self._bundle(Path(tmp), {"kept": (1_000_000_000, 10, "p30", 5_000_000)}, {"p30": 30}, recovery)
            row = analyze_bundle(path, captured=self.CAPTURED)
        self.assertIn("bulk_restart_gb_day", row)
        self.assertEqual(row["bulk_restart_indices"], 0)       # not a data stream write index, so no rate to flag

    def test_the_restart_and_blend_notes_reach_the_quote(self):
        acc = {
            "deployments": {"active": {"month": "2026-10", "names": ["a"]}},
            "solutions": {"by_deployment": [{"name": "a", "solution": "observability", "platform": "ech", "gb": 10, "ecu": 1}], "totals": {}},
            "sizing": {"consolidated": {}}, "storage": {},
        }
        rows = [{"name": "a", "bulk_gb_day": 10.0, "store_gb": 10.0, "retention_days": 30.0,
                 "bulk_restart_gb_day": 2.0, "bulk_restart_indices": 3, "retention_known_share": 0.5}]
        cov = apply_diagnostics(acc, rows, {"a"})["sizing"]["diagnostics"]
        self.assertEqual(cov["bulk_restart_gb_day"], 2.0)
        self.assertEqual(cov["bulk_restart_deployments"], [{"name": "a", "gb_day": 2.0}])
        self.assertEqual(cov["retention_blended_share"], 0.5)
        notes = " ".join(apply_diagnostics(acc, rows, {"a"})["sizing"]["notes"])
        self.assertIn("restarted or moved", notes)
        self.assertIn("50% of local store has no delete policy", notes)
        self.assertEqual(cov["bulk_gb_day"], 10.0)             # the quote keeps esdiag's figure


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
        self.assertNotIn("compressioncalc_accounted_for", row)
        self.assertNotIn("compression_ratio", row)

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
        self.assertNotIn("compressioncalc_accounted_for", row)
        self.assertNotIn("compression_ratio", row)

    def test_on_disk_above_bulk_does_not_change_the_rate(self):
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
        self.assertNotIn("compression_ratio", row)
        self.assertNotIn("compressioncalc_accounted_for", row)

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
            {"name": "alpha", "bulk_gb_day": 100, "compressioncalc_accounted_for": 20, "compression_ratio": 0.2, "retention_days": 30.416, "store_gb": 5, "snapshot_gb": 0, "bulk_mix": {"logs": 90, "metrics": 10}},
            {"name": "beta", "bulk_gb_day": 10, "compressioncalc_accounted_for": 5, "compression_ratio": 0.5, "retention_days": 60.832, "store_gb": 4, "snapshot_gb": 0, "bulk_mix": {"logs": 9, "metrics": 1}},
            {"name": "gamma", "bulk_gb_day": 8, "compressioncalc_accounted_for": 2, "compression_ratio": 0.25, "retention_days": 30.416, "store_gb": 8, "snapshot_gb": 0},
            {"name": "delta", "bulk_gb_day": 10, "compressioncalc_accounted_for": 0, "compression_ratio": None, "retention_days": 30.416, "store_gb": 1, "snapshot_gb": 0},
        ]
        quoted = apply_diagnostics(acc, rows)
        observability = quoted["sizing"]["consolidated"]["observability"]
        self.assertEqual(observability["daily_raw_gb"], 110.0)
        self.assertEqual(observability["logs_gb_day"], 99.0)
        self.assertEqual(observability["metrics_gb_day"], 11.0)
        self.assertEqual(observability["bulk_gb_day"], 110.0)
        self.assertNotIn("compressioncalc_accounted_for", observability)
        self.assertNotIn("compression_ratio", observability)
        self.assertEqual(quoted["sizing"]["consolidated"]["search"]["daily_raw_gb"], 8.0)
        self.assertEqual(quoted["sizing"]["consolidated"]["security"]["daily_raw_gb"], 10.0)


class MeasuredMixTests(unittest.TestCase):
    def test_mix_keeps_measured_proportions_and_refuses_a_missing_split(self):
        from diag_account import observability_mix
        measured = {"daily_raw_gb": 60.0, "logs_gb_day": 80.0, "k8s_logs_gb_day": 20.0, "metrics_gb_day": 15.0, "traces_gb_day": 5.0, "retention_days_metrics": 90}
        mix = observability_mix([measured])
        # The row's bulk was scaled to 60 (compression-applied). The proportions hold.
        self.assertEqual((mix["logs_gb_day"], mix["metrics_gb_day"], mix["traces_gb_day"], mix["k8s_logs_gb_day"]), (48.0, 9.0, 3.0, 12.0))
        self.assertEqual((mix["k8s_logs_share"], mix["metrics_retention_days"], mix["basis"]), (0.25, 90.0, "measured"))
        with self.assertRaises(ValueError):
            observability_mix([{"daily_raw_gb": 100.0}])
        with self.assertRaises(ValueError):
            observability_mix([measured, {"daily_raw_gb": 100.0}])
        self.assertIsNone(observability_mix([{"daily_raw_gb": 0}])["basis"])

    def test_rollup_writes_the_measured_split_and_the_metrics_month(self):
        acc = {
            "solutions": {"by_deployment": [{"name": "a", "solution": "observability", "platform": "ech", "gb": 1}], "totals": {}},
            "sizing": {"consolidated": {}},
        }
        row = {"name": "a", "bulk_gb_day": 100.0, "store_gb": 50, "retention_days": 30, "retention_days_metrics": 95,
               "bulk_mix": {"logs": 70.0, "k8s_logs": 7.0, "metrics": 25.0, "traces": 5.0}}
        blk = apply_diagnostics(acc, [row])["sizing"]["consolidated"]["observability"]
        self.assertEqual((blk["logs_gb_day"], blk["metrics_gb_day"], blk["traces_gb_day"]), (70.0, 25.0, 5.0))
        self.assertEqual((blk["k8s_logs_share"], blk["mix_basis"]), (0.1, "measured"))
        self.assertEqual((blk["retention_months"], blk["retention_months_metrics"]), (12, 12))   # every estimate is one year, measured ages aside
        old = apply_diagnostics(acc, [{"name": "a", "bulk_gb_day": 100.0, "store_gb": 50, "retention_days": 30}])
        blk = old["sizing"]["consolidated"]["observability"]
        self.assertIsNone(blk["mix_basis"])
        self.assertIn("not given a split", " ".join(old["sizing"]["notes"]))
        self.assertEqual(blk["retention_months"], 12)

    def test_metered_basis_writes_option_1_and_leaves_option_2_at_zero(self):
        from size_serverless import observability_input_cells
        mix = {"basis": "measured", "logs_gb_day": 900.0, "metrics_gb_day": 60.0, "traces_gb_day": 40.0, "k8s_logs_share": 0.05}
        cells = observability_input_cells({}, "Premium", "r", 2, 1000.0, mix=mix, retention_metrics=4, basis="metered")
        # The sheet turns each back into GB/day with ÷ 730 × 24: logs and traces on G19, TSDS metrics on G20.
        self.assertAlmostEqual(cells["G19"] / 730 * 24, 940.0, places=3)
        self.assertAlmostEqual(cells["G20"] / 730 * 24, 60.0, places=3)
        self.assertEqual((cells["G23"], cells["G24"], cells["G25"], cells["G26"]), (0, "0%", 0, 0))
        self.assertEqual((cells["G11"], cells["G12"]), (2, 4))
        with self.assertRaises(ValueError):
            observability_input_cells({}, "Premium", "r", 2, 1000.0, basis="metered")

    def test_raw_basis_keeps_option_2_and_folds_traces_into_logs(self):
        from size_serverless import observability_input_cells
        mix = {"basis": "measured", "logs_gb_day": 900.0, "metrics_gb_day": 60.0, "traces_gb_day": 40.0, "k8s_logs_share": 0.05}
        folded = observability_input_cells({}, "Premium", "r", 2, 1000.0, mix=mix)
        self.assertEqual((folded["G19"], folded["G20"], folded["G23"], folded["G25"], folded["G26"], folded["G12"]), (0, 0, 940.0, 60.0, 0, 2))
        self.assertAlmostEqual(folded["G24"], round(45 / 940, 4))    # the same Kubernetes GB over a larger logs row
        with self.assertRaises(ValueError):
            observability_input_cells({}, "Premium", "r", 2, 1000.0)

    def test_basis_follows_the_ingest_source(self):
        from size_serverless import ingest_basis
        self.assertEqual(ingest_basis({"sizing": {"ingest_source": "diagnostics"}}), "metered")
        self.assertEqual(ingest_basis({"sizing": {"ingest_source": "k360"}}), "raw")
        self.assertEqual(ingest_basis({}), "raw")

    def test_metered_echo_is_the_two_ladder_inputs_and_raw_echo_is_g28(self):
        from quote_checks import echo_gate, ingest_echo, observability_echo_gate, observability_readback
        rb = {"total_raw_gb_day": 0, "metered_gb_day": 0, "metered_ex_metrics_gb_day": 940.0, "metered_metrics_gb_day": 60.0}
        metered = observability_readback(rb, "metered")
        self.assertEqual((metered["echo_gb_day"], metered["metered_gb_day"], metered["total_raw_gb_day"]), (1000.0, 1000.0, None))
        self.assertTrue(echo_gate(1000.0, ingest_echo(metered))["accepted"])
        mix = {"logs_gb_day": 900.0, "traces_gb_day": 40.0, "metrics_gb_day": 60.0}
        matched = observability_echo_gate(mix, metered)
        self.assertTrue(matched["accepted"])
        self.assertEqual((matched["written_logs_traces"], matched["written_metrics"], matched["sheet_logs_traces"], matched["sheet_metrics"]), (940.0, 60.0, 940.0, 60.0))
        swapped = observability_echo_gate(mix, {"metered_ex_metrics_gb_day": 60.0, "metered_metrics_gb_day": 940.0})
        self.assertFalse(swapped["accepted"])
        self.assertEqual(swapped["written_bulk"], swapped["sheet_gb_day"])
        blank = observability_echo_gate(mix, {"metered_ex_metrics_gb_day": 940.0, "metered_metrics_gb_day": None})
        self.assertFalse(blank["accepted"])
        self.assertFalse(echo_gate(1000.0, ingest_echo(observability_readback({**rb, "metered_metrics_gb_day": None}, "metered")))["accepted"])
        raw = observability_readback({"total_raw_gb_day": 10.2, "metered_gb_day": 16.3}, "raw")
        self.assertEqual((raw["echo_gb_day"], raw["metered_gb_day"]), (10.2, 16.3))
        # A read-back saved before the echo field existed still gates on the raw total.
        self.assertEqual(ingest_echo({"total_raw_gb_day": 7}), 7)

    def test_a_swapped_observability_mix_is_not_quoted(self):
        from quote_checks import observability_echo_gate
        acc = _quote_account(g28=10, e73=0)
        acc["sizing"]["estimators"]["observability"]["echo_gate"] = observability_echo_gate(
            {"logs_gb_day": 9, "metrics_gb_day": 1},
            {"ingest_basis": "metered", "metered_ex_metrics_gb_day": 1, "metered_metrics_gb_day": 9},
        )
        acc["sizing"]["estimators"]["observability"]["readback"]["ingest_basis"] = "metered"
        cons = build_consolidation(acc, None)
        observability = cons["serverless"]["by_solution"]["observability"]
        self.assertTrue(observability["pricing"].startswith("rejected"))
        self.assertEqual(observability["total_mo"], 0)

    def test_bars_carry_the_cover_eis_and_ml_leaves_the_ech_side(self):
        from consolidation import per_cluster_plan
        acc = _quote_account(g28=10, e73=0)
        acc["ecu"]["monthly_avg_complete_months"] = 100
        acc["solutions"]["by_deployment"] = [
            {"name": "ech-search", "solution": "search", "platform": "ech", "ecu": 10, "gb": 50, "ingest_source": "diagnostics"},
            {"name": "logs", "solution": "observability", "platform": "ech", "ecu": 10, "gb": 20, "daily_raw_gb": 10, "ingest_source": "diagnostics"},
        ]
        acc["sizing"]["deployment_plan"] = {"bars": [
            {"label": "Ceiling", "projects": 2, "monthly": 100, "groups": [
                {"names": ["ech-search"], "monthly": 40},
                {"names": ["logs"], "monthly": 60},
            ]},
            {"label": "Floor", "projects": 2, "monthly": 90, "groups": [
                {"names": ["ech-search"], "monthly": 30},
                {"names": ["logs"], "monthly": 60},
            ]},
        ]}
        _stamp_search_eis(acc, "ech-search", eis=10, tokens=1, ml=0.1)
        for name in ("ech-search", "logs"):
            acc["ecu"]["daily_deployment_costs"]["by_deployment"][name] = [1.0]
        acc["sizing"]["quote_options"] = {"ech_scope": "all", "keep_serverless_spend": True, "min_priced_ecu_pct": 90, "unmeasured": "exclude"}
        acc["sizing"]["consolidated"]["search"] = {"stored_gb": 50, "daily_raw_gb": 0, "deployments": 1}
        acc["sizing"]["estimators"]["search"] = {"readback": {
            "monthly_steady_state": 50.0, "annual_year1": 600.0, "annual_steady_state": 600.0,
            "search_monthly": 30.0, "ingest_monthly": 10.0, "storage_monthly": 3.48, "support_monthly": 6.52,
        }}
        cons = build_consolidation(acc, None)
        search = cons["serverless"]["by_solution"]["search"]
        eis = search["eis_with_support_mo"]
        bars = acc["sizing"]["deployment_plan"]["bars"]
        self.assertAlmostEqual(bars[0]["monthly"], 100 + eis)
        self.assertAlmostEqual(bars[1]["monthly"], 90 + eis)
        self.assertAlmostEqual(bars[0]["groups"][0]["eis_with_support_mo"], eis)
        self.assertIsNone(bars[0]["groups"][1].get("eis_with_support_mo"))
        cluster = per_cluster_plan(acc["sizing"], acc, None)
        sized = [row for row in cluster["rows"] if row["status"] == "sized"]
        for index, bar in enumerate(bars):
            column = sum(float((row["steps"][index] or {}).get("monthly") or 0) for row in sized)
            column += cluster["unallocated"][index]
            self.assertAlmostEqual(column, bar["monthly"], places=2)
        ml = search["ml_node_ecu_mo"]
        self.assertAlmostEqual(search["total_mo"], round(50 + eis, 2))
        self.assertAlmostEqual(cons["savings"]["ech_ml_excluded_mo"], ml)
        self.assertAlmostEqual(cons["savings"]["ech_list_mo"], round(cons["ech_scope"]["in_comparison_mo"] - ml, 2))
        self.assertIn("ML-node ECU", " ".join(cons["notes"]))

    def _ml_only_fleet(self, *, ml=12.0, scope="all"):
        """Observability-only fleet: Search is never quoted, and an ML node bills on it."""
        acc = _quote_account(g28=10, e73=0)
        acc["ecu"]["monthly_avg_complete_months"] = 100
        acc["solutions"]["by_deployment"] = [
            {"name": "logs", "solution": "observability", "platform": "ech", "ecu": 10, "gb": 20,
             "daily_raw_gb": 10, "ingest_source": "diagnostics"},
        ]
        _stamp_search_eis(acc, "logs", eis=0.0, tokens=0.0, ml=ml)
        # One billed day of $100, so the ML exclusion is a slice of the bill and not more than it.
        acc["ecu"]["daily_totals"] = [100.0]
        acc["ecu"]["daily_deployment_costs"]["org_daily_totals"] = [100.0]
        acc["ecu"]["daily_deployment_costs"]["by_deployment"] = {"logs": [100.0]}
        acc["sizing"]["quote_options"] = {"ech_scope": scope, "keep_serverless_spend": True,
                                          "min_priced_ecu_pct": 0, "unmeasured": "exclude"}
        acc["sizing"]["consolidated"]["search"] = {"stored_gb": 0, "daily_raw_gb": 0, "deployments": 0}
        acc["sizing"]["estimators"]["search"] = {"status": "skipped — no search workload detected"}
        return acc

    def test_ml_leaves_the_ech_side_when_search_was_never_quoted(self):
        acc = self._ml_only_fleet(ml=12.0)
        cons = build_consolidation(acc, None)
        search = cons["serverless"]["by_solution"]["search"]
        self.assertNotIn("quotable", str(search.get("pricing") or ""))
        exclusion = cons["ech_ml_exclusion"]
        self.assertEqual(exclusion["status"], "complete")
        self.assertEqual(exclusion["deployments"], 1)
        self.assertGreater(exclusion["monthly"], 0)
        self.assertEqual(cons["savings"]["ech_ml_excluded_mo"], exclusion["monthly"])
        self.assertEqual(
            cons["savings"]["ech_list_mo"],
            round(cons["ech_scope"]["in_comparison_mo"] - exclusion["monthly"], 2),
        )
        self.assertIn("not only the ones Search priced", " ".join(cons["notes"]))

    def test_an_unverified_ml_read_excludes_nothing_and_says_so(self):
        acc = self._ml_only_fleet(ml=12.0)
        # The authoritative total is non-zero and the daily series does not cover the window.
        acc["ai_ml"]["daily_usage_by_deployment"]["logs"]["days"] = ["2020-01-01"]
        cons = build_consolidation(acc, None)
        self.assertEqual(cons["ech_ml_exclusion"]["status"], "incomplete")
        self.assertIsNone(cons["ech_ml_exclusion"]["monthly"])
        self.assertNotIn("ech_ml_excluded_mo", cons["savings"])
        self.assertEqual(cons["savings"]["ech_list_mo"], cons["ech_scope"]["in_comparison_mo"])
        self.assertIn("saving is understated", " ".join(cons["notes"]))
        codes = [row.get("code") for row in acc["sizing"]["diagnostics"]["estimation_limits"]]
        self.assertIn("ml_node_ecu_unverified", codes)

    def test_a_deployment_with_no_ai_ml_billing_is_a_zero_not_missing_data(self):
        from consolidation_eis import ml_node_ecu
        acc = self._ml_only_fleet(ml=12.0)
        acc["solutions"]["by_deployment"].append(
            {"name": "quiet", "solution": "observability", "platform": "ech", "ecu": 5, "gb": 5,
             "daily_raw_gb": 1, "ingest_source": "diagnostics"}
        )
        acc["ai_ml"]["billing_by_deployment"]["quiet"] = {"eis_ecu": 0.0, "million_tokens": 0.0, "ml_node_ecu": 0.0}
        read = ml_node_ecu(acc, {"logs", "quiet"})
        self.assertEqual(read["status"], "complete")
        self.assertEqual(read["deployments"], 1)
        self.assertEqual(list(read["by_deployment_mo"]), ["logs"])

    def test_the_comparison_roster_follows_the_ech_scope(self):
        from consolidation_scope import comparison_deployment_names
        scope = {
            "status": "complete", "scope": "priced",
            "by_deployment_mo": {"a": 10.0, "b": 20.0, "c": 30.0},
            "unpriced": [{"name": "c", "reason": "no diagnostic bundle"}],
        }
        self.assertEqual(comparison_deployment_names(scope), {"a", "b"})
        self.assertEqual(comparison_deployment_names({**scope, "scope": "all"}), {"a", "b", "c"})
        self.assertEqual(comparison_deployment_names({"status": "unavailable"}), set())
        self.assertEqual(comparison_deployment_names(None), set())

    def test_security_g4_is_measured_gb_over_the_sheet_expansion(self):
        import sizing_fill
        import size_serverless as ss
        cells = ss.security_input_cells({}, "Premium", "r", 12, 166.0, expansion=1.66)
        self.assertEqual(cells["G4"], 100.0)
        self.assertEqual(ss.security_input_cells({}, "Premium", "r", 12, 166.0)["G4"], 166.0)
        reads = iter([
            {"raw": 100, "metered": 166, "reference": 1.66},     # expansion: G5 ÷ G4
            {"monthly_steady_state": 115.0, "cps_addon_monthly": 0, "ingest_monthly": 70.0, "retention_monthly": 30.0,
             "support_monthly": 15.0, "steady_total": 115.0, "metered_gb_day": 250.0},
        ])
        with mock.patch.object(sizing_fill, "read_cells", side_effect=lambda *_a, **_k: next(reads)), \
             mock.patch.object(sizing_fill, "clear_security_addons", return_value={}), \
             mock.patch.object(sizing_fill, "push_cells", return_value=6) as pushed:
            cells, n, rb, gate = ss.fill_security(None, "copy", {}, "Premium", "r", 12, 250.0, [], "metered")
        self.assertAlmostEqual(cells["G4"], round(250.0 / 1.66, 4))
        self.assertEqual(pushed.call_args.args[3]["G4"], cells["G4"])
        self.assertTrue(gate["accepted"])
        self.assertEqual((rb["expansion"], rb["echo_gb_day"], rb["monthly_steady_state"]), (1.66, 250.0, 115.0))
        # G5 does not come back as the GB/day measured: the monthly is cleared.
        reads = iter([
            {"raw": 0, "metered": 0, "reference": 1.66},         # G4 was 0 on the copy: the reference cell is used
            {"monthly_steady_state": 115.0, "cps_addon_monthly": 0, "metered_gb_day": 400.0},
        ])
        with mock.patch.object(sizing_fill, "read_cells", side_effect=lambda *_a, **_k: next(reads)), \
             mock.patch.object(sizing_fill, "clear_security_addons", return_value={}), \
             mock.patch.object(sizing_fill, "push_cells", return_value=6):
            _cells, _n, rb, gate = ss.fill_security(None, "copy", {}, "Premium", "r", 12, 250.0, [], "metered")
        self.assertFalse(gate["accepted"])
        self.assertIsNone(rb["monthly_steady_state"])
        with mock.patch.object(sizing_fill, "read_cells", return_value={"raw": 0, "metered": 0, "reference": None}):
            with self.assertRaises(RuntimeError):
                ss.security_expansion(None, "copy")

    def test_ladder_gate_and_band_walk_apply_no_expansion_to_metered_bulk(self):
        from consolidation import observability_metered_rates
        raw = observability_metered_rates([{"name": "a", "daily_raw_gb": 10.0}])[0]
        metered = observability_metered_rates([{"name": "a", "daily_raw_gb": 10.0}], 1.0)[0]
        self.assertEqual((raw["metered_gb_day"], metered["metered_gb_day"]), (15.95, 10.0))
        self.assertIsNone(metered["usd_per_gb"])
        metrics = observability_metered_rates([{"name": "m", "daily_raw_gb": 100.0, "logs_gb_day": 0, "metrics_gb_day": 100, "traces_gb_day": 0}], 1.0)[0]
        mostly_logs = observability_metered_rates([{"name": "l", "daily_raw_gb": 100.0, "logs_gb_day": 90, "metrics_gb_day": 10, "traces_gb_day": 0}], 1.0)[0]
        self.assertNotEqual(metrics["usd_per_gb"], mostly_logs["usd_per_gb"])


class UnmeasuredDeploymentTests(unittest.TestCase):
    """An active deployment with no bundle: on neither side, priced from K360 and labelled, or a stop."""

    def _acc(self) -> dict:
        return {
            "deployments": {"active": {"month": "2026-10", "names": ["measured", "nobundle", "search-nb", "retired"]}},
            "solutions": {"by_deployment": [
                {"name": "measured", "solution": "observability", "platform": "ech", "gb": 50, "ecu": 10},
                {"name": "nobundle", "solution": "observability", "platform": "ech", "gb": 912.48, "ecu": 10},
                {"name": "search-nb", "solution": "search", "platform": "ech", "gb": 300, "ecu": 10},
                {"name": "retired", "solution": "observability", "platform": "ech", "gb": 700, "ecu": 10},
            ], "totals": {}},
            "sizing": {"consolidated": {}}, "storage": {},
        }

    ROWS = [{"name": "measured", "bulk_gb_day": 20.0, "store_gb": 40.0, "retention_days": 30.416}]
    ADMIN = {"measured", "nobundle", "search-nb"}

    def test_exclude_sizes_nothing_for_it(self):
        out = apply_diagnostics(self._acc(), self.ROWS, self.ADMIN)
        by = {d["name"]: d for d in out["solutions"]["by_deployment"]}
        self.assertEqual((by["nobundle"]["gb"], by["nobundle"]["daily_raw_gb"], by["nobundle"]["k360_gb"]), (0.0, 0.0, 912.48))
        self.assertEqual((by["search-nb"]["gb"], by["search-nb"]["ingest_source"]), (0.0, "active_no_bundle"))
        self.assertEqual((by["retired"]["gb"], by["retired"]["ingest_source"], by["retired"]["k360_gb"]), (0.0, "decommissioned", 700))
        con = out["sizing"]["consolidated"]
        self.assertEqual(con["observability"]["daily_raw_gb"], 20.0)
        self.assertEqual(con["search"]["stored_gb"], 0.0)
        cov = out["sizing"]["diagnostics"]
        self.assertEqual(cov["unmeasured_mode"], "exclude")
        self.assertEqual(cov["missing_diagnostics"], 2)
        self.assertEqual(sorted(cov["fallback_names"]), ["nobundle", "search-nb"])

    def test_k360_unmeasured_mode_is_refused(self):
        with self.assertRaises(ValueError):
            apply_diagnostics(self._acc(), self.ROWS, self.ADMIN, unmeasured="k360")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            apply_diagnostics(self._acc(), self.ROWS, self.ADMIN, unmeasured="guess")


class BulkCellTests(unittest.TestCase):
    def test_bulk_shows_up_to_two_decimals(self):
        from render_pdf import bulk_cell
        self.assertEqual([bulk_cell(v) for v in (19455.7, 282, 12.5, 0.1, 0.008, 0.001, 0.0049, 0, None)],
                         ["19,455.7", "282", "12.5", "0.1", "0.01", "<0.01", "<0.01", "0", "0"])
        self.assertEqual(bulk_cell(1.234), "1.23")


if __name__ == "__main__":
    unittest.main()
