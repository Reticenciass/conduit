"""Safe tmux planning and optional explicitly-confirmed execution."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TmuxWindow:
    """A desired tmux window."""

    name: str
    command: str | None = None


DEFAULT_WINDOWS = (
    TmuxWindow("dashboard"),
    TmuxWindow("pivot"),
    TmuxWindow("notes"),
    TmuxWindow("loot"),
    TmuxWindow("logs"),
)


class TmuxManager:
    """Generate tmux commands and execute only after a caller confirms."""

    def __init__(self, session_name: str, runner: Any = None) -> None:
        self.session_name = session_name
        self.runner = runner or subprocess

    @staticmethod
    def available() -> bool:
        """Return whether tmux is installed locally."""

        return shutil.which("tmux") is not None

    def commands(self, windows: tuple[TmuxWindow, ...] = DEFAULT_WINDOWS) -> list[list[str]]:
        """Return a plan for a new session and its windows."""

        commands: list[list[str]] = [
            ["tmux", "new-session", "-d", "-s", self.session_name, "-n", windows[0].name]
        ]
        for window in windows[1:]:
            commands.append(["tmux", "new-window", "-t", self.session_name, "-n", window.name])
        for window in windows:
            if window.command:
                commands.append(
                    [
                        "tmux",
                        "send-keys",
                        "-t",
                        f"{self.session_name}:{window.name}",
                        window.command,
                        "Enter",
                    ]
                )
        return commands

    def commands_for_state(
        self,
        windows: tuple[TmuxWindow, ...] = DEFAULT_WINDOWS,
        *,
        existing_windows: Collection[str] = (),
        session_exists: bool = False,
    ) -> list[list[str]]:
        """Return only commands missing from the current tmux session."""

        if not session_exists:
            return self.commands(windows)
        existing = set(existing_windows)
        commands: list[list[str]] = []
        for window in windows:
            if window.name in existing:
                continue
            commands.append(["tmux", "new-window", "-t", self.session_name, "-n", window.name])
            if window.command:
                commands.append(
                    [
                        "tmux",
                        "send-keys",
                        "-t",
                        f"{self.session_name}:{window.name}",
                        window.command,
                        "Enter",
                    ]
                )
        return commands

    @staticmethod
    def windows_for_hosts(host_names: list[str]) -> tuple[TmuxWindow, ...]:
        """Extend the base workspace with deterministic per-host windows."""

        windows = [DEFAULT_WINDOWS[0]]
        for host in sorted(set(host_names)):
            safe = host.replace(" ", "-")
            windows.extend((TmuxWindow(f"shell-{safe}"), TmuxWindow(f"enum-{safe}")))
        windows.extend(DEFAULT_WINDOWS[1:])
        return tuple(windows)

    def session_exists(self) -> bool:
        """Return whether the configured tmux session exists."""

        if not self.available():
            return False
        result = self.runner.run(
            ["tmux", "has-session", "-t", self.session_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return bool(result.returncode == 0)

    def planned_commands(
        self, windows: tuple[TmuxWindow, ...] = DEFAULT_WINDOWS
    ) -> list[list[str]]:
        """Build a plan from the current session state without mutating it."""

        exists = self.session_exists()
        current = self.list_windows() if exists else []
        return self.commands_for_state(windows, existing_windows=current, session_exists=exists)

    def execute(self, windows: tuple[TmuxWindow, ...] = DEFAULT_WINDOWS) -> None:
        """Execute a previously reviewed plan."""

        if not self.available():
            raise RuntimeError("tmux não está instalado ou não está no PATH.")
        for command in self.planned_commands(windows):
            self.runner.run(command, check=True)

    def list_windows(self) -> list[str]:
        """Read current window names without modifying tmux."""

        if not self.available():
            return []
        completed = self.runner.run(
            ["tmux", "list-windows", "-t", self.session_name, "-F", "#W"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return []
        return [line for line in completed.stdout.splitlines() if line]
