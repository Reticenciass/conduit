"""Non-executing shell upgrade suggestions."""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolAvailability:
    """Whether a local command appears available."""

    name: str
    path: str | None

    @property
    def available(self) -> bool:
        return self.path is not None


@dataclass(frozen=True, slots=True)
class ShellSuggestion:
    """A command suggestion that must be copied/executed by the operator."""

    tool: str
    command: str
    instructions: str


TOOLS = ("python", "python3", "perl", "ruby", "bash", "script", "socat")


def detect_tools() -> list[ToolAvailability]:
    """Detect local helper binaries without invoking them."""

    return [ToolAvailability(name, shutil.which(name)) for name in TOOLS]


def suggestions(available: list[ToolAvailability] | None = None) -> list[ShellSuggestion]:
    """Build common interactive-shell suggestions from detected tools."""

    tools = {item.name for item in (available or detect_tools()) if item.available}
    result: list[ShellSuggestion] = []
    if "python3" in tools or "python" in tools:
        python = "python3" if "python3" in tools else "python"
        result.append(
            ShellSuggestion(
                tool=python,
                command=f"{python} -c 'import pty; pty.spawn(\"/bin/bash\")'",
                instructions=(
                    "Depois ajuste o terminal local conforme o procedimento do seu laboratório."
                ),
            )
        )
    if "script" in tools:
        result.append(
            ShellSuggestion(
                tool="script",
                command="script -qc /bin/bash /dev/null",
                instructions="Útil quando o alvo fornece o utilitário script.",
            )
        )
    if "socat" in tools:
        result.append(
            ShellSuggestion(
                tool="socat",
                command="socat exec:'bash -li',pty,stderr,setsid,sigint,sane",
                instructions=(
                    "Verifique o contexto do laboratório antes de usar qualquer comando gerado."
                ),
            )
        )
    return result
