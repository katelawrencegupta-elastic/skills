"""Doc lint for the consolidation skill.

Retired phrases stay in the docs until the Phase 4 rewrite, so that check fails
on purpose. Check names and brief keys are required now.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
DOCS = [
    SKILL / "SKILL.md",
    SKILL / "references" / "calculations.md",
    SKILL / "references" / "sizing-cells.md",
]
RETIRED = (
    "measured month",
    "at the measured month",
    "month 36",
    "month-36",
    "1 month",
    "cb254",
    "0.60",
    "serverless-consolidation-plan",
    "serverless_analyzer/diag_artifacts",
)


def _check_names() -> set[str]:
    tree = ast.parse((HERE / "quote_checks.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # _share_limit_row takes the check name as its first argument, so a check built that
        # way is just as real as one with the literal inline.
        if not isinstance(func, ast.Name) or func.id not in {"_integrity_row", "_share_limit_row"}:
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            names.add(node.args[0].value)
    return names


def _table_checks() -> set[str]:
    text = (SKILL / "references" / "calculations.md").read_text()
    found = set()
    in_table = False
    for line in text.splitlines():
        if line.startswith("| Check |"):
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            cell = line.split("|")[1].strip()
            if cell:
                found.add(cell)
            continue
        if in_table and not line.startswith("|"):
            break
    return found


def _brief_keys() -> set[str]:
    text = (SKILL / "SKILL.md").read_text()
    keys = set()
    for line in text.splitlines():
        if "per_cluster_plan" in line:
            keys.add("per_cluster_plan")
        if line.strip().startswith("Quote only when") or "from the brief:" in line:
            chunk = line
            while "`" in chunk:
                left, _, rest = chunk.partition("`")
                key, _, chunk = rest.partition("`")
                if key and key.isidentifier():
                    keys.add(key)
    return keys


class ModuleBoundaryTests(unittest.TestCase):
    """What a module publishes is a decision, not a side effect of a star import."""

    SCRIPTS = sorted(
        path for path in HERE.glob("*.py")
        if not path.name.startswith("test_")
    )

    def test_no_module_star_imports_a_sibling(self):
        offenders = [
            f"{path.name}:{index + 1}"
            for path in self.SCRIPTS
            for index, line in enumerate(path.read_text().splitlines())
            if line.startswith("from ") and line.rstrip().split("#")[0].rstrip().endswith("import *")
        ]
        self.assertEqual(
            offenders, [],
            "a star import re-exports every name it received, so which copy of a duplicated "
            "constant wins depends on import order. Import the names you use.",
        )

    def test_no_module_computes_all_from_globals(self):
        offenders = [
            path.name for path in self.SCRIPTS
            if "__all__ = [name for name in globals()" in path.read_text()
        ]
        self.assertEqual(offenders, [], "__all__ built from globals() re-exports imports and privates")

    def test_no_module_re_exports_another_module(self):
        import importlib
        import sys
        import types
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        leaked = {}
        for path in self.SCRIPTS:
            try:
                module = importlib.import_module(path.stem)
            except Exception:  # an entry point that needs credentials to import
                continue
            names = getattr(module, "__all__", None)
            if names is None:
                continue
            found = sorted(n for n in names if isinstance(getattr(module, n, None), types.ModuleType))
            if found:
                leaked[path.name] = found
        self.assertEqual(leaked, {}, "__all__ should not publish imported modules")

    def test_a_constant_has_one_home(self):
        import ast
        import collections
        seen = collections.defaultdict(list)
        for path in self.SCRIPTS:
            for node in ast.parse(path.read_text()).body:
                targets = []
                if isinstance(node, ast.Assign):
                    targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    targets = [node.target.id]
                for name in targets:
                    if name.lstrip("_").isupper() and len(name.lstrip("_")) > 2:
                        seen[name].append(path.name)
        # Per-module anchors, not shared values: HERE is each module's own directory, and
        # ORG_SKILL resolves the same path once per import family (the families never merge).
        # Path anchors each entry point resolves for itself. They are the same value, computed
        # from the module's own location, and the trees that define them never merge.
        local = {"HERE", "ORG_SKILL", "K360_SCRIPTS", "K360"}
        duplicated = {
            name: files for name, files in seen.items()
            if len(files) > 1 and name not in local
        }
        self.assertEqual(
            duplicated, {},
            "a constant defined in two modules drifts silently; give it one home",
        )


class DocLintTests(unittest.TestCase):
    def test_retired_phrases_are_gone(self):
        hits = []
        for path in DOCS:
            raw = path.read_text()
            if "_Serverless_" in raw:
                hits.append(f"{path.name}: _Serverless_")
            text = raw.lower()
            for phrase in RETIRED:
                if phrase.lower() in text:
                    hits.append(f"{path.name}: {phrase}")
        self.assertEqual(hits, [])

    def test_every_weighted_check_is_real_and_documented(self):
        """A weight on a name no check emits would silently never count."""
        import sys
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        from constants import CONFIDENCE_WEIGHTS
        emitted = _check_names()
        self.assertEqual(sorted(set(CONFIDENCE_WEIGHTS) - emitted), [])
        self.assertEqual(sorted(set(CONFIDENCE_WEIGHTS) - _table_checks()), [])
        text = (SKILL / "references" / "calculations.md").read_text()
        for name in CONFIDENCE_WEIGHTS:
            self.assertIn(name, text)

    def test_every_integrity_check_is_in_the_table(self):
        missing = _check_names() - _table_checks()
        self.assertEqual(missing, set())

    def test_every_brief_key_in_the_skill_is_emitted(self):
        source = (HERE / "run_consolidation.py").read_text()
        missing = [key for key in sorted(_brief_keys()) if f'"{key}"' not in source and f"'{key}'" not in source]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()


class NoRetentionDefaultTests(unittest.TestCase):
    """A retention month is measured or absent. No default age or month stands in for it."""

    RETIRED_DEFAULT = (
        "fallback_30d", "retention_month_fallback", "retention_30d_assumed", "retention_blended_share",
        "retention_assumed_share", "RETENTION_FALLBACK_DAYS", "RETENTION_ASSUMED_MAX_SHARE",
        "30-day default", "30-day assumed", "30-day assumption",
    )

    def test_docs_do_not_describe_a_retention_default(self):
        for doc in DOCS:
            text = doc.read_text()
            for phrase in self.RETIRED_DEFAULT:
                self.assertNotIn(phrase, text, f"{doc.name} still mentions {phrase!r}")

    def test_code_has_no_retention_default_constant_or_age(self):
        import constants
        self.assertFalse(hasattr(constants, "RETENTION_FALLBACK_DAYS"))
        self.assertFalse(hasattr(constants, "RETENTION_ASSUMED_MAX_SHARE"))
        source = (HERE / "admin_diags.py").read_text()
        self.assertNotIn("default_age", source)
        self.assertNotIn("fallback_30d", (HERE / "diag_inputs.py").read_text())

    def test_the_documented_limit_codes_are_the_emitted_ones(self):
        calc = (SKILL / "references" / "calculations.md").read_text()
        emitted = (HERE / "diag_account.py").read_text()
        for code in ("retention_month_missing", "retention_unaged"):
            self.assertIn(code, calc)
            self.assertIn(f'"{code}"', emitted)


class SearchWorkloadWordingTests(unittest.TestCase):
    """Search QPS, complexity and hour shares come from the proxy logs, and docs/s from the bundles."""

    FILES = ("es3_estimate.py", "search_sheet.py", "search_inputs.py", "diag_account.py", "consolidation.py",
             "consolidation_scope.py")
    RETIRED = ("day averages from index telemetry", "Cloud Tracker index telemetry", "from Cloud Tracker telemetry",
               "exact-window Cloud Tracker")

    def test_no_user_visible_text_names_cloud_tracker_as_the_search_workload_source(self):
        for name in self.FILES:
            text = (HERE / name).read_text()
            for phrase in self.RETIRED:
                self.assertNotIn(phrase, text, f"{name} still says {phrase!r}")

    def test_the_basis_note_says_what_was_measured(self):
        import es3_estimate
        proxy = es3_estimate.workload_basis_note({"source": es3_estimate.PROXY_SOURCE})
        self.assertIn("95th percentile", proxy)
        self.assertIn("median hourly p90", proxy)
        self.assertIn("not a day average", proxy)
        other = es3_estimate.workload_basis_note({"source": "k360"})
        self.assertNotIn("proxy", other)
        self.assertNotIn("proxy", es3_estimate.workload_basis_note(None))


class LimitCodesAreDocumentedTests(unittest.TestCase):
    """Every limit the scripts record is named in calculations.md, so a reader of a brief can look it up."""

    PATTERNS = (
        r'"code":\s*"([a-z_0-9]+)"',
        r'(?:note_limit|warn|stage)\(\s*[A-Za-z_\.]*,?\s*"([a-z_0-9]+)"',
        r'\bstage\(\s*"([a-z_0-9]+)"',
    )

    def _emitted(self) -> dict[str, set[str]]:
        import re
        found: dict[str, set[str]] = {}
        for path in sorted(HERE.glob("*.py")):
            if path.name.startswith("test_") or path.name == "_test_support.py":
                continue
            text = path.read_text()
            for pattern in self.PATTERNS:
                for match in re.finditer(pattern, text):
                    found.setdefault(match.group(1), set()).add(path.name)
        return found

    def test_every_emitted_limit_code_is_in_the_reference(self):
        doc = (SKILL / "references" / "calculations.md").read_text()
        emitted = self._emitted()
        self.assertGreater(len(emitted), 20)                      # the scan found the codes, not nothing
        missing = {code: sorted(files) for code, files in emitted.items() if f"`{code}`" not in doc}
        self.assertEqual(missing, {}, "limit codes recorded by the scripts and not named in calculations.md")


class SecurityGateFigureIsDocumentedTests(unittest.TestCase):
    """`cps_addon_monthly` is the gate figure, not the cell E73. The docs have to say so where a reader looks."""

    def test_every_doc_that_describes_the_gate_names_the_fold_and_the_kept_cell(self):
        for doc in DOCS:
            text = doc.read_text()
            self.assertIn("cps_addon_monthly", text, doc.name)
            self.assertIn("cps_addon_e73", text, f"{doc.name} does not say where the sheet's own E73 is kept")

    def test_the_fold_the_docs_describe_is_what_the_code_does(self):
        from quote_checks import money, security_readback
        base = {"monthly_steady_state": 1650.0, "cps_addon_monthly": 0, "ingest_monthly": 700.0,
                "retention_monthly": 300.0, "support_monthly": 150.0}
        folded = security_readback(base, 12)
        self.assertEqual(money(folded["cps_addon_monthly"]), 500.0)         # E73 (0) plus other_addons_monthly (500)
        self.assertEqual(folded["cps_addon_e73"], 0)                         # the cell itself
        self.assertTrue(folded["addon_note"])
        unchecked = security_readback({**base, "support_monthly": None}, 12)
        self.assertIsNone(unchecked["cps_addon_monthly"])                    # null when the lines cannot be checked
        self.assertEqual(unchecked["cps_addon_e73"], 0)
        untouched = security_readback({**base, "monthly_steady_state": 1150.0}, 12)
        self.assertEqual(untouched["cps_addon_monthly"], 0)                  # nothing folded: it is the cell
        self.assertNotIn("cps_addon_e73", untouched)
