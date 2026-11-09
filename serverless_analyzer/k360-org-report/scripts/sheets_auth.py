#!/usr/bin/env python3
"""One-time Google OAuth for Sheets + Drive API (Desktop client).

Setup:
  1. Download OAuth client JSON (Desktop app) from GCP
  2. Save as ~/.config/cloud-tracker-sheets/client_secret.json
  3. Enable Google Sheets API + Google Drive API on the project
  4. Run:  python scripts/sheets_auth.py
  5. Browser opens → sign in → token saved to token.json

Env overrides:
  SHEETS_CLIENT_SECRET  path to client_secret.json
  SHEETS_TOKEN          path to token.json

Re-run auth after scope changes (Drive is required to copy estimator templates).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Drive needed to copy estimator templates into per-customer artifacts.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
DEFAULT_DIR = Path.home() / ".config" / "cloud-tracker-sheets"


def paths() -> tuple[Path, Path]:
    secret = Path(
        os.environ.get("SHEETS_CLIENT_SECRET")
        or (DEFAULT_DIR / "client_secret.json")
    )
    token = Path(os.environ.get("SHEETS_TOKEN") or (DEFAULT_DIR / "token.json"))
    return secret, token


def _lock_private(path: Path, mode: int) -> None:
    try:
        if path.exists():
            path.chmod(mode)
    except OSError:
        return


def get_creds(*, interactive: bool = True):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise SystemExit(
            "Missing packages. Install with:\n"
            "  pip install google-auth google-auth-oauthlib google-api-python-client"
        ) from e

    secret, token = paths()
    DEFAULT_DIR.mkdir(parents=True, exist_ok=True)
    _lock_private(DEFAULT_DIR, 0o700)
    _lock_private(secret, 0o600)
    _lock_private(token, 0o600)
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        # Force re-consent if stored token is missing Drive scope
        if creds and creds.scopes and not set(SCOPES).issubset(set(creds.scopes or [])):
            creds = None
            if token.exists():
                token.unlink()
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token.write_text(creds.to_json())
            _lock_private(token, 0o600)
            _lock_private(secret, 0o600)
            return creds
        except Exception:
            creds = None
    if not interactive:
        raise SystemExit(f"No valid token at {token}. Run: python scripts/sheets_auth.py")
    if not secret.exists():
        raise SystemExit(
            f"Client secret not found: {secret}\n"
            "Download Desktop OAuth client JSON from GCP and save it there,\n"
            "or set SHEETS_CLIENT_SECRET=/path/to/client_secret.json"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0)
    token.write_text(creds.to_json())
    _lock_private(token, 0o600)
    _lock_private(secret, 0o600)
    print(f"Saved token → {token}", file=sys.stderr)
    return creds


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--client-secret",
        help="Path to OAuth client JSON (default ~/.config/cloud-tracker-sheets/client_secret.json)",
    )
    args = ap.parse_args()
    if args.client_secret:
        os.environ["SHEETS_CLIENT_SECRET"] = args.client_secret
    secret, token = paths()
    print(f"client_secret: {secret} ({'OK' if secret.exists() else 'MISSING'})", file=sys.stderr)
    print(f"token:         {token}", file=sys.stderr)
    print(f"scopes:        {', '.join(SCOPES)}", file=sys.stderr)
    get_creds(interactive=True)
    print("Auth OK — you can run push_estimator.py", file=sys.stderr)


if __name__ == "__main__":
    main()
