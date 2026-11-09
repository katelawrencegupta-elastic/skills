#!/usr/bin/env python3
"""Guided installer for the serverless_analyzer skills.

Checks this machine, links the three skills into ~/.cursor/skills, and can
create the Sheets and es3-estimator virtualenvs. It does not write API keys.
An existing .env is only inspected for key names.

  python3 install.py           # ask at each step
  python3 install.py --check   # report what is present and change nothing
  python3 install.py --yes     # accept the safe defaults; skip OAuth and prompts
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILLS = ("k360-org-report", "k360-account-analysis", "diag-serverless-consolidation")
SHEETS_DIR = Path.home() / ".config" / "cloud-tracker-sheets"
SHEETS_VENV = SHEETS_DIR / "venv"
SHEETS_PY = SHEETS_VENV / "bin" / "python"
SHEETS_REQ = ROOT / "k360-org-report" / "scripts" / "requirements-sheets.txt"
ES3_VENV = Path.home() / ".config" / "es3-estimator" / "venv"
ES3_PY = ES3_VENV / "bin" / "python"
ES3_PACKAGES = ("pandas", "numpy", "joblib", "scikit-learn==1.7.2")
REQUIRED_ENV = ("KIBANA_URL", "ELASTIC_API_KEY")
OPTIONAL_ENV = ("FOUND_ADMIN_API_KEY",)
CHROME_CANDIDATES = (
    "CHROME_PATH",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)
ENV_TEMPLATE = """# Read by the serverless_analyzer skills. Do not commit this file.
KIBANA_URL=https://
ELASTIC_API_KEY=
FOUND_ADMIN_API_KEY=
# FOUND_ADMIN_URL=https://admin.found.no
# FOUND_ADMIN_HOST_ALLOWLIST=
"""


def say(text: str = "") -> None:
    print(text, flush=True)


def section(title: str) -> None:
    say("")
    say(title)
    say("-" * len(title))


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        say("")
        return default or ""
    return answer or (default or "")


def yes(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})")
    if not answer:
        return default
    return answer.lower() in {"y", "yes"}


def run(cmd: list[str]) -> int:
    say("+ " + " ".join(cmd))
    return subprocess.call(cmd)


def python_ok() -> bool:
    return sys.version_info >= (3, 10)


def env_keys(path: Path) -> dict[str, bool]:
    """Return whether each assignment has a non-empty value. Values are not returned."""
    found: dict[str, str] = {}
    if not path.is_file():
        return {}
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        found[key.strip()] = value.strip().strip("'\"")
    return {key: bool(value) for key, value in found.items()}


def default_project() -> Path | None:
    candidate = Path.home() / "Documents" / "GItHub" / "cloud_tracking"
    if (candidate / ".env").is_file():
        return candidate
    return None


def link_one(name: str, dest_root: Path, assume_yes: bool) -> str:
    src = (ROOT / name).resolve()
    dest = dest_root / name
    if not src.is_dir():
        return f"missing source {src}"
    dest_root.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        current = dest.resolve()
        if current == src:
            return f"already linked {dest}"
        if assume_yes or yes(f"{dest} points at {current}. Point it at this repo?", default=False):
            dest.unlink()
            dest.symlink_to(src)
            return f"relinked {dest}"
        return f"left existing link {dest}"
    if dest.exists():
        if assume_yes or not yes(f"{dest} is a real directory. Replace it with a symlink to this repo?", default=False):
            return f"left existing directory {dest}"
        shutil.rmtree(dest)
    dest.symlink_to(src)
    return f"linked {dest}"


def link_skills(assume_yes: bool) -> list[str]:
    notes = []
    cursor = Path.home() / ".cursor" / "skills"
    if assume_yes or yes(f"Link the three skills into {cursor}?", default=True):
        for name in SKILLS:
            notes.append(link_one(name, cursor, assume_yes))
    else:
        notes.append("skipped Cursor skill links")
    claude = Path.home() / ".claude" / "skills"
    if not assume_yes and yes(f"Also link them into {claude}?", default=False):
        for name in SKILLS:
            notes.append(link_one(name, claude, assume_yes=False))
    return notes


def inspect_project(assume_yes: bool) -> list[str]:
    notes = []
    suggested = default_project()
    if assume_yes:
        project = suggested
    else:
        default = str(suggested) if suggested else ""
        raw = ask("Project directory that holds .env", default or None)
        project = Path(raw).expanduser() if raw else None
    if project is None:
        notes.append("no project directory chosen")
        return notes
    env_path = project / ".env"
    if env_path.is_file():
        keys = env_keys(env_path)
        for name in REQUIRED_ENV:
            notes.append(f"{name} {'set' if keys.get(name) else 'missing'} in {env_path}")
        for name in OPTIONAL_ENV:
            notes.append(f"{name} {'set' if keys.get(name) else 'missing'} in {env_path} (needed for a fresh diagnostic pull)")
        if keys.get("KIBANA_URL") and not _https_url(env_path, "KIBANA_URL"):
            notes.append("KIBANA_URL is set but does not start with https://")
        return notes
    notes.append(f"no .env at {env_path}")
    if assume_yes or not yes(f"Write an empty template at {env_path}?", default=False):
        notes.append("did not write a template")
        return notes
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(ENV_TEMPLATE)
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    notes.append(f"wrote template {env_path} (mode 0600). Fill the keys before a run")
    return notes


def _https_url(env_path: Path, key: str) -> bool:
    for raw in env_path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if line.startswith(f"{key}="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            return value.lower().startswith("https://")
    return False


def sheets_setup(assume_yes: bool) -> list[str]:
    notes = []
    if not SHEETS_REQ.is_file():
        return [f"requirements file missing: {SHEETS_REQ}"]
    create = assume_yes or yes(f"Create or update the Sheets venv at {SHEETS_VENV}?", default=True)
    if not create:
        notes.append("skipped Sheets venv")
    else:
        if not SHEETS_PY.is_file():
            SHEETS_DIR.mkdir(parents=True, exist_ok=True)
            code = run([sys.executable, "-m", "venv", str(SHEETS_VENV)])
            if code != 0:
                return notes + ["Sheets venv creation failed"]
        pip = SHEETS_VENV / "bin" / "pip"
        code = run([str(pip), "install", "-r", str(SHEETS_REQ)])
        notes.append("Sheets packages installed" if code == 0 else "Sheets package install failed")
    secret = SHEETS_DIR / "client_secret.json"
    token = SHEETS_DIR / "token.json"
    notes.append(f"client_secret.json {'found' if secret.is_file() else 'missing'} at {secret}")
    notes.append(f"token.json {'found' if token.is_file() else 'missing'} at {token}")
    if secret.is_file() and not assume_yes and yes("Run Google Sheets and Drive sign-in now? A browser opens.", default=False):
        code = run([str(SHEETS_PY), str(ROOT / "k360-org-report" / "scripts" / "sheets_auth.py")])
        notes.append("Sheets auth finished" if code == 0 else "Sheets auth failed")
    elif not secret.is_file():
        notes.append("download a Desktop OAuth client JSON from Google Cloud, enable the Sheets and Drive APIs, and save it as client_secret.json before signing in")
    return notes


def es3_setup(assume_yes: bool) -> list[str]:
    default_dir = Path.home() / "Documents" / "GItHub" / "es3-estimator-main"
    override = os.environ.get("ES3_ESTIMATOR_DIR")
    library = Path(override).expanduser() if override else default_dir
    present = (library / "estimator" / "lib").is_dir() or (library / "lib").is_dir()
    notes = [f"es3-estimator library {'found' if present else 'not found'} at {library}"]
    if assume_yes and not present:
        notes.append("skipped es3 venv because the library is not installed")
        return notes
    if not assume_yes and not yes("Set up the es3-estimator venv? Search quotes need it. Observability quotes do not.", default=present):
        notes.append("skipped es3 venv")
        return notes
    if not ES3_PY.is_file():
        ES3_VENV.parent.mkdir(parents=True, exist_ok=True)
        code = run([sys.executable, "-m", "venv", str(ES3_VENV)])
        if code != 0:
            return notes + ["es3 venv creation failed"]
    code = run([str(ES3_VENV / "bin" / "pip"), "install", *ES3_PACKAGES])
    notes.append("es3 packages installed" if code == 0 else "es3 package install failed")
    if not present:
        notes.append("clone es3-estimator so estimator/lib is on disk, or set ES3_ESTIMATOR_DIR")
    return notes


def chrome_note() -> str:
    override = os.environ.get("CHROME_PATH")
    candidates = [override] if override else []
    candidates.extend(path for path in CHROME_CANDIDATES[1:])
    for path in candidates:
        if path and Path(path).is_file():
            return f"Chrome found at {path}"
    return "Chrome not found. Install Google Chrome or set CHROME_PATH. HTML is still written when Chrome is missing"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="Report status and change nothing")
    ap.add_argument("--yes", action="store_true", help="Accept safe defaults and do not open a browser")
    args = ap.parse_args()
    if args.check and args.yes:
        say("Use either --check or --yes.")
        return 2

    say("Serverless analyzer setup")
    say(f"Source: {ROOT}")
    section("Python")
    if python_ok():
        say(f"Python {sys.version.split()[0]} is new enough.")
    else:
        say(f"Python {sys.version.split()[0]} is too old. These scripts need 3.10 or newer.")
        return 1

    if args.check:
        section("Cursor skills")
        cursor = Path.home() / ".cursor" / "skills"
        for name in SKILLS:
            dest = cursor / name
            src = (ROOT / name).resolve()
            if dest.is_symlink() and dest.resolve() == src:
                say(f"linked  {dest}")
            elif dest.exists():
                say(f"other   {dest}")
            else:
                say(f"missing {dest}")
        section("Project .env")
        for line in inspect_project(assume_yes=True):
            say(line)
        section("Sheets")
        say(f"venv {'found' if SHEETS_PY.is_file() else 'missing'} at {SHEETS_VENV}")
        say(f"client_secret.json {'found' if (SHEETS_DIR / 'client_secret.json').is_file() else 'missing'}")
        say(f"token.json {'found' if (SHEETS_DIR / 'token.json').is_file() else 'missing'}")
        section("Search estimator")
        for line in es3_setup_status():
            say(line)
        section("PDF")
        say(chrome_note())
        return 0

    notes: list[str] = []
    section("Skill links")
    say("The runners look in ~/.cursor/skills unless a path override is set.")
    notes.extend(link_skills(args.yes))
    section("K360 .env")
    say("The file stays in the project you run from. This installer does not print or invent key values.")
    notes.extend(inspect_project(args.yes))
    section("Google Sheets and Drive")
    say("Estimator copies and PDF upload use this venv. The OAuth client file stays in ~/.config.")
    notes.extend(sheets_setup(args.yes))
    section("Search estimator")
    say("Only a Search quote needs es3-estimator. scikit-learn stays at 1.7.2.")
    notes.extend(es3_setup(args.yes))
    section("PDF")
    notes.append(chrome_note())

    section("Result")
    for line in notes:
        say(f"- {line}")
    say("")
    say("Next: from the project that holds .env,")
    say("  python ~/.cursor/skills/k360-org-report/scripts/run_org_report.py --org \"ORG NAME\" --report exec")
    say("  python ~/.cursor/skills/k360-account-analysis/scripts/run_account_analysis.py --org \"ORG NAME\" --skip-diags --skip-sizing")
    say("  python ~/.cursor/skills/diag-serverless-consolidation/scripts/run_consolidation.py --org \"ORG NAME\" --reuse <org_report.json> --diags-dir <bundles>")
    say("Written steps are INSTALL.md. The script catalog is README.md.")
    return 0


def es3_setup_status() -> list[str]:
    default_dir = Path(os.environ.get("ES3_ESTIMATOR_DIR") or Path.home() / "Documents" / "GItHub" / "es3-estimator-main")
    present = (default_dir / "estimator" / "lib").is_dir() or (default_dir / "lib").is_dir()
    return [
        f"library {'found' if present else 'not found'} at {default_dir}",
        f"venv {'found' if ES3_PY.is_file() else 'missing'} at {ES3_VENV}",
    ]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("\nStopped.")
        raise SystemExit(130)
