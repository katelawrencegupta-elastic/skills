import unittest
from datetime import datetime, timedelta, timezone

import proxy_logs as pl

DEP = "4fcf9890263a9abb93a0068c442286d3"


def hour(day: int, h: int) -> datetime:
    # 2026-11-16 is a Monday
    return datetime(2026, 11, 16 + day, h, tzinfo=timezone.utc)


class SummarizeTests(unittest.TestCase):
    def test_peak_window_is_weekdays_13_to_23_utc(self):
        self.assertEqual(pl.window_of(hour(0, 13)), "peak")
        self.assertEqual(pl.window_of(hour(0, 22)), "peak")
        self.assertEqual(pl.window_of(hour(0, 23)), "non_peak")
        self.assertEqual(pl.window_of(hour(0, 12)), "non_peak")
        self.assertEqual(pl.window_of(hour(5, 15)), "non_peak")  # Saturday

    def test_complexity_thresholds(self):
        self.assertEqual(pl.complexity_of(250), "simple")
        self.assertEqual(pl.complexity_of(250.1), "moderate")
        self.assertEqual(pl.complexity_of(500), "moderate")
        self.assertEqual(pl.complexity_of(501), "complex")
        self.assertIsNone(pl.complexity_of(None))

    def test_window_figures(self):
        rows = []
        for day in range(7):
            for h in range(24):
                peak = pl.window_of(hour(day, h)) == "peak"
                rows.append({"hour": hour(day, h), "search_requests": 36000 if peak else 3600,
                             "peak_minute_search_qps": 12.4 if peak else 2.0,
                             "p90": 300.0 if peak else 40.0, "p99": 900.0 if peak else 80.0})
        out = pl.summarize(rows)
        self.assertEqual(out["peak"]["hours"], 50)
        self.assertEqual(out["peak"]["pct_of_hours"], 30)
        self.assertEqual(out["non_peak"]["pct_of_hours"], 70)
        self.assertEqual(out["peak"]["recommended_qps"], 13)       # ceil of p95 peak-minute qps
        self.assertEqual(out["peak"]["avg_qps"], 10.0)
        self.assertEqual(out["peak"]["query_complexity"], "moderate")
        self.assertEqual(out["non_peak"]["query_complexity"], "simple")
        self.assertEqual(out["non_peak"]["recommended_qps"], 2)

    def test_hours_with_no_traffic_are_idle_not_dropped(self):
        # Weekday traffic 08:00-23:00 UTC for a week. Only those 75 hours are rows.
        rows = []
        for day in range(5):
            for h in range(8, 23):
                peak = pl.window_of(hour(day, h)) == "peak"
                rows.append({"hour": hour(day, h), "search_requests": 36000 if peak else 3600,
                             "peak_minute_search_qps": 12.4 if peak else 2.0, "p90": 100.0, "p99": 200.0})
        out = pl.summarize(rows, total_hours=168)
        self.assertEqual(out["peak"]["pct_of_hours"], 30)       # 50 of 168, not 50 of 75
        self.assertEqual(out["non_peak"]["pct_of_hours"], 15)   # 25 of 168
        self.assertEqual(out["non_peak"]["recommended_qps"], 2)  # percentiles are over active hours
        self.assertEqual(out["non_peak"]["hours"], 25)

    def test_percentile_interpolates(self):
        self.assertEqual(pl.percentile([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(pl.percentile([5], 95), 5)
        self.assertIsNone(pl.percentile([], 50))


class QueryTests(unittest.TestCase):
    def test_query_filters_by_deployment_window_and_agents(self):
        q = pl.hourly_query([DEP], datetime(2026, 11, 16, tzinfo=timezone.utc))
        self.assertIn(f'handling_deployment IN ("{DEP}")', q)
        self.assertIn('@timestamp >= "2026-11-16T00:00:00Z" AND @timestamp < "2026-11-17T00:00:00Z"', q)
        self.assertIn('request_source == "external"', q)
        self.assertIn('user_agent LIKE "Elastic-Metricbeat*"', q)
        self.assertNotIn("“", q)

    def test_a_non_id_is_refused_before_it_reaches_the_query(self):
        with self.assertRaises(ValueError):
            pl.hourly_query(['x" OR 1==1 //'], datetime(2026, 11, 16, tzinfo=timezone.utc))


class WorkloadTests(unittest.TestCase):
    def fake(self, base, key, query):
        day = query.split('@timestamp >= "')[1][:10]
        d = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        cols = [{"name": n} for n in ("hour", "search_requests", "peak_minute_search_qps", "search_latency_p90_ms", "search_latency_p99_ms")]
        vals = []
        for h in range(24):
            hr = d + timedelta(hours=h)
            peak = pl.window_of(hr) == "peak"
            vals.append([hr.strftime("%Y-%m-%dT%H:00:00.000Z"), 7200 if peak else 3600, 3.0 if peak else 1.5, 30.0, 60.0])
        return {"columns": cols, "values": vals}

    def test_workload_shape_feeds_the_estimators(self):
        now = datetime(2026, 11, 23, 9, tzinfo=timezone.utc)  # Monday
        wl = pl.search_workload("https://x", "k", [DEP], days=7, now=now, run=self.fake)
        self.assertEqual(wl["window_start"], "2026-11-16")
        self.assertEqual(wl["window_end"], "2026-11-22")
        self.assertEqual(wl["sampled_hours"], 168)
        peak, nonpeak = wl["search"]["peak"], wl["search"]["nonpeak"]
        self.assertEqual((peak["qps"], nonpeak["qps"]), (3.0, 2.0))
        self.assertEqual(peak["complexity"], "simple")
        self.assertEqual(peak["complexity_ms"], 30.0)
        self.assertEqual(peak["pct_hours_mo"] + nonpeak["pct_hours_mo"], 100.0)
        self.assertEqual(wl["search"]["idle_pct_hours_mo"], 0.0)

    def test_no_traffic_is_an_error_not_a_zero_quote(self):
        empty = lambda b, k, q: {"columns": [{"name": "hour"}], "values": []}
        with self.assertRaises(RuntimeError):
            pl.search_workload("https://x", "k", [DEP], days=1, run=empty)

    def test_settings_need_both_values_and_https(self):
        with self.assertRaises(ValueError):
            pl.proxy_settings({"PROXY_LOGS_URL": "https://x"})
        with self.assertRaises(ValueError):
            pl.proxy_settings({"PROXY_LOGS_URL": "http://x", "PROXY_LOGS_API_KEY": "k"})


if __name__ == "__main__":
    unittest.main()


class RetryTests(unittest.TestCase):
    def test_a_failing_span_is_split_until_it_reads(self):
        calls = []

        def flaky(base, key, query):
            start = query.split('@timestamp >= "')[1][:13]
            end = query.split('@timestamp < "')[1][:13]
            calls.append((start, end))
            hours = (datetime.fromisoformat(end + ":00") - datetime.fromisoformat(start + ":00")).total_seconds() / 3600
            if hours > 6:
                raise RuntimeError("proxy logs HTTP 502")
            cols = [{"name": n} for n in ("hour", "search_requests", "peak_minute_search_qps", "search_latency_p90_ms", "search_latency_p99_ms")]
            return {"columns": cols, "values": [[f"{start}:00:00.000Z", 3600, 1.0, 20.0, 40.0]]}

        day = datetime(2026, 11, 16, tzinfo=timezone.utc)
        rows = pl._read_span("b", "k", [DEP], day, day + timedelta(days=1), flaky, attempts=1, pause=0)
        self.assertEqual(len(rows), 4)  # 24 h -> 12 h -> 6 h pieces, four of them

    def test_a_single_hour_that_keeps_failing_is_an_error(self):
        def always(base, key, query):
            raise RuntimeError("proxy logs HTTP 502")

        day = datetime(2026, 11, 16, tzinfo=timezone.utc)
        with self.assertRaises(RuntimeError):
            pl._read_span("b", "k", [DEP], day, day + timedelta(hours=1), always, attempts=1, pause=0)
