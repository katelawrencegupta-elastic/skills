"""Operations tests for the diagnostic quote. No Google and no sheets."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(HERE))
from skill_paths import find_skill, scripts_dir  # noqa: E402

K360 = scripts_dir(find_skill("k360-account-analysis", HERE))
sys.path.insert(0, str(K360))

from admin_diags import admin_base, capture_target, capture_url, pull_diagnostics, safe_id  # noqa: E402
from consolidation import build_consolidation  # noqa: E402
from diag_account import apply_diagnostics  # noqa: E402
from k360_infra import es_base_and_key  # noqa: E402
from privacy import seal_dir, seal_text, unignored_secret_warnings  # noqa: E402
from quote_checks import echo_gate, integrity_score  # noqa: E402
from run_consolidation import method_stamp  # noqa: E402
from render_pdf import export_documents, render_consolidation_html, stat  # noqa: E402
from size_serverless import (  # noqa: E402
    DEFAULT_O11Y,
    DEFAULT_SECURITY,
    assemble_plan_bars,
    assert_not_template,
    cell_input_mode,
    copy_template,
    partition_cells,
    plan_sheet_jobs,
    push_cells,
    require_customer_parent,
)
from vpn_check import tunnel_reaches  # noqa: E402

from _test_support import HERE, K360, _SheetCall, _guard_args, _quote_account  # noqa: E402


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

    def test_artifacts_land_outside_the_checkout(self):
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
        self.assertEqual(root, Path.home() / "serverless_analyzer_artifacts")

    def test_env_override_wins_over_the_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["K360_ORG_SKILL_DIR"] = tmp
            try:
                self.assertEqual(find_skill("k360-org-report", HERE), Path(tmp))
            finally:
                os.environ.pop("K360_ORG_SKILL_DIR", None)


class DiagDownloadTests(unittest.TestCase):
    def _zip(self, path: Path, ok: bool = True) -> None:
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a.json", "{}" * 100)
            zf.writestr("nodes_stats.json", "{}")
            zf.writestr("indices_stats.json", "{}")
        if not ok:
            data = path.read_bytes()
            path.write_bytes(data[: len(data) // 2])

    def test_a_truncated_zip_is_not_complete(self):
        import admin_diags
        with tempfile.TemporaryDirectory() as tmp:
            good, bad = Path(tmp) / "g.zip", Path(tmp) / "b.zip"
            self._zip(good)
            self._zip(bad, ok=False)
            self.assertTrue(admin_diags._zip_ok(good))
            self.assertTrue(admin_diags._is_zip(bad))
            self.assertFalse(admin_diags._zip_ok(bad))

    def test_capture_retries_three_times_then_reports_the_failure(self):
        import admin_diags
        calls = []

        def boom(*_a, **_k):
            calls.append(1)
            raise RuntimeError("response was not a complete zip")

        orig = admin_diags._capture_one
        admin_diags._capture_one = boom
        try:
            with tempfile.TemporaryDirectory() as tmp:
                row = admin_diags._capture_with_retry("b", "k", {"id": "a" * 32, "name": "n"}, Path(tmp) / "x.zip", pause=0)
        finally:
            admin_diags._capture_one = orig
        self.assertEqual(len(calls), 3)
        self.assertEqual(row["result"], "error")
        self.assertEqual(row["attempts"], 3)

    def test_capture_recovers_on_a_later_attempt(self):
        import admin_diags
        calls = []

        def flaky(base, key, row, dest):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("cut short")
            return {**row, "result": "ok", "bytes": 1, "file": str(dest)}

        orig = admin_diags._capture_one
        admin_diags._capture_one = flaky
        try:
            row = admin_diags._capture_with_retry("b", "k", {"id": "a" * 32, "name": "n"}, Path("/nonexistent/x.zip"), pause=0)
        finally:
            admin_diags._capture_one = orig
        self.assertEqual((row["result"], row["attempts"]), ("ok", 3))

    def test_cover_names_deployments_with_no_bundle(self):
        from render_pdf import _missing_diag_notes
        self.assertEqual(_missing_diag_notes({}), [])
        note = _missing_diag_notes({"download_failed": [{"name": "alpha"}], "k360_fallback": 1, "fallback_names": ["alpha"]})[0]
        self.assertIn("alpha", note)
        self.assertIn("could not be downloaded after retries", note)
        note = _missing_diag_notes({"k360_fallback": 2, "fallback_names": ["a", "b"]})[0]
        self.assertIn("2 active deployments are not measured", note)


class DegradationDisclosureTests(unittest.TestCase):
    """A warning that changes the quote has to reach the artifact, not only stderr."""

    def test_note_limit_writes_once_to_estimation_limits(self):
        import run_consolidation as rc
        acc = {"sizing": {}}
        rc.note_limit(acc, "sheets_venv_missing", "venv gone", "high")
        rc.note_limit(acc, "sheets_venv_missing", "venv gone again", "high")
        limits = acc["sizing"]["diagnostics"]["estimation_limits"]
        self.assertEqual([row["code"] for row in limits], ["sheets_venv_missing"])
        self.assertEqual(limits[0]["severity"], "high")

    def test_note_limit_accepts_a_sizing_block_directly(self):
        import run_consolidation as rc
        sz = {"diagnostics": {"estimation_limits": []}}
        rc.note_limit(sz, "pdf_upload_failed", "upload failed", "low")
        self.assertEqual(sz["diagnostics"]["estimation_limits"][0]["code"], "pdf_upload_failed")

    def test_warn_prints_and_records(self):
        import contextlib, io
        import run_consolidation as rc
        acc, err = {"sizing": {}}, io.StringIO()
        with contextlib.redirect_stderr(err):
            rc.warn(acc, "regional_sizing_failed", "per-region sizing failed", "high")
        self.assertIn("WARN: per-region sizing failed", err.getvalue())
        self.assertEqual(acc["sizing"]["diagnostics"]["estimation_limits"][0]["code"], "regional_sizing_failed")

    def test_every_draft_path_also_records_a_limit(self):
        """A run marked DRAFT must say why in the artifact, not only on the terminal."""
        import pathlib
        import run_consolidation as rc
        lines = pathlib.Path(rc.__file__).read_text().splitlines()
        for index, line in enumerate(lines):
            if '["draft"] = True' not in line:
                continue
            window = "\n".join(lines[max(0, index - 6):index + 1])
            self.assertIn(
                "warn(", window,
                f"run_consolidation.py:{index + 1} marks the run DRAFT without recording a limit",
            )

    def test_a_degraded_step_is_not_left_on_stderr_alone(self):
        """Only the paths that produce no quote at all may warn without a limit."""
        import pathlib
        import run_consolidation as rc
        source = pathlib.Path(rc.__file__).read_text()
        # Every remaining bare WARN sets diagnostics_status and returns, or is warn()'s own print.
        bare = [
            line.strip() for line in source.splitlines()
            if ('print(f"WARN:' in line or 'print("WARN:' in line) and "{detail}" not in line
        ]
        self.assertLessEqual(len(bare), 6, f"a degraded step warns without recording a limit: {bare}")

    def test_a_missing_es3_interpreter_is_named(self):
        import consolidation_regional as cr
        failures: list[dict] = []
        with mock.patch.dict(os.environ, {"ES3_ESTIMATOR_PY": "/nonexistent/python"}):
            self.assertIsNone(cr._es3_region({"stored_gb": 1}, failures))
            self.assertEqual(cr.es3_estimator_missing(), "/nonexistent/python")
        self.assertEqual([row["code"] for row in failures], ["es3_estimator_missing"])
        self.assertIn("/nonexistent/python", failures[0]["detail"])

    def test_an_es3_run_that_fails_is_told_apart_from_a_missing_one(self):
        import consolidation_regional as cr
        failures: list[dict] = []
        proc = mock.Mock(returncode=1, stdout="", stderr="boom\n")
        with mock.patch.object(cr.Path, "is_file", lambda self: True), \
             mock.patch.object(cr.subprocess, "run", return_value=proc):
            self.assertIsNone(cr._es3_region({"stored_gb": 1}, failures))
        self.assertEqual([row["code"] for row in failures], ["es3_estimator_failed"])
        self.assertIn("boom", failures[0]["detail"])


class CleanTreeRuleTests(unittest.TestCase):
    """Only a change that could alter what the scripts do marks the tree dirty."""

    @staticmethod
    def _stamp(tracked: str, untracked: list[str]):
        import run_consolidation as rc
        every = tracked + "".join(f"?? {name}\n" for name in untracked)

        def fake_run(argv, **_kw):
            if "rev-parse" in argv:
                return mock.Mock(returncode=0, stdout="abc1234\n")
            if "--untracked-files=no" in argv:
                return mock.Mock(returncode=0, stdout=tracked)
            return mock.Mock(returncode=0, stdout=every)

        with mock.patch.object(rc.subprocess, "run", side_effect=fake_run):
            return rc.method_stamp(90)

    def test_untracked_notes_do_not_mark_the_tree_dirty(self):
        stamp = self._stamp("", ["serverless_analyzer/REVIEW_v6.md", "serverless_analyzer/notes.txt"])
        self.assertFalse(stamp["uncommitted_changes"])
        self.assertEqual(stamp["untracked_importable"], [])
        self.assertEqual(len(stamp["untracked_ignored"]), 2)

    def test_an_untracked_script_does_mark_the_tree_dirty(self):
        stamp = self._stamp("", ["serverless_analyzer/diag-serverless-consolidation/scripts/patch.py"])
        self.assertTrue(stamp["uncommitted_changes"])
        self.assertEqual(
            stamp["untracked_importable"],
            ["serverless_analyzer/diag-serverless-consolidation/scripts/patch.py"],
        )

    def test_an_untracked_python_file_outside_scripts_is_ignored(self):
        stamp = self._stamp("", ["serverless_analyzer/scratch.py"])
        self.assertFalse(stamp["uncommitted_changes"])
        self.assertEqual(stamp["untracked_ignored"], ["serverless_analyzer/scratch.py"])

    def test_a_modified_tracked_file_still_marks_the_tree_dirty(self):
        stamp = self._stamp(" M serverless_analyzer/diag-serverless-consolidation/SKILL.md\n", ["x.md"])
        self.assertTrue(stamp["uncommitted_changes"])

    def test_a_clean_tree_is_clean(self):
        stamp = self._stamp("", [])
        self.assertFalse(stamp["uncommitted_changes"])
        self.assertEqual(stamp["commit"], "abc1234")


class PreflightTests(unittest.TestCase):
    def _rows(self, **over):
        import argparse
        import run_consolidation as rc
        args = argparse.Namespace(**{
            "lookback_days": 90, "env": "/nonexistent/.env", "reuse": None, "skip_vpn_check": True, **over,
        })
        return {row["check"]: row for row in rc.preflight(args, network=False)}

    def test_a_missing_env_is_reported_before_anything_runs(self):
        rows = self._rows()
        self.assertFalse(rows[".env"]["ok"])
        self.assertIn("not found", rows[".env"]["detail"])

    def test_an_org_report_with_no_daily_series_is_caught(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"ecu": {"daily_totals": []}}, handle)
        rows = self._rows(reuse=handle.name)
        self.assertFalse(rows["org report"]["ok"])
        self.assertIn("no daily billing series", rows["org report"]["detail"])

    def test_an_org_report_with_a_daily_series_passes(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"ecu": {"daily_totals": [1.0, 2.0]}}, handle)
        rows = self._rows(reuse=handle.name)
        self.assertTrue(rows["org report"]["ok"])
        self.assertIn("2 daily billing days", rows["org report"]["detail"])

    def test_the_virtualenvs_are_checked(self):
        rows = self._rows()
        self.assertIn("Sheets venv", rows)
        # Search is priced on the Search estimator sheet. The es3 notebook is checked only for --search-notebook.
        self.assertNotIn("es3 estimator", rows)
        self.assertIn("es3 estimator", self._rows(search_notebook=True))

    def test_print_preflight_reports_failure(self):
        import contextlib
        import io
        import run_consolidation as rc
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = rc.print_preflight([
                {"check": "a", "ok": True, "detail": "fine"},
                {"check": "b", "ok": False, "detail": "broken"},
                {"check": "c", "ok": None, "detail": "unknown"},
            ])
        self.assertFalse(ok)
        self.assertIn("failed: b", out.getvalue())
        self.assertIn("2/3 ok", out.getvalue())


class PhaseGuardTests(unittest.TestCase):
    def test_scripts_parse_on_python_310(self):
        py = subprocess.check_output(["uv", "python", "find", "3.10"], text=True).strip()
        roots = [HERE, HERE.parents[1] / "k360-account-analysis" / "scripts"]
        files = [path for root in roots for path in root.glob("*.py")]
        script = (
            "import ast, pathlib, sys\n"
            "failed = []\n"
            "for name in sys.argv[1:]:\n"
            "    path = pathlib.Path(name)\n"
            "    try:\n"
            "        ast.parse(path.read_text())\n"
            "    except SyntaxError as exc:\n"
            "        failed.append(f'{path}: {exc}')\n"
            "if failed:\n"
            "    raise SystemExit('\\n'.join(failed))\n"
        )
        subprocess.check_call([py, "-c", script, *[str(path) for path in files]])

    def test_a_region_off_the_list_uses_the_provider_default(self):
        from size_serverless import match_region
        options = ["AWS: us-east-1 (N. Virginia)", "AWS: us-west-2 (Oregon)", "AZURE: eastus (Virginia)", "GCP: us-east1 (South Carolina)"]
        option, how = match_region("aws", "us-east-2", options)
        self.assertEqual((option, how), ("AWS: us-east-1 (N. Virginia)", "provider_default"))
        option, how = match_region("azure", "francecentral", options)
        self.assertEqual((option, how), ("AZURE: eastus (Virginia)", "provider_default"))
        option, how = match_region("gcp", "asia-east1", options)
        self.assertEqual((option, how), ("GCP: us-east1 (South Carolina)", "provider_default"))
        security = ["AZURE: us-east-1 (Virginia)", "GCP: us-east-1 (S. Carolina)"]
        option, how = match_region("azure", "uksouth", security)
        self.assertEqual((option, how), ("AZURE: us-east-1 (Virginia)", "provider_default"))
        option, how = match_region("gcp", "asia-east1", security)
        self.assertEqual((option, how), ("GCP: us-east-1 (S. Carolina)", "provider_default"))
        listed, how = match_region("aws", "us-west-2", options)
        self.assertEqual((listed, how), ("AWS: us-west-2 (Oregon)", "exact"))

    def test_search_uses_the_same_provider_default(self):
        from es3_estimate import match_region
        pricing = {"AWS": {"us-east-1": {}, "eu-west-1": {}}, "Azure": {"eastus": {}}, "GCP": {"us-east1": {}}}
        self.assertEqual(match_region("AWS", "us-east-2", pricing), ("us-east-1", "provider_default"))
        self.assertEqual(match_region("Azure", "southcentralus", pricing), ("eastus", "provider_default"))
        self.assertEqual(match_region("GCP", "asia-east1", pricing), ("us-east1", "provider_default"))
        self.assertEqual(match_region("AWS", "eu-west-1", pricing), ("eu-west-1", "exact"))

    def test_an_aws_region_does_not_resolve_to_azure(self):
        from size_serverless import match_region
        option, how = match_region("aws", "us-east-1", ["AZURE: eastus (Virginia)", "GCP: us-east1 (South Carolina)"])
        self.assertIsNone(option)
        self.assertEqual(how, "none")

    def test_two_observability_rows_make_two_plan_jobs(self):
        rows = []
        for name, region in (("one", "us-central1"), ("two", "europe-west1")):
            rows.append({
                "name": name,
                "solution": "observability",
                "region": region,
                "provider": "gcp",
                "bulk_gb_day": 10,
                "store_gb": 100,
                "retention_days": 30,
                "logs_gb_day": 6,
                "k8s_logs_gb_day": 1,
                "metrics_gb_day": 2,
                "traces_gb_day": 1,
                "bulk_mix": {"logs": 6, "k8s_logs": 1, "metrics": 2, "traces": 1},
            })
        _specs, jobs, error, _floor = plan_sheet_jobs(
            rows, run_key="case", name_suffix="", force_months=12, o11y_id="o", security_id="s",
            parent_id="p", reuse=True, provider="gcp", region_name="us-central1", support="Premium",
            feature_tier="Complete",
        )
        self.assertIsNone(error)
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(not job.get("error") for job in jobs))

    def test_one_failed_plan_fill_does_not_clear_the_next_bar(self):
        specs = [
            {"cut_pct": 100, "label": "Ceiling", "projects": 1, "reused_headline": True, "groups": []},
            {"cut_pct": 0, "label": "Floor", "projects": 2, "groups": [
                {"sheet": 0, "names": ["a"], "bulk_gb_day": 1},
            ]},
            {"cut_pct": 50, "label": "Mid", "projects": 1, "groups": [
                {"sheet": 1, "names": ["b"], "bulk_gb_day": 1},
            ]},
        ]
        built = assemble_plan_bars(specs, [{"error": "region missing"}, {"monthly": 40, "url": "https://example.invalid"}])
        by_label = {bar["label"]: bar for bar in built["bars"]}
        self.assertIsNone(by_label["Floor"]["monthly"])
        self.assertEqual(by_label["Mid"]["monthly"], 40)
        self.assertTrue(by_label["Floor"].get("gap"))

    def test_a_region_off_the_estimator_list_is_quoted_and_flagged(self):
        """Priced at the same provider's default, named on the cover, and never withheld."""
        acc = _quote_account(g28=10, e73=0)
        acc["sizing"]["consolidated"]["security"] = {"stored_gb": 0, "daily_raw_gb": 0, "deployments": 0}
        acc["sizing"]["consolidated"]["search"] = {"stored_gb": 0, "daily_raw_gb": 0, "deployments": 0}
        acc["sizing"]["estimators"]["security"] = {}
        acc["sizing"]["estimators"]["search"] = {}
        mix = {"logs_gb_day": 6, "k8s_logs_gb_day": 1, "metrics_gb_day": 2, "traces_gb_day": 1}
        acc["cloud"] = {"by_deployment": [
            {"name": "only", "region": "southcentralus", "provider": "azure"},
            {"name": "other", "region": "eastus", "provider": "azure"},
        ]}
        acc["solutions"]["by_deployment"] = [
            {"name": "only", "solution": "observability", "platform": "ech", "gb": 10, "daily_raw_gb": 8, "ecu": 1, **mix},
            {"name": "other", "solution": "observability", "platform": "ech", "gb": 4, "daily_raw_gb": 2, "ecu": 1, **mix},
        ]
        acc["sizing"]["estimators"]["projects"] = [
            {
                "solution": "observability",
                "region": "southcentralus",
                "region_match": "AZURE: eastus (Virginia) (provider_default; wanted southcentralus)",
                "readback": {"monthly_steady_state": 200, "total_raw_gb_day": 8, "annual_year1": 2400, "annual_steady_state": 2400},
                "echo_gate": {"accepted": True, "written_bulk": 8, "sheet_gb_day": 8},
            },
            {
                "solution": "observability",
                "region": "eastus",
                "region_match": "AZURE: eastus (Virginia) (exact)",
                "readback": {"monthly_steady_state": 50, "total_raw_gb_day": 2, "annual_year1": 600, "annual_steady_state": 600},
                "echo_gate": {"accepted": True, "written_bulk": 2, "sheet_gb_day": 2},
            },
        ]
        acc["sizing"]["architecture"] = {"per_region": True}
        cons = build_consolidation(acc, {"by_deployment": acc["cloud"]["by_deployment"]})
        block = cons["serverless"]["by_solution"]["observability"]
        self.assertEqual(block["total_mo"], 250.0)
        self.assertIn("quotable", str(block.get("pricing") or ""))
        self.assertEqual(cons["savings"]["serverless_mo"], 250.0)
        names = {row["check"]: row for row in cons["integrity"]["checks"]}
        self.assertTrue(names["Region fidelity"]["passed"])
        self.assertTrue(names["Region fidelity"]["warning"])
        self.assertIn("provider_default", names["Region fidelity"]["detail"])
        self.assertTrue(names["Quote present"]["passed"])
        self.assertTrue(any("priced as" in note for note in cons["notes"]))

    def test_one_off_list_search_region_is_priced_at_the_provider_default(self):
        acc = _quote_account(g28=10, e73=0)
        acc["sizing"]["consolidated"]["observability"] = {"stored_gb": 0, "daily_raw_gb": 0, "deployments": 0}
        acc["sizing"]["consolidated"]["security"] = {"stored_gb": 0, "daily_raw_gb": 0, "deployments": 0}
        acc["sizing"]["consolidated"]["search"] = {"stored_gb": 20, "daily_raw_gb": 0, "deployments": 2}
        acc["sizing"]["estimators"]["observability"] = {}
        acc["sizing"]["estimators"]["security"] = {}
        acc["sizing"]["estimators"]["search"] = {"readback": {"monthly_steady_state": 500, "annual_year1": 6000}}
        acc["cloud"] = {"by_deployment": [
            {"name": "kept", "region": "us-central1", "provider": "gcp"},
            {"name": "dropped", "region": "asia-east1", "provider": "gcp"},
        ], "summary": {"provider": "gcp"}}
        acc["solutions"]["by_deployment"] = [
            {"name": "kept", "solution": "search", "platform": "ech", "gb": 12, "ecu": 10},
            {"name": "dropped", "solution": "search", "platform": "ech", "gb": 8, "ecu": 20},
        ]
        acc["ecu"]["monthly"] = {"months": ["2026-10"], "totals": [30], "partial_months": []}
        acc["ecu"]["monthly_by_deployment"] = {"kept": [10], "dropped": [20]}
        acc["sizing"]["estimators"]["projects"] = [
            {"solution": "search", "region": "us-central1", "region_match": "GCP us-central1 (exact)",
             "readback": {"monthly_steady_state": 300, "annual_year1": 3600}},
            {"solution": "search", "region": "asia-east1", "region_match": "GCP us-east1 (provider_default; wanted asia-east1)",
             "readback": {"monthly_steady_state": 200, "annual_year1": 2400, "annual_steady_state": 2400}},
        ]
        acc["sizing"]["architecture"] = {"per_region": True}
        cons = build_consolidation(acc, {"by_deployment": acc["cloud"]["by_deployment"]})
        block = cons["serverless"]["by_solution"]["search"]
        self.assertEqual(block["total_mo"], 500.0)
        self.assertIn("quotable", str(block.get("pricing") or ""))
        self.assertEqual(cons["savings"]["serverless_mo"], 500.0)
        priced = {row["region"]: row["monthly"] for row in block["by_region"]}
        self.assertEqual(priced, {"us-central1": 300.0, "asia-east1": 200.0})
        names = {row["check"]: row for row in cons["integrity"]["checks"]}
        self.assertTrue(names["Region fidelity"]["passed"])
        self.assertTrue(names["Region fidelity"]["warning"])
        left_out = {row["name"] for row in (cons.get("ech_recent_months_priced") or {}).get("unpriced") or []}
        self.assertNotIn("dropped", left_out)
        self.assertNotIn("kept", left_out)

    def test_an_unstable_bill_compares_the_latest_month(self):
        from consolidation import recent_ech_months_priced
        acc = {
            "meta": {"customer": "Example", "date": "2026-11-16"},
            "ecu": {"monthly": {"months": ["2026-08", "2026-09", "2026-10"], "partial_months": []},
                    "monthly_by_deployment": {"kept": [396, 377, 9682]}},
            "solutions": {"by_deployment": [
                {"name": "kept", "solution": "search", "platform": "ech", "gb": 10},
            ]},
            "sizing": {"ingest_source": "diagnostics"},
        }
        priced = recent_ech_months_priced(acc, {"search": {"pricing": "es3-estimator performant (quotable)"}})
        self.assertFalse(priced["stable"])
        self.assertEqual(priced["comparison"], 9682)
        self.assertEqual(priced["comparison_basis"], "latest_complete_month")
        scored = integrity_score({"billing_window": priced})
        row = next(item for item in scored["checks"] if item["check"] == "Billing window")
        self.assertFalse(row["passed"])
        acc["consolidation"] = {
            "savings": {"serverless_mo": 2600, "list_mo": None, "ech_list_mo": None, "list_pct": None},
            "ech_scope": {"status": "unavailable"},
            "ech_recent_months_priced": priced,
            "integrity": {"passed": False, "checks": [row]},
            "serverless": {"dominant_solution": "search", "by_solution": {"search": {"total_mo": 2600}}},
            "basis": {},
            "estimate_months": 12,
            "sensitivity": {"fleet": [{"scenario": "Complete-month average", "ech_ecu_mo": 9682, "serverless_mo": 2600, "scale": 1}], "retention": []},
        }
        html = render_consolidation_html(acc)
        self.assertNotIn("3-month average", html)
        self.assertIn("$9,682", html)

    def test_a_missing_quote_shows_the_bill_and_no_zero_bar(self):
        acc = {
            "meta": {"customer": "Example", "date": "2026-11-16"},
            "cloud": {"summary": {}},
            "consolidation": {
                "savings": {"serverless_mo": None, "list_mo": None, "ech_list_mo": None, "list_pct": None},
                "ech_scope": {"status": "unavailable"},
                "ech_recent_months": {"months": [{"month": "2026-10", "cost": 317000}], "average": 317000},
                "ech_recent_months_priced": {"months": [], "average": None, "comparison": None},
                "integrity": {"passed": False, "checks": [{"check": "Quote present", "passed": False}]},
                "serverless": {"dominant_solution": "observability", "by_solution": {}},
                "basis": {},
                "sensitivity": {"fleet": [{"scenario": "Complete-month average", "ech_ecu_mo": 317000, "scale": 1}], "retention": []},
            },
        }
        html = render_consolidation_html(acc)
        self.assertIn("$317,000", html)
        self.assertIn("No serverless price was returned", html)
        self.assertIn("ECH bill, no serverless price", html)
        self.assertNotIn("estimated monthly reduction", html.lower())

    def test_neutral_wording_leaves_product_text_and_filenames(self):
        acc = {
            "meta": {"customer": "Example", "date": "2026-11-16", "draft": True},
            "cloud": {"summary": {}},
            "consolidation": {
                "savings": {"serverless_mo": 10, "list_mo": None, "ech_list_mo": None},
                "ech_scope": {"status": "unavailable"},
                "integrity": {"passed": False, "checks": [{"check": "Quote present", "passed": False}]},
                "serverless": {"dominant_solution": "search", "by_solution": {"search": {"total_mo": 10}}},
                "basis": {},
                "notes": [
                    "Upgrade to unlock LogsDB / TSDS storage savings",
                    "see bulk_savings.pdf",
                ],
                "sensitivity": {"fleet": [{"scenario": "Complete-month average", "ech_ecu_mo": 10, "serverless_mo": 10, "scale": 1}], "retention": []},
            },
        }
        html = render_consolidation_html(acc)
        self.assertIn("Upgrade to unlock LogsDB / TSDS storage savings", html)
        self.assertIn("bulk_savings.pdf", html)
        self.assertIn("serverless estimate for", html)
        self.assertNotIn("estimated savings from", html.lower())

    def test_plan_consistency_fails_when_the_floor_is_not_the_headline(self):
        scored = integrity_score({
            "plan_consistency": {
                "headline": 100,
                "floor_monthly": 80,
                "floor_projects": 2,
                "regional_projects": 2,
            },
        })
        row = next(item for item in scored["checks"] if item["check"] == "Plan consistency")
        self.assertFalse(row["passed"])
        matched = integrity_score({
            "plan_consistency": {
                "headline": 100,
                "floor_monthly": 100,
                "floor_projects": 2,
                "regional_projects": 2,
            },
        })
        ok = next(item for item in matched["checks"] if item["check"] == "Plan consistency")
        self.assertTrue(ok["passed"])

    def test_a_short_window_or_a_restarted_counter_warns_and_keeps_the_quote(self):
        over = integrity_score({"coverage": {
            "bulk_gb_day": 100,
            "bulk_short_window_gb_day": 60,
            "bulk_restart_gb_day": 40,
        }})
        self.assertTrue(over["passed"])
        rows = {row["check"]: row for row in over["checks"]}
        self.assertTrue(rows["Rate window"]["warning"])
        self.assertTrue(rows["Rate window"]["passed"])
        self.assertIn("rated over a short window", rows["Rate window"]["detail"])
        self.assertIn("still produced", rows["Rate window"]["detail"])
        self.assertTrue(rows["Counter age"]["warning"])
        self.assertTrue(rows["Counter age"]["passed"])
        self.assertIn("restarted bulk counter", rows["Counter age"]["detail"])
        under = integrity_score({"coverage": {
            "bulk_gb_day": 100,
            "bulk_short_window_gb_day": 10,
            "bulk_restart_gb_day": 10,
        }})
        quiet = {row["check"]: row for row in under["checks"]}
        self.assertTrue(under["passed"])
        self.assertNotIn("warning", quiet["Rate window"])
        self.assertNotIn("warning", quiet["Counter age"])
        blocked = integrity_score({
            "coverage": {"bulk_gb_day": 100, "bulk_short_window_gb_day": 60, "bulk_restart_gb_day": 0},
            "priced": {"status": "unavailable", "reason": "unreconciled"},
        })
        self.assertFalse(blocked["passed"])
        still = {row["check"]: row for row in blocked["checks"]}
        self.assertTrue(still["Rate window"]["warning"])
        self.assertFalse(still["Priced share"]["passed"])
        account = _quote_account(g28=10, e73=0)
        account["sizing"]["diagnostics"] = {
            "bulk_gb_day": 100,
            "bulk_short_window_gb_day": 60,
            "bulk_restart_gb_day": 40,
            "matched": 1,
            "active_deployments": 1,
        }
        quoted = build_consolidation(account, None)
        names = {row["check"]: row for row in quoted["integrity"]["checks"]}
        self.assertTrue(names["Rate window"]["warning"])
        self.assertTrue(names["Counter age"]["warning"])
        self.assertTrue(any("rated over a short window" in note and "still produced" in note for note in quoted["notes"]))
        self.assertTrue(any("restarted bulk counter" in note for note in quoted["notes"]))

    def test_the_quote_page_warns_and_still_shows_the_price(self):
        acc = {
            "meta": {"customer": "Example", "date": "2026-11-16"},
            "cloud": {"summary": {}},
            "consolidation": {
                "savings": {"serverless_mo": 250, "list_mo": 100, "ech_list_mo": 350, "list_pct": 28.6, "cost_mo": 100},
                "ech_scope": {"status": "complete"},
                "integrity": {"passed": True, "checks": [{
                    "check": "Rate window",
                    "score": 0.4,
                    "passed": True,
                    "warning": True,
                    "detail": "60.000 GB/day of 100.000 is rated over a short window (60.0%). The bar is 25%. The quote is still produced.",
                }, {
                    "check": "Counter age",
                    "score": 0.6,
                    "passed": True,
                    "warning": True,
                    "detail": "40.000 GB/day of 100.000 is from a restarted bulk counter (40.0%). The bar is 25%. The quote is still produced.",
                }]},
                "serverless": {"dominant_solution": "search", "by_solution": {"search": {"total_mo": 250, "pricing": "es3-estimator performant (quotable)"}}},
                "basis": {},
                "notes": ["Warning: 60.000 GB/day of 100.000 is rated over a short window (60.0%). The bar is 25%. The quote is still produced."],
                "sensitivity": {"fleet": [{"scenario": "Complete-month average", "ech_ecu_mo": 350, "serverless_mo": 250, "scale": 1}], "retention": []},
            },
        }
        html = render_consolidation_html(acc)
        self.assertIn(">Warning<", html)
        self.assertIn("rated over a short window", html)
        self.assertIn("restarted bulk counter", html)
        self.assertNotIn(">Unverified<", html)
        self.assertIn("$250", html)

    @staticmethod
    def _retention_basis(**coverage):
        recorded = integrity_score({"coverage": {"bulk_gb_day": 10, **coverage}})
        return next(item for item in recorded["checks"] if item["check"] == "Retention basis")

    def test_retention_basis_warns_but_never_fails_the_quote(self):
        row = self._retention_basis(retention_policy_bulk_gb_day=4)
        self.assertEqual(row["score"], 0.4)
        self.assertTrue(row["passed"])
        self.assertTrue(row["warning"])
        self.assertIn("no retention age", row["detail"])
        self.assertNotIn("30-day", row["detail"])
        recorded = integrity_score({"coverage": {"bulk_gb_day": 10, "retention_policy_bulk_gb_day": 4}})
        self.assertNotIn("Retention basis", [item["check"] for item in recorded["checks"] if item.get("passed") is False])

    def test_retention_basis_passes_at_the_90_percent_bar(self):
        row = self._retention_basis(retention_policy_bulk_gb_day=9)
        self.assertEqual(row["score"], 0.9)
        self.assertTrue(row["passed"])
        self.assertNotIn("warning", row)

    def test_retention_basis_names_bulk_counted_inside_the_threshold(self):
        row = self._retention_basis(
            retention_policy_bulk_gb_day=10, retention_policy_within_threshold_gb_day=7,
        )
        self.assertEqual(row["score"], 1.0)
        self.assertNotIn("warning", row)
        self.assertIn("inside the threshold", row["detail"])

    def test_one_unconfigured_index_keeps_a_deployment_policy_aged(self):
        from constants import RETENTION_UNAGED_MAX_SHARE
        self.assertEqual(RETENTION_UNAGED_MAX_SHARE, 0.10)
        from diag_account import apply_diagnostics
        acc = {
            "meta": {},
            "solutions": {"by_deployment": [
                {"name": "mostly", "solution": "observability", "gb": 100.0, "ecu": 100.0},
                {"name": "half", "solution": "observability", "gb": 100.0, "ecu": 100.0},
            ]},
            "sizing": {"consolidated": {}},
            "storage": {},
        }
        mix = {"logs": 1.0, "k8s_logs": 0.0, "metrics": 0.0, "traces": 0.0}
        rows = [
            # 0.3% of store with no retention age: inside the threshold, still policy-aged.
            {"name": "mostly", "bulk_gb_day": 120.0, "store_gb": 100.0, "retention_days": 48.0,
             "retention_basis": "policy", "retention_known_share": 0.997, "retention_unaged_share": 0.003,
             "retention_index_count": 10, "retention_index_days_sum": 480.0, "bulk_mix": mix},
            # 40% with no retention age: past the threshold, contributes nothing.
            {"name": "half", "bulk_gb_day": 80.0, "store_gb": 100.0, "retention_days": 35.0,
             "retention_basis": "partial", "retention_known_share": 0.6, "retention_unaged_share": 0.4,
             "retention_index_count": 10, "retention_index_days_sum": 350.0, "bulk_mix": mix},
        ]
        cov = apply_diagnostics(acc, rows)["sizing"]["diagnostics"]
        self.assertEqual(cov["retention_policy_bulk_gb_day"], 120.0)
        self.assertEqual(cov["retention_policy_within_threshold_gb_day"], 120.0)
        self.assertEqual(cov["retention_unaged_max_share"], 0.10)
        named = {row["name"]: row for row in cov["retention_unaged"]}
        self.assertTrue(named["mostly"]["within_threshold"])
        self.assertFalse(named["half"]["within_threshold"])

    def test_the_bundle_basis_uses_the_ten_percent_threshold(self):
        import admin_diags
        def basis(known_bytes, unaged_bytes):
            local = known_bytes + unaged_bytes
            if local <= 0:
                return None
            share = unaged_bytes / local
            if known_bytes <= 0:
                return None                            # no aged store: no basis, no days, no default
            if unaged_bytes <= 0 or share <= admin_diags.RETENTION_UNAGED_MAX_SHARE:
                return "policy"
            return "partial"
        self.assertEqual(basis(1000, 0), "policy")
        self.assertEqual(basis(997, 3), "policy")      # 0.3% with no retention age
        self.assertEqual(basis(900, 100), "policy")    # exactly at the 10% bar
        self.assertEqual(basis(899, 101), "partial")
        self.assertIsNone(basis(0, 1000))

    def test_a_sheet_with_two_problems_is_one_finding_and_the_score_stays_in_range(self):
        from quote_checks import _region_fidelity_row
        # An alias of a different region number, written to another cloud. This used to append two
        # findings for one sheet, so a lone sheet scored 1 - 2/1 = -1.
        row = _region_fidelity_row([{
            "solution": "observability", "region": "us-east-2", "provider": "azure",
            "region_match": "AWS: us-east-1 (N. Virginia) (alias)",
        }])
        self.assertGreaterEqual(row["score"], 0.0)
        self.assertLessEqual(row["score"], 1.0)
        self.assertEqual(row["score"], 0.0)
        self.assertTrue(row["warning"])
        self.assertTrue(row["passed"])
        self.assertIn("1 of 1 written regions", row["detail"])
        self.assertIn("alias of a different region", row["detail"])
        self.assertIn("written to another cloud", row["detail"])

    def test_the_score_is_a_share_of_sheets_whatever_is_wrong_with_each(self):
        from quote_checks import _region_fidelity_row
        sheets = [
            {"solution": "observability", "region": "us-east-1", "provider": "aws", "region_match": "AWS: us-east-1 (N. Virginia) (exact)"},
            {"solution": "security", "region": "us-east-2", "provider": "azure", "region_match": "AWS: us-east-1 (N. Virginia) (alias)"},
            {"solution": "search", "region": "mars-1", "provider": "gcp", "region_match": "AWS: us-east-1 (N. Virginia) (provider_default)"},
        ]
        row = _region_fidelity_row(sheets)
        self.assertEqual(row["score"], round(1 / 3, 3))
        self.assertIn("2 of 3 written regions", row["detail"])

    def test_a_substitute_region_on_the_wrong_cloud_is_reported_as_another_cloud(self):
        from quote_checks import _region_fidelity_row
        # A provider_default or nearest match used to `continue` before the cloud was compared.
        row = _region_fidelity_row([{
            "solution": "search", "region": "us-east1", "provider": "gcp",
            "region_match": "AWS: us-east-1 (N. Virginia) (provider_default)",
        }])
        self.assertIn("provider_default; written to another cloud", row["detail"])
        self.assertLess(row["detail"].index("another cloud"), row["detail"].index("Each is priced"))

    def test_the_same_cloud_substitute_is_still_not_called_another_cloud(self):
        from quote_checks import _region_fidelity_row
        row = _region_fidelity_row([{
            "solution": "search", "region": "us-east1", "provider": "gcp",
            "region_match": "GCP: us-east1 (S. Carolina) (provider_default)",
        }])
        self.assertTrue(row["warning"])
        self.assertNotIn("another cloud", row["detail"])

    def test_every_estimator_entry_records_the_provider_the_deployments_run_on(self):
        import ast
        tree = ast.parse((Path(__file__).resolve().parent / "size_serverless.py").read_text())
        entries = [
            node for node in ast.walk(tree) if isinstance(node, ast.Dict)
            and {"kind", "region"} <= {k.value for k in node.keys if isinstance(k, ast.Constant)}
        ]
        # Observability and security, each filled and unquoted, plus the Search fleet entry. The regional-scope
        # Search entry is an existing dict updated in place, and is checked by its assignment below.
        self.assertGreaterEqual(len(entries), 5)
        for node in entries:
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            self.assertIn("provider", keys, f"line {node.lineno}: an estimator entry has no provider")
        source = (Path(__file__).resolve().parent / "size_serverless.py").read_text()
        self.assertIn('entry["provider"] = solution_provider(acc, "search", provider)', source)

    def test_a_regional_job_entry_carries_its_provider_filled_or_unquoted(self):
        import plan_sizing
        from unittest import mock
        search_job = {
            "solution": "search", "region": "us-east-1", "title": "t", "stored_gb": 10.0, "parent_id": "p", "local_dir": ".",
            "provider": "aws", "support": "Premium", "workload": {}, "org_gb": 10.0, "reuse": True,
        }
        result = ({"status": "estimated", "region_match": "AWS: us-east-1 (N. Virginia) (exact)",
                   "readback": {"monthly_steady_state": 100.0}}, {"url": "u", "spreadsheet_id": "i"})
        with mock.patch.object(plan_sizing, "services_from", return_value=(None, None)), \
             mock.patch.object(plan_sizing, "price_search", return_value=result):
            entry = plan_sizing._fill_regional_job(search_job, None)
        self.assertEqual(entry["provider"], "aws")

        sheet_job = {
            "solution": "security", "region": "mars-1", "title": "t", "stored_gb": 10.0, "parent_id": "p", "local_dir": ".",
            "provider": "azure", "support": "Premium", "template_id": "tpl", "tab": "T", "readback": {}, "reuse": True,
            "retention": 3, "daily": 5.0, "skip_xlsx": True,
        }
        with mock.patch.object(plan_sizing, "services_from", return_value=(None, None)), \
             mock.patch.object(plan_sizing, "copy_verified", return_value=({"id": "x", "name": "n", "url": "u"}, False, {"version": "2.6", "newer_than_validated": False})), \
             mock.patch.object(plan_sizing, "region_options", return_value=[]), \
             mock.patch.object(plan_sizing, "match_region", return_value=(None, "none")), \
             mock.patch.object(plan_sizing, "trash_file"):
            entry = plan_sizing._fill_regional_job(sheet_job, None)
        self.assertEqual(entry["status"], "unquoted")
        self.assertEqual(entry["provider"], "azure")

    def test_the_cross_cloud_finding_is_reachable_from_real_estimator_entries(self):
        from consolidation_regional import _written_regions
        # What the fill now records: the provider the deployments run on, beside the cloud the sheet was written to.
        estimators = {"observability": {
            "kind": "observability", "region": "eastus", "provider": "azure",
            "region_match": "AWS: us-east-1 (N. Virginia) (exact)", "readback": {"monthly_steady_state": 10.0},
        }}
        written = _written_regions(estimators)
        self.assertEqual(written[0]["provider"], "azure")
        recorded = integrity_score({"written_regions": written})
        row = next(item for item in recorded["checks"] if item["check"] == "Region fidelity")
        self.assertTrue(row["warning"])
        self.assertIn("written to another cloud", row["detail"])

    def test_an_alias_with_no_region_recorded_says_so(self):
        from quote_checks import _region_fidelity_row
        row = _region_fidelity_row([{"solution": "security", "region": None, "region_match": "AZURE: us-east-2 (alias)"}])
        self.assertTrue(row["warning"])
        self.assertIn("alias match and no region", row["detail"])
        self.assertNotIn("different region number", row["detail"])

    def test_a_fleet_sheet_region_makes_an_alias_checkable(self):
        from quote_checks import _region_fidelity_row
        row = _region_fidelity_row([{"solution": "security", "region": "us-east-2", "region_match": "AZURE: us-east-2 (Virginia) (alias)"}])
        self.assertTrue(row["passed"])
        self.assertEqual(row["score"], 1.0)

    @staticmethod
    def _retention_month(**facts):
        recorded = integrity_score({"retention_month": {"months": 12, "has_bulk": True, **facts}})
        return next(item for item in recorded["checks"] if item["check"] == "Retention month")

    def test_the_index_average_month_passes_without_a_warning(self):
        row = self._retention_month(months=4, index_count=537, index_days=93.97)
        self.assertTrue(row["passed"])
        self.assertEqual(row["score"], 1.0)
        self.assertNotIn("warning", row)
        self.assertIn("537 indexes", row["detail"])

    def test_a_named_retention_month_passes(self):
        row = self._retention_month(months=2, explicit_days=60)
        self.assertTrue(row["passed"])
        self.assertEqual(row["score"], 1.0)
        self.assertNotIn("warning", row)
        self.assertIn("--retention-days", row["detail"])

    def test_no_ascertainable_month_scores_zero_and_names_that_nothing_was_assumed(self):
        row = self._retention_month(index_count=None, months=None, basis="unascertained")
        self.assertTrue(row["passed"])      # the missing price fails Quote present, not this row
        self.assertTrue(row["warning"])
        self.assertEqual(row["score"], 0.0)
        self.assertIn("no default was assumed", row["detail"])
        self.assertIn("--retention-days", row["detail"])
        self.assertNotIn("30-day", row["detail"])

    def test_a_written_month_with_no_recorded_basis_is_a_warning(self):
        row = self._retention_month(index_count=None, months=1, basis=None)
        self.assertTrue(row["warning"])
        self.assertEqual(row["score"], 0.0)
        self.assertIn("basis was not recorded", row["detail"])

    def test_a_measured_delete_age_without_index_counts_is_a_measured_month(self):
        row = self._retention_month(index_count=None, months=4, basis="measured_age")
        self.assertTrue(row["passed"])
        self.assertEqual(row["score"], 1.0)
        self.assertNotIn("warning", row)
        self.assertIn("delete age the bundles measured", row["detail"])

    def test_the_retention_month_is_not_applicable_without_bulk_or_on_the_k360_backup(self):
        self.assertIsNone(self._retention_month(has_bulk=False)["passed"])
        recorded = integrity_score({"retention_month": None})
        row = next(item for item in recorded["checks"] if item["check"] == "Retention month")
        self.assertIsNone(row["passed"])

    def test_no_ascertainable_month_is_a_limit_a_note_and_no_written_month(self):
        from diag_account import apply_diagnostics
        acc = {
            "meta": {},
            "solutions": {"by_deployment": [{"name": "A", "solution": "observability", "gb": 10.0, "ecu": 100.0}]},
            "sizing": {"consolidated": {}},
            "storage": {},
        }
        # Bulk, but no index retention count and no measured delete age: nothing to ascertain retention from.
        rows = [{"name": "A", "bulk_gb_day": 5.0, "store_gb": 10.0, "retention_days": None,
                 "bulk_mix": {"logs": 5.0, "k8s_logs": 0.0, "metrics": 0.0, "traces": 0.0}}]
        out = apply_diagnostics(acc, rows)
        limits = (out["sizing"].get("diagnostics") or {}).get("estimation_limits") or []
        hit = next(row for row in limits if row.get("code") == "retention_month_missing")
        self.assertEqual(hit["severity"], "high")
        self.assertNotIn("30-day", hit["detail"].replace("No default", ""))
        self.assertIsNone(out["meta"]["estimate_months"])
        self.assertEqual(out["meta"]["retention_month_basis"], "unascertained")
        self.assertNotIn("retention_months", out["sizing"]["consolidated"]["observability"])
        self.assertFalse(any(row.get("code") == "retention_month_fallback" for row in limits))
        notes = " ".join(str(note) for note in out["sizing"].get("notes") or [])
        self.assertIn("no month was written", notes)
        self.assertIn("--retention-days", notes)
        self.assertNotIn("30-day default", notes)

    def test_the_proxy_log_fetch_runs_under_the_lock_and_a_deadline(self):
        import inspect
        import time
        import run_consolidation as rc
        seen = {}

        def read(deadline):
            seen["deadline"] = deadline
            seen["lock_held"] = path.exists()
            return {"regions": {}, "errors": {}}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".telemetry.lock"
            before = time.monotonic()
            out = rc.fetch_proxy_workloads(read, lock_path=path, budget_s=5, grace_s=1)
        self.assertEqual(out, {"regions": {}, "errors": {}})
        self.assertTrue(seen["lock_held"])
        self.assertTrue(before + 4.5 <= seen["deadline"] <= time.monotonic() + 5.5)
        # The production call goes through it. A bare regional_workloads call is the un-wired state.
        source = inspect.getsource(rc._diagnostics_document)
        self.assertIn("fetch_proxy_workloads(", source)
        self.assertNotIn("= search_inputs.regional_workloads(", source)

    def test_a_deadline_that_passes_mid_fetch_keeps_what_was_read_and_does_not_retry(self):
        import time
        import run_consolidation as rc
        calls = []

        def read(deadline):
            calls.append(deadline)
            while time.monotonic() < deadline:
                time.sleep(0.01)
            return {"regions": {"a": {"search": {}}}, "errors": {"b": "proxy logs exceeded their time budget"}}

        with tempfile.TemporaryDirectory() as tmp:
            out = rc.fetch_proxy_workloads(read, lock_path=Path(tmp) / ".lock", budget_s=0.1, grace_s=1, attempts=2)
        self.assertEqual(len(calls), 1)                      # the thread finished on its own: no second attempt
        self.assertEqual(set(out["regions"]), {"a"})
        self.assertEqual(set(out["errors"]), {"b"})

    def test_a_named_retention_is_recorded_even_when_it_converts_to_12_months(self):
        # 12 is also the "unset" value. 335 to 365 days must still be recorded as the override,
        # or the quote falls back to the index average and ignores what was asked for.
        import run_consolidation  # noqa: F401  (puts the k360 scripts on the path)
        from run_account_analysis import apply_retention_override
        from diag_inputs import whole_months
        from constants import DAYS_PER_MONTH
        for days in (14, 60, 335, 365):
            acc = {"meta": {}}
            months = whole_months(days / DAYS_PER_MONTH)
            key = apply_retention_override(acc, "acme_2026-11-24", months, days)
            self.assertEqual(acc["meta"]["retention_days"], days, days)
            self.assertEqual(key, f"acme_2026-11-24_{days}d")
            self.assertEqual(acc["meta"]["run_key"], key)
            if months == 12:
                self.assertNotIn("estimate_months", acc["meta"])      # 12 stays the unset value
            else:
                self.assertEqual(acc["meta"]["estimate_months"], months)
        unset = {"meta": {}}
        self.assertEqual(apply_retention_override(unset, "k", 12, None), "k")
        self.assertNotIn("retention_days", unset["meta"])

    def test_no_month_is_forced_and_the_sizing_step_skips_the_sheet(self):
        import argparse
        import run_consolidation as rc
        from estimate_paths import _months_for_sheet, estimator_skip
        args = argparse.Namespace(estimate_months=12)
        # Nothing ascertainable: the runner has no month to force. It must not fall back to 12.
        unascertained = {"meta": {"retention_month_basis": "unascertained", "estimate_months": None}}
        self.assertIsNone(rc._written_months(unascertained, args))
        # An index average and an explicit --retention-days are still written.
        self.assertEqual(rc._written_months({"meta": {"retention_month_basis": "index_average", "estimate_months": 4}}, args), 4)
        # A block with bulk and no month is skipped for retention, even when a month is forced.
        block = {"has_workload": True, "daily_raw_gb": 5.0}
        self.assertIsNone(_months_for_sheet(block, None))
        self.assertIsNone(_months_for_sheet(block, 6))
        self.assertEqual(estimator_skip(block, None), "retention")
        self.assertEqual(estimator_skip({**block, "retention_months": 3}, None), None)

    def test_no_month_leaves_the_solution_unquoted_and_the_quote_unverified(self):
        from quote_checks import integrity_score
        recorded = integrity_score({
            "retention_month": {"months": None, "has_bulk": True, "basis": "unascertained"},
            "solutions": [{"solution": "observability", "has_workload": True, "serverless_mo": 0.0, "direct": False}],
        })
        by_name = {row["check"]: row for row in recorded["checks"]}
        self.assertTrue(by_name["Retention month"]["warning"])
        self.assertFalse(by_name["Quote present"]["passed"])
        self.assertFalse(recorded["passed"])

    def test_a_measured_delete_age_is_used_when_no_index_was_counted(self):
        from diag_account import apply_diagnostics
        acc = {
            "meta": {},
            "solutions": {"by_deployment": [{"name": "A", "solution": "observability", "gb": 10.0, "ecu": 100.0}]},
            "sizing": {"consolidated": {}},
            "storage": {},
        }
        # A bundle parsed before the index fields existed: bulk and a measured delete age, no index count.
        rows = [{"name": "A", "bulk_gb_day": 5.0, "store_gb": 10.0, "retention_days": 120.0,
                 "bulk_mix": {"logs": 5.0, "k8s_logs": 0.0, "metrics": 0.0, "traces": 0.0}}]
        out = apply_diagnostics(acc, rows)
        limits = (out["sizing"].get("diagnostics") or {}).get("estimation_limits") or []
        self.assertFalse(any(row.get("code") == "retention_month_fallback" for row in limits))
        self.assertEqual(out["meta"]["retention_month_basis"], "measured_age")
        self.assertEqual(out["meta"]["estimate_months"], 4)
        self.assertEqual(out["sizing"]["consolidated"]["observability"]["retention_months"], 4)

    def test_a_counted_index_average_raises_no_fallback_limit(self):
        from diag_account import apply_diagnostics
        acc = {
            "meta": {},
            "solutions": {"by_deployment": [{"name": "A", "solution": "observability", "gb": 10.0, "ecu": 100.0}]},
            "sizing": {"consolidated": {}},
            "storage": {},
        }
        rows = [{"name": "A", "bulk_gb_day": 5.0, "store_gb": 10.0, "retention_days": 30.0,
                 "retention_index_count": 100, "retention_index_days_sum": 9000.0,
                 "bulk_mix": {"logs": 5.0, "k8s_logs": 0.0, "metrics": 0.0, "traces": 0.0}}]
        out = apply_diagnostics(acc, rows)
        limits = (out["sizing"].get("diagnostics") or {}).get("estimation_limits") or []
        self.assertNotIn("retention_month_fallback", [row.get("code") for row in limits])
        self.assertEqual(out["meta"]["estimate_months"], 3)

    def test_dirty_tree_refuses_the_run(self):
        import contextlib
        import io
        import run_consolidation as rc
        args = _guard_args()
        err = io.StringIO()
        with mock.patch.object(rc, "parse_args", return_value=args), mock.patch.object(
            rc, "method_stamp", return_value={"uncommitted_changes": True, "commit": "abc1234"},
        ):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as caught:
                    rc.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("uncommitted changes", err.getvalue())
        self.assertIn("--allow-dirty", err.getvalue())

    def test_a_tree_that_is_not_a_checkout_refuses_the_run(self):
        import contextlib
        import io
        import run_consolidation as rc
        args = _guard_args()
        err = io.StringIO()
        with mock.patch.object(rc, "parse_args", return_value=args), mock.patch.object(
            rc, "method_stamp", return_value={"uncommitted_changes": None, "commit": None},
        ):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as caught:
                    rc.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("not in a git checkout", err.getvalue())


class TelemetryFetchTests(unittest.TestCase):
    def test_a_timeout_is_retried_and_the_second_attempt_wins(self):
        import run_consolidation as rc
        import threading

        calls = []
        release = threading.Event()

        def fetch():
            calls.append(1)
            if len(calls) == 1:
                release.wait(5)
                return "late"
            return "ok"

        try:
            self.assertEqual(rc.fetch_with_budget(fetch, budget_s=0.05, attempts=2), "ok")
        finally:
            release.set()
        self.assertEqual(len(calls), 2)

    def test_every_attempt_failing_raises_the_last_error(self):
        import run_consolidation as rc

        def fetch():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            rc.fetch_with_budget(fetch, budget_s=1, attempts=2)

    def test_a_timeout_names_the_attempt(self):
        import run_consolidation as rc
        import threading

        release = threading.Event()
        try:
            with self.assertRaises(TimeoutError) as ctx:
                rc.fetch_with_budget(lambda: release.wait(5), budget_s=0.02, attempts=2)
        finally:
            release.set()
        self.assertIn("attempt 2 of 2", str(ctx.exception))

    def test_the_lock_is_exclusive_and_released(self):
        import run_consolidation as rc

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / ".telemetry.lock"
            with rc.telemetry_lock(path):
                self.assertTrue(path.exists())
            with rc.telemetry_lock(path):
                pass


if __name__ == "__main__":
    unittest.main()
