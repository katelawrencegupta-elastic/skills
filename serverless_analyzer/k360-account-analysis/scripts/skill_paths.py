"""Find a sibling skill whether it is installed for Claude or for Cursor.

Search order: an environment override, the sibling next to the calling skill,
then ~/.claude/skills and ~/.cursor/skills, then a project .claude/skills or
.cursor/skills. Keep this file identical in each skill's scripts directory.
"""
from __future__ import annotations

import os
from pathlib import Path

_ENV = {
    "k360-org-report": "K360_ORG_SKILL_DIR",
    "k360-account-analysis": "K360_ACCOUNT_SKILL_DIR",
    "diag-serverless-consolidation": "DIAG_SKILL_DIR",
}


def find_skill(name: str, caller: Path) -> Path:
    """Return the skill directory that contains SKILL.md.

    An environment override is returned even when that directory is missing,
    so a bad override fails at the import that needs the skill.
    """
    override = os.environ.get(_ENV[name])
    if override:
        return Path(override).expanduser()
    caller = caller.resolve()
    if caller.is_file():
        caller = caller.parent
    skill_dir = caller.parent if caller.name == "scripts" else caller
    candidates = [skill_dir.parent / name]
    home = Path.home()
    for base in (
        home / ".claude" / "skills",
        home / ".cursor" / "skills",
        Path.cwd() / ".claude" / "skills",
        Path.cwd() / ".cursor" / "skills",
    ):
        candidates.append(base / name)
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if (path / "SKILL.md").is_file():
            return path
    return candidates[0]


def scripts_dir(skill: Path) -> Path:
    scripts = skill / "scripts"
    return scripts if scripts.is_dir() else skill


def artifacts_root() -> Path:
    """Parent of <customer>_<date>/ run folders.

    SERVERLESS_ANALYZER_ARTIFACTS overrides this. The default is
    serverless_analyzer/diag_artifacts, beside the three skill folders.
    CLOUD_TRACKER_ARTIFACTS_ROOT is honored only when the new variable is unset.
    """
    for name in ("SERVERLESS_ANALYZER_ARTIFACTS", "CLOUD_TRACKER_ARTIFACTS_ROOT"):
        env = os.environ.get(name)
        if env:
            return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "diag_artifacts"
