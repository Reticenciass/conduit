"""Network-output collector wrapper.

The collector delegates to the agent's fixed allowlist and never accepts an
arbitrary shell command.
"""

from __future__ import annotations

from typing import Any

from ctfws.agent import collect


def collect_network() -> dict[str, Any]:
    """Return only the fixed network outputs from the local agent."""

    payload = collect()
    outputs = payload.get("outputs", {})
    keys = ("ip_addr", "ip_route", "ip_neigh", "ss", "hosts", "resolv")
    return {key: outputs.get(key, "") for key in keys}
