"""Search estimator sheet tests. No Google and no sheets."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import search_sheet  # noqa: E402
import sizing_shared  # noqa: E402
from estimator_layout import (  # noqa: E402
    GOLDEN_SEARCH, TEMPLATE_ANCHORS, is_golden_template, layout_recorded, template_check,
)

OPTIONS = [
    "AWS: us-east-1 (N. Virginia)", "AWS: eu-west-1 (Ireland)",
    "Azure: eastus (Virginia)", "GCP: us-east1 (South Carolina)",
]

WORKLOAD = {
    "quote_window_telemetry": {"status": "complete"},
    "ingest": {"docs_per_sec": 95, "avg_doc_bytes": 1024, "active_hours_mo": 24},
    "search": {
        "peak": {"qps": 30, "complexity_ms": 40, "pct_hours_mo": 6},
        "nonpeak": {"qps": 10, "complexity_ms": 700, "pct_hours_mo": 68},
    },
}


def payload(**over) -> dict:
    base = {
        "stored_gb": 3000, "org_stored_gb": 3000, "provider": "aws", "region": "us-east-1",
        "support_tier": "Premium", "workload": WORKLOAD,
    }
    base.update(over)
    return base


def sheet_values(monthly=1000.0) -> dict:
    values = {"region_valid": "Yes", "F55": 3000, "F62": 4, "F65": 96}
    for col, factor in (("F", 0.5), ("G", 1.0), ("H", 2.0)):
        values.update({
            f"{col}89": 8 * factor, f"{col}96": 2 * factor, f"{col}98": 5000 * factor,
            f"{col}103": 600 * factor, f"{col}104": 20, f"{col}105": 141, f"{col}106": 0,
            f"{col}107": 0, f"{col}109": 100 * factor, f"{col}110": monthly * factor,
        })
    return values


class LayoutTests(unittest.TestCase):
    def test_search_layout_is_recorded_and_the_org_report_es3_layout_is_not(self):
        self.assertTrue(layout_recorded("search"))
        self.assertFalse(layout_recorded("es3"))

    def test_the_search_template_is_never_written(self):
        self.assertTrue(is_golden_template(GOLDEN_SEARCH))
        self.assertEqual(sizing_shared.DEFAULT_SEARCH, GOLDEN_SEARCH)

    def test_a_moved_label_is_a_problem(self):
        spec = TEMPLATE_ANCHORS["search"]["labels"]
        good = {cell: want for cell, want in spec.items()}
        self.assertEqual(template_check("search", good, None)["problems"], [])
        bad = dict(good, B19="[PEAK] Something else")
        self.assertEqual(len(template_check("search", bad, None)["problems"]), 1)

    def test_readback_cells_are_all_on_the_sheet_layout(self):
        self.assertEqual(sizing_shared.SEARCH_READBACK["G110"], "G110")
        self.assertEqual(sizing_shared.SEARCH_READBACK["region_valid"], "C53")


class InputCellTests(unittest.TestCase):
    def cells(self, **over):
        notes: list[str] = []
        cells = search_sheet.search_input_cells(payload(**over), "us-east-1 (N. Virginia)", "AWS", "Enterprise", notes)
        return cells, notes

    def test_every_input_is_written_including_zeros(self):
        cells, _ = self.cells()
        for cell in ("C22", "C27", "C30", "C37", "C40", "C45"):
            self.assertEqual(cells[cell], 0)
        self.assertEqual(cells["C12"], 3000)
        self.assertEqual(cells["C48"], "No")
        self.assertEqual(cells["C8"], "text")

    def test_hours_become_shares_of_the_730_hour_month(self):
        cells, _ = self.cells()
        self.assertAlmostEqual(cells["C21"], 0.06, places=3)
        self.assertAlmostEqual(cells["C26"], 0.68, places=3)
        self.assertLessEqual(cells["C21"] + cells["C26"], 1.0)

    def test_measured_query_time_follows_the_documented_thresholds_and_says_so(self):
        cells, notes = self.cells()
        self.assertEqual(cells["C20"], "simple")
        self.assertEqual(cells["C25"], "complex")
        self.assertTrue(any("40 ms is 'simple'" in note for note in notes))

    def test_ingest_cells_carry_the_measured_rate(self):
        cells, _ = self.cells()
        self.assertEqual((cells["C33"], cells["C34"], cells["C35"]), (95.0, 1024.0, 24.0))

    def test_a_small_project_takes_its_share_of_org_telemetry(self):
        cells, notes = self.cells(stored_gb=300, org_stored_gb=3000)
        self.assertAlmostEqual(cells["C19"], 3.0)
        self.assertAlmostEqual(cells["C33"], 9.5)
        self.assertTrue(any("share of diagnostic org store" in note for note in notes))

    def test_no_search_queries_leaves_zero_qps_not_the_template_default(self):
        wl = {k: v for k, v in WORKLOAD.items() if k != "search"}
        cells, _ = self.cells(workload=wl)
        self.assertEqual((cells["C19"], cells["C21"], cells["C24"], cells["C26"]), (0.0, 0.0, 0.0, 0.0))

    def test_missing_telemetry_is_an_error_not_a_guess(self):
        wl = dict(WORKLOAD, quote_window_telemetry={"status": "missing"})
        with self.assertRaises(ValueError):
            self.cells(workload=wl)


class ReadbackTests(unittest.TestCase):
    def test_performant_is_the_quote_and_the_other_profiles_ride_beside_it(self):
        rows = search_sheet.profile_rows(sheet_values(1000))
        self.assertEqual([r["Search Power Setting"] for r in rows], ["on-demand", "performant", "high availability"])
        rb = search_sheet.readback_from(rows)
        self.assertEqual(rb["monthly_steady_state"], 1000.0)
        self.assertEqual(rb["annual_year1"], 12000.0)
        self.assertEqual(rb["provisioned_search_vcu"], 8)
        self.assertEqual(rb["profile"], "performant")

    def test_a_non_numeric_performant_total_is_no_quote(self):
        values = sheet_values()
        values["G110"] = "#N/A"
        self.assertIsNone(search_sheet.readback_from(search_sheet.profile_rows(values)))


class FillTests(unittest.TestCase):
    def fill(self, values=None, **over):
        pushed = {}

        def fake_push(_sheets, _sid, tab, cells, *, allowed_text=None):
            pushed.update(cells)
            pushed["_tab"] = tab
            pushed["_allowed"] = allowed_text
            return len(cells)

        with mock.patch.object(search_sheet, "push_cells", side_effect=fake_push), \
             mock.patch.object(search_sheet, "read_cells", return_value=values or sheet_values()):
            result = search_sheet.fill_search(object(), "copy-id", payload(**over), OPTIONS)
        return result, pushed

    def test_writes_the_matched_region_text_and_quotes_performant(self):
        result, pushed = self.fill(provider="azure", region="eastus")
        self.assertEqual(pushed["C5"], "Azure")
        self.assertEqual(pushed["C6"], "eastus (Virginia)")
        self.assertEqual(pushed["_tab"], "Estimator")
        self.assertIn("eastus (Virginia)", pushed["_allowed"])
        self.assertEqual(result["readback"]["monthly_steady_state"], 1000.0)
        self.assertIn("exact", result["region_match"])
        self.assertEqual(result["support_level"], "Enterprise")

    def test_an_unlisted_region_is_priced_in_the_provider_default_and_says_so(self):
        result, pushed = self.fill(region="ap-nowhere-1")
        self.assertEqual(pushed["C6"], "us-east-1 (N. Virginia)")
        self.assertIn("provider_default", result["region_match"])
        self.assertTrue(any("is not on the estimator list" in note for note in result["assumptions"]))

    def test_a_region_the_sheet_calls_invalid_is_rejected(self):
        values = sheet_values()
        values["region_valid"] = "No"
        result, _ = self.fill(values)
        self.assertEqual(result["status"], "error")
        self.assertIn("not on the estimator list", result["error"])

    def test_an_unknown_provider_is_rejected_before_any_write(self):
        result, pushed = self.fill(provider="oracle")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("C5", pushed)


class DispatchTests(unittest.TestCase):
    def test_notebook_flag_runs_the_library_and_publishes_its_csv(self):
        lib = {"status": "estimated", "readback": {"monthly_steady_state": 5}, "report_csv": "a,b"}
        with mock.patch.object(search_sheet, "run_es3", return_value=lib) as run, \
             mock.patch.object(search_sheet, "write_search_sheet", return_value={"url": "u"}) as publish, \
             mock.patch.object(search_sheet, "copy_verified") as copy:
            result, published = search_sheet.price_search(
                "drive", "sheets", "folder", HERE, "t", payload(), notebook=True, template_id="x", reuse=True, skip_xlsx=True,
            )
        run.assert_called_once()
        publish.assert_called_once()
        copy.assert_not_called()
        self.assertEqual(published, {"url": "u"})
        self.assertEqual(result["readback"]["monthly_steady_state"], 5)

    def test_default_copies_the_search_template_and_never_runs_the_library(self):
        copied = {"id": "copy-id", "url": "https://sheet"}
        with mock.patch.object(search_sheet, "run_es3") as run, \
             mock.patch.object(search_sheet, "copy_verified", return_value=(copied, False, {"version": None})) as copy, \
             mock.patch.object(search_sheet, "search_region_options", return_value=OPTIONS), \
             mock.patch.object(search_sheet, "push_cells", return_value=30), \
             mock.patch.object(search_sheet, "read_cells", return_value=sheet_values(777)):
            result, published = search_sheet.price_search(
                "drive", "sheets", "folder", HERE, "t", payload(), notebook=False, template_id="tmpl", reuse=True, skip_xlsx=True,
            )
        run.assert_not_called()
        self.assertEqual(copy.call_args.args[2], "tmpl")
        self.assertEqual(copy.call_args.kwargs["kind"], "search")
        self.assertEqual(published["url"], "https://sheet")
        self.assertEqual(result["readback"]["monthly_steady_state"], 777.0)

    def test_a_rejected_region_trashes_the_new_copy_and_publishes_nothing(self):
        copied = {"id": "copy-id", "url": "https://sheet"}
        with mock.patch.object(search_sheet, "copy_verified", return_value=(copied, False, {"version": None})), \
             mock.patch.object(search_sheet, "search_region_options", return_value=OPTIONS), \
             mock.patch.object(search_sheet, "trash_file") as trash:
            result, published = search_sheet.price_search(
                "drive", "sheets", "folder", HERE, "t", payload(provider="oracle"), notebook=False, template_id="tmpl",
                reuse=True, skip_xlsx=True,
            )
        trash.assert_called_once_with("drive", "copy-id")
        self.assertEqual(result["status"], "error")
        self.assertEqual(published, {})


if __name__ == "__main__":
    unittest.main()
