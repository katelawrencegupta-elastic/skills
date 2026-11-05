#!/usr/bin/env python3
"""Aviatrix VPN Client tunnel check.

The menu-bar app and its AVPNC_RP helper stay running while the tunnel is down.
Aviatrix starts an `openvpn` process only after the tunnel connects. The diagnostic
pull reaches admin.found.no through that tunnel, so the pull waits for `openvpn`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

APP_MARK = "Aviatrix VPN Client.app"


def _commands() -> list[str]:
    try:
        out = subprocess.run(
            ["ps", "-ax", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as ex:
        raise RuntimeError(f"could not list processes ({ex})") from ex
    if out.returncode != 0:
        err = (out.stderr or "").strip() or f"ps exit {out.returncode}"
        raise RuntimeError(f"could not list processes ({err})")
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _is_openvpn(command: str) -> bool:
    exe = Path(command.split()[0]).name if command.split() else ""
    return exe == "openvpn" or exe.startswith("openvpn-")


def aviatrix_status() -> tuple[bool, str]:
    """Return whether the Aviatrix tunnel is up, and a sentence explaining the result."""
    commands = _commands()
    if any(_is_openvpn(cmd) for cmd in commands):
        return True, "Aviatrix VPN tunnel is up (openvpn is running)."
    app_open = any(APP_MARK in cmd and not _is_openvpn(cmd) for cmd in commands)
    if app_open:
        return False, (
            "Aviatrix VPN tunnel is down. The Aviatrix VPN Client app is open. "
            "Connect the tunnel before the diagnostic pull. Pass --skip-vpn-check to pull anyway."
        )
    return False, (
        "Aviatrix VPN tunnel is down. Open Aviatrix VPN Client and connect before the diagnostic pull. "
        "Pass --skip-vpn-check to pull anyway."
    )


def require_aviatrix() -> None:
    ok, detail = aviatrix_status()
    if not ok:
        raise SystemExit(detail)


if __name__ == "__main__":
    ok, detail = aviatrix_status()
    print(detail)
    raise SystemExit(0 if ok else 1)
