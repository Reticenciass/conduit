"""Optional safe agent for authorized lab hosts.

The agent runs a fixed allowlist of read-only commands and emits JSON. It does
not accept arbitrary commands, open listeners, scan networks or transmit data.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
from datetime import UTC, datetime
from typing import Any

COMMANDS: dict[str, tuple[str, ...]] = {
    "whoami": ("whoami",),
    "id": ("id",),
    "hostname": ("hostname",),
    "hostname_f": ("hostname", "-f"),
    "uname": ("uname", "-a"),
    "os_release": ("cat", "/etc/os-release"),
    "ip_addr": ("ip", "addr"),
    "ip_route": ("ip", "route"),
    "ip_neigh": ("ip", "neigh"),
    "ss": ("ss", "-tunap"),
    "hosts": ("cat", "/etc/hosts"),
    "resolv": ("cat", "/etc/resolv.conf"),
}


def _run(args: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return f"[ctfws agent: unavailable: {error}]"
    return completed.stdout if completed.stdout else completed.stderr


def collect() -> dict[str, Any]:
    """Collect the fixed read-only dataset and normalize system facts."""

    outputs = {name: _run(args) for name, args in COMMANDS.items()}
    uid_match = re.search(r"uid=(\d+)", outputs["id"])
    groups_match = re.search(r"groups=(.*)", outputs["id"])
    os_release = outputs["os_release"]
    pretty_match = re.search(r"^PRETTY_NAME=(?:\"([^\"]+)\"|(.*))$", os_release, re.MULTILINE)
    facts = {
        "USER": outputs["whoami"].strip(),
        "UID": uid_match.group(1) if uid_match else "",
        "GROUPS": groups_match.group(1).strip() if groups_match else "",
        "HOSTNAME": outputs["hostname"].strip(),
        "FQDN": outputs["hostname_f"].strip(),
        "KERNEL": outputs["uname"].strip(),
        "ARCHITECTURE": platform.machine(),
        "DISTRIBUTION": (
            (pretty_match.group(1) or pretty_match.group(2)).strip() if pretty_match else None
        ),
    }
    return {
        "format": "ctfws-agent-v1",
        "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "system": facts,
        "outputs": outputs,
    }


def main() -> None:
    """Print one JSON document to stdout."""

    print(json.dumps(collect(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
