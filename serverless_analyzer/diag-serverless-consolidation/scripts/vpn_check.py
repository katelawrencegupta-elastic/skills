#!/usr/bin/env python3
"""Reachability check for the Elastic Cloud admin host.

A TLS handshake to the pinned admin host is the check. The API key is not sent.
When the handshake fails, the message tells the operator to connect Aviatrix.
`--skip-vpn-check` remains the explicit bypass.
"""
from __future__ import annotations

import socket
import ssl
import subprocess
from pathlib import Path

APP_MARK = "Aviatrix VPN Client.app"
DEFAULT_HOST = "admin.found.no"


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


def _openvpn_running() -> bool:
    try:
        commands = _commands()
    except RuntimeError:
        return False
    return any(_is_openvpn(cmd) for cmd in commands)


def tunnel_reaches(host: str, port: int = 443, timeout: float = 5.0) -> tuple[bool, str]:
    """TLS connect only. No HTTP request and no API key."""
    host = (host or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host):
                return True, f"Admin host {host} accepted a TLS connection."
    except OSError as ex:
        if _openvpn_running():
            return False, (
                f"Could not reach {host}: {ex}. An openvpn process is running. "
                "Connect the Aviatrix tunnel before the diagnostic pull. "
                "Pass --skip-vpn-check to pull anyway."
            )
        return False, (
            f"Could not reach {host}: {ex}. Open Aviatrix VPN Client and connect before the diagnostic pull. "
            "Pass --skip-vpn-check to pull anyway."
        )


def aviatrix_status(host: str = DEFAULT_HOST, port: int = 443) -> tuple[bool, str]:
    """Return whether the admin host accepted TLS, and a sentence explaining the result."""
    return tunnel_reaches(host, port)


def require_aviatrix(host: str = DEFAULT_HOST) -> None:
    ok, detail = aviatrix_status(host)
    if not ok:
        raise SystemExit(detail)


if __name__ == "__main__":
    ok, detail = aviatrix_status()
    print(detail)
    raise SystemExit(0 if ok else 1)
