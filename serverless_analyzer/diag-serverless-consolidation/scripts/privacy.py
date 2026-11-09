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


def _default_artifacts_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "diag_artifacts"


def _exposed(repo: Path, rel: str) -> bool | None:
    """True when `rel` is tracked or a file inside it would be committed.

    None when git cannot answer. A tracked `.gitignore` inside the directory
    does not count as a customer file.
    """
    tracked = _git(repo, ["git", "ls-files", "--error-unmatch", "--", rel])
    ignored = _git(repo, ["git", "check-ignore", "-q", "--", rel])
    if ignored is None or tracked is None:
        return None
    probe = rel.rstrip("/") + "/customer.json"
    if ignored != 0:
        probe_ignored = _git(repo, ["git", "check-ignore", "-q", "--", probe])
        if probe_ignored is None:
            return None
        if probe_ignored != 0 and rel in {".env", "artifacts"}:
            slash = _git(repo, ["git", "check-ignore", "-q", "--", rel.rstrip("/") + "/"])
            if slash is None:
                return None
            ignored = slash
        else:
            ignored = probe_ignored
    if rel in {".env", "artifacts"}:
        return tracked == 0 or ignored != 0
    listed = _git_out(repo, ["git", "ls-files", "--", rel])
    if listed is None:
        return None
    tracked_customer = any(
        line and not line.endswith(".gitignore")
        for line in listed[1].splitlines()
    )
    return tracked_customer or ignored != 0


def _git_out(repo: Path, args: list[str]) -> tuple[int, str] | None:
    try:
        proc = subprocess.run(args, cwd=repo, capture_output=True, text=True)
    except OSError:
        return None
    return proc.returncode, proc.stdout


def unignored_secret_warnings(root: Path | None = None) -> list[str]:
    """Paths that a commit in this checkout could pick up.

    Warns when `.env` or `artifacts` is tracked, or when no ignore rule covers it.
    Also checks `serverless_analyzer/diag_artifacts`, the default run directory.
    Does not edit `.gitignore`.
    """
    start = (root or Path.cwd()).resolve()
    warnings: list[str] = []
    repo = _repo_root(start)
    if repo is not None:
        for rel in (".env", "artifacts"):
            exposed = _exposed(repo, rel)
            if exposed:
                warnings.append(
                    f"{rel} is tracked or not gitignored in {repo}. "
                    "Add `.env` and `artifacts/` to .gitignore before committing."
                )
    art = _default_artifacts_dir()
    art_repo = _repo_root(art)
    if art_repo is None:
        return warnings
    try:
        rel = art.relative_to(art_repo).as_posix()
    except ValueError:
        return warnings
    if repo is not None and art_repo == repo and rel in {".env", "artifacts"}:
        return warnings
    exposed = _exposed(art_repo, rel)
    if exposed:
        warnings.append(
            f"{rel} is tracked or not gitignored in {art_repo}. "
            "Add `.env` and `diag_artifacts/` to .gitignore before committing."
        )
    return warnings


def warn_unignored_secrets(root: Path | None = None) -> None:
    for line in unignored_secret_warnings(root):
        print(f"WARN: {line}", file=sys.stderr)
