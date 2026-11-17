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
    "1 month",
    "_Serverless_",
    "0.60",
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


class DocLintTests(unittest.TestCase):
    def test_retired_phrases_are_gone(self):
        hits = []
        for path in DOCS:
            text = path.read_text().lower()
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
