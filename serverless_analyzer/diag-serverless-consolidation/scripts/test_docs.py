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
        if not isinstance(func, ast.Name) or func.id != "_integrity_row":
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

    def test_every_integrity_check_is_in_the_table(self):
        missing = _check_names() - _table_checks()
        self.assertEqual(missing, set())

    def test_every_brief_key_in_the_skill_is_emitted(self):
        source = (HERE / "run_consolidation.py").read_text()
        missing = [key for key in sorted(_brief_keys()) if f'"{key}"' not in source and f"'{key}'" not in source]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
