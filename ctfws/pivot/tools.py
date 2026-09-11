"""Tool detection and command generation; no command is executed here."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from ctfws.models.connection import ConnectionProfileRead
from ctfws.models.forward import ForwardCreate
from ctfws.pivot.adapters import TransportPlan, adapter_for


@dataclass(frozen=True, slots=True)
class PivotTool:
    """Availability information for one local pivot tool."""

    name: str
    executable: str | None
    installed: bool
    version: str | None = None


def detect_pivot_tools() -> list[PivotTool]:
    """Detect binaries without invoking them."""

    candidates = {
        "SSH": ("ssh",),
        "Chisel": ("chisel",),
        "Ligolo-ng": ("ligolo-proxy", "ligolo-ng"),
        "Proxychains": ("proxychains4", "proxychains"),
        "Netcat": ("nc", "ncat", "netcat"),
        "gsocket": ("gs-netcat",),
    }
    result: list[PivotTool] = []
    for name, executables in candidates.items():
        executable = next((shutil.which(item) for item in executables), None)
        result.append(PivotTool(name, executable, executable is not None, _version(executable)))
    return result


def _version(executable: str | None) -> str | None:
    """Read a short version string without invoking a shell or accepting input."""

    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (completed.stdout or completed.stderr).strip()
    return output.splitlines()[0][:200] if output else None


def ssh_target(user: str | None, address: str) -> str:
    """Build a shell-safe SSH destination."""

    return f"{user}@{address}" if user else address


def generate_forward_command(
    spec: ForwardCreate,
    via_address: str,
    via_user: str | None,
    profile: ConnectionProfileRead | None = None,
) -> str:
    """Generate a reviewed command for SSH, Chisel or Ligolo-style plans."""

    return build_forward_plan(spec, via_address, via_user, profile).command


def build_forward_plan(
    spec: ForwardCreate,
    via_address: str,
    via_user: str | None,
    profile: ConnectionProfileRead | None = None,
) -> TransportPlan:
    """Build a complete, non-executing transport plan for review and APIs."""

    return adapter_for(spec.tool).build(spec, via_address, via_user, profile)
