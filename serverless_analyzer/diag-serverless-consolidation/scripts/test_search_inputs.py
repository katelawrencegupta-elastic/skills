import unittest
from datetime import datetime, timedelta, timezone

import proxy_logs as pl
import search_inputs as si

A = "a" * 32
B = "b" * 32
C = "c" * 32


def acc():
    return {
        "cloud": {"by_deployment": [
            {"name": "s-us-1", "region": "us-central1"}, {"name": "s-us-2", "region": "us-central1"},
            {"name": "s-eu", "region": "europe-west1"}, {"name": "o11y", "region": "us-central1"},
        ]},
        "solutions": {"by_deployment": [
            {"name": "s-us-1", "solution": "search", "deployment_id": A, "daily_raw_gb": 0.6, "store_bytes": 600_000_000, "docs_count": 300_000},
            {"name": "s-us-2", "solution": "search", "deployment_id": B, "daily_raw_gb": 0.4, "store_bytes": 400_000_000, "docs_count": 200_000},
            {"name": "s-eu", "solution": "search", "deployment_id": C, "daily_raw_gb": 0.0, "store_bytes": 100, "docs_count": 10},
            {"name": "o11y", "solution": "observability", "deployment_id": "d" * 32, "daily_raw_gb": 40, "store_bytes": 1, "docs_count": 1},
            {"name": "unmeasured", "solution": "search", "daily_raw_gb": 1},
        ]},
    }


def fake_run(base, key, query):
    day = datetime.fromisoformat(query.split('@timestamp >= "')[1][:10]).replace(tzinfo=timezone.utc)
    if C in query:
        return {"columns": [{"name": "hour"}], "values": []}
    cols = [{"name": n} for n in ("hour", "search_requests", "peak_minute_search_qps", "search_latency_p90_ms", "search_latency_p99_ms")]
    rows = []
    for h in range(24):
        hr = day + timedelta(hours=h)
        peak = pl.window_of(hr) == "peak"
        rows.append([hr.strftime("%Y-%m-%dT%H:00:00.000Z"), 7200 if peak else 3600, 3.0 if peak else 1.5, 40.0, 80.0])
    return {"columns": cols, "values": rows}


class RegionRowTests(unittest.TestCase):
    def test_only_measured_search_deployments_group_by_region(self):
        rows = si.search_region_rows(acc())
        self.assertEqual(set(rows), {"us-central1", "europe-west1"})
        self.assertEqual(rows["us-central1"]["ids"], [A, B])
        self.assertAlmostEqual(rows["us-central1"]["daily_raw_gb"], 1.0)
        self.assertEqual(rows["us-central1"]["docs_count"], 500_000)

    def test_ingest_is_the_measured_bulk_rate_over_730_hours(self):
        ing = si.ingest_from_diagnostics(si.search_region_rows(acc())["us-central1"])
        self.assertAlmostEqual(ing["bytes_per_sec"], 1e9 / 86400, places=1)
        self.assertEqual(ing["avg_doc_bytes"], 2000.0)
        self.assertAlmostEqual(ing["docs_per_sec"] * ing["avg_doc_bytes"], ing["bytes_per_sec"], places=1)
        self.assertEqual(ing["active_hours_mo"], 730)

    def test_no_bulk_is_refused_not_invented(self):
        with self.assertRaises(ValueError):
            si.ingest_from_diagnostics(si.search_region_rows(acc())["europe-west1"])


class WorkloadTests(unittest.TestCase):
    now = datetime(2026, 11, 23, 9, tzinfo=timezone.utc)

    def test_each_region_gets_its_own_exact_block_and_a_failed_region_is_named(self):
        wl = si.regional_workloads(acc(), "https://x", "k", days=7, now=self.now, run=fake_run, workers=1)
        us = wl["regions"]["us-central1"]
        self.assertEqual(us["quote_window_telemetry"]["status"], "complete")
        self.assertEqual(us["search"]["peak"]["qps"], 3.0)
        self.assertEqual(us["ingest"]["active_hours_mo"], 730)
        eu = wl["regions"]["europe-west1"]
        self.assertEqual(eu["quote_window_telemetry"]["status"], "incomplete")
        self.assertIn("europe-west1", wl["errors"])
        self.assertEqual(wl["quote_window_telemetry"]["status"], "complete")  # one region is enough for the org sum
        self.assertEqual(wl["search"]["peak"]["qps"], 3.0)

    def test_exact_inputs_make_the_share_one(self):
        wl = si.regional_workloads(acc(), "https://x", "k", days=7, now=self.now, run=fake_run, workers=1)
        block, org = si.exact_inputs(wl, "us-central1", 1.5, 90.0)
        self.assertIs(block, wl["regions"]["us-central1"])
        self.assertEqual(org, 1.5)

    def test_without_a_block_the_org_workload_and_store_pass_through(self):
        legacy = {"search": {"peak": {"qps": 5}}}
        self.assertEqual(si.exact_inputs(legacy, "us-central1", 1.5, 90.0), (legacy, 90.0))

    def test_a_region_idle_in_one_band_still_merges(self):
        def run(base, key, query):
            day = datetime.fromisoformat(query.split('@timestamp >= "')[1][:10]).replace(tzinfo=timezone.utc)
            cols = [{"name": n} for n in ("hour", "search_requests", "peak_minute_search_qps", "search_latency_p90_ms", "search_latency_p99_ms")]
            rows = []
            for h in range(24):
                hr = day + timedelta(hours=h)
                peak = pl.window_of(hr) == "peak"
                if C in query and not peak:
                    continue  # the EU region only has peak-window traffic
                rows.append([hr.strftime("%Y-%m-%dT%H:00:00.000Z"), 7200 if peak else 3600, 3.0 if peak else 1.5, 40.0, 80.0])
            return {"columns": cols, "values": rows}

        a = acc()
        a["solutions"]["by_deployment"][2]["daily_raw_gb"] = 0.5
        wl = si.regional_workloads(a, "https://x", "k", days=7, now=self.now, run=run, workers=1)
        self.assertEqual(wl["regions"]["europe-west1"]["search"]["nonpeak"]["qps"], None)
        self.assertEqual(wl["quote_window_telemetry"]["status"], "complete")
        self.assertEqual(wl["search"]["peak"]["qps"], 6.0)           # both regions add
        self.assertEqual(wl["search"]["nonpeak"]["qps"], 2.0)        # only the US region has it
        self.assertEqual(wl["search"]["nonpeak"]["pct_hours_mo"], 35.0)  # mean of 70 and 0

    def test_every_region_failing_marks_the_org_workload_incomplete(self):
        empty = lambda b, k, q: {"columns": [{"name": "hour"}], "values": []}
        wl = si.regional_workloads(acc(), "https://x", "k", days=1, now=self.now, run=empty, workers=1)
        self.assertEqual(wl["quote_window_telemetry"]["status"], "incomplete")
        self.assertTrue(wl["quote_window_fetch_error"])


if __name__ == "__main__":
    unittest.main()
