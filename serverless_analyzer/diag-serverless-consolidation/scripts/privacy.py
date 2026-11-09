#!/usr/bin/env python3
"""File modes and a gitignore warning for customer artifacts. Stdlib only."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        return


def seal_dir(path: Path) -> None:
    """Create a run directory and keep it private to this user."""
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, 0o700)


def seal_text(path: Path, text: str) -> None:
    """Write a customer file and keep it private to this user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _chmod(path, 0o600)


def seal_file(path: Path) -> None:
    """Tighten a file that was already written."""
    _chmod(path, 0o600)


def _git(repo: Path, args: list[str]) -> int | None:
    try:
        proc = subprocess.run(args, cwd=repo, capture_output=True)
    except OSError:
        return None
    return proc.returncode


def _repo_root(start: Path) -> Path | None:
    for parent in [start, *start.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def unignored_secret_warnings(root: Path | None = None) -> list[str]:
    """Paths that a commit in this checkout could pick up.

    Warns when `.env` or `artifacts` is tracked, or when no ignore rule covers it.
    Does not edit `.gitignore`.
    """
    start = (root or Path.cwd()).resolve()
    repo = _repo_root(start)
    if repo is None:
        return []
    warnings: list[str] = []
    for rel in (".env", "artifacts"):
        tracked = _git(repo, ["git", "ls-files", "--error-unmatch", "--", rel])
        ignored = _git(repo, ["git", "check-ignore", "-q", "--", rel])
        if ignored is None or tracked is None:
            return []
        if rel == "artifacts" and ignored != 0:
            ignored = _git(repo, ["git", "check-ignore", "-q", "--", "artifacts/"])
            if ignored is None:
                return []
        if tracked == 0 or ignored != 0:
            warnings.append(
                f"{rel} is tracked or not gitignored in {repo}. "
                "Add `.env` and `artifacts/` to .gitignore before committing."
            )
    return warnings


def warn_unignored_secrets(root: Path | None = None) -> None:
    for line in unignored_secret_warnings(root):
        print(f"WARN: {line}", file=sys.stderr)
