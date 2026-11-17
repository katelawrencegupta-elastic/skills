"""Load stripped consolidation fixtures. No network and no customer names."""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "tests" / "fixtures"
CASES = ("ref-a", "ref-b", "ref-c")


def load_case(name: str) -> tuple[dict, dict]:
    """Return (diags account, infra) for one stripped reference run."""
    folder = FIXTURES / name
    account = json.loads((folder / "diags_account.json").read_text())
    infra = json.loads((folder / "infra.json").read_text())
    return account, infra
