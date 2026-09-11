"""Cross-platform process liveness helpers."""

from __future__ import annotations

import errno
import hashlib
import os
import subprocess
from dataclasses import dataclass
from typing import Any

try:
    import psutil as _psutil  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - exercised in minimal installs
    _psutil = None
psutil: Any = _psutil


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Stable-enough identity captured for a process owned by the motor."""

    pid: int
    create_time: float | None
    executable: str | None
    command_fingerprint: str | None


def process_identity(pid: int) -> ProcessIdentity | None:
    """Read process identity without sending a signal or changing state."""

    if pid < 1:
        return None
    if psutil is not None:
        try:
            process = psutil.Process(pid)
            command = "\x00".join(process.cmdline())
            return ProcessIdentity(
                pid,
                process.create_time(),
                process.exe() or None,
                hashlib.sha256(command.encode()).hexdigest() if command else None,
            )
        except (psutil.Error, OSError):
            return None
    return ProcessIdentity(pid, None, None, None) if is_process_alive(pid) else None


def process_matches(
    identity: ProcessIdentity | None,
    *,
    create_time: float | None,
    executable: str | None,
    command_fingerprint: str | None,
) -> bool:
    """Return whether a live process still matches its recorded identity."""

    if identity is None:
        return False
    if create_time is not None and identity.create_time is not None:
        if abs(identity.create_time - create_time) > 0.01:
            return False
    if executable and identity.executable:
        if os.path.normcase(os.path.abspath(identity.executable)) != os.path.normcase(
            os.path.abspath(executable)
        ):
            return False
    if command_fingerprint and identity.command_fingerprint:
        if command_fingerprint != identity.command_fingerprint:
            return False
    return True


def is_process_alive(pid: int) -> bool:
    """Check a PID without sending a terminating signal on Windows."""

    if pid < 1:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError:
            return False
        return f'"{pid}"' in result.stdout

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if error.errno == errno.EPERM:
            return True
        return False
    return True
