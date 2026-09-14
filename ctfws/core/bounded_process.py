"""Run a process with a deadline and a bounded output buffer."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass

MAX_PROCESS_OUTPUT_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    """Small, bounded result returned by a contextual process."""

    returncode: int | None
    timed_out: bool
    output: bytes
    output_truncated: bool


def run_bounded_process(
    command: Sequence[str], timeout_seconds: int | float, *, start_new_session: bool = True
) -> BoundedProcessResult:
    """Capture at most four MiB while ensuring the child is reaped."""

    process = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=start_new_session,
    )
    stream = process.stdout
    if stream is None:  # pragma: no cover - subprocess always provides PIPE here.
        raise RuntimeError("O processo não forneceu uma saída capturável.")
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    output = bytearray()
    output_truncated = False
    timed_out = False
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))

    def read_ready(wait: float) -> bool:
        nonlocal output_truncated
        events = selector.select(max(0.0, wait))
        if not events:
            return False
        for key, _ in events:
            try:
                chunk = os.read(key.fd, 64 * 1024)
            except OSError:
                chunk = b""
            if not chunk:
                try:
                    selector.unregister(key.fileobj)
                except Exception:
                    pass
                continue
            remaining = MAX_PROCESS_OUTPUT_BYTES - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
            if len(chunk) > max(0, remaining):
                output_truncated = True
        return True

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process(process)
                break
            if process.poll() is not None:
                # Drain data already queued by the kernel, but do not wait
                # forever for a descendant that inherited stdout.
                if not read_ready(0.1):
                    break
                continue
            read_ready(min(0.1, remaining))
        if process.poll() is None:
            _terminate_process(process)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_process(process)
            process.wait(timeout=2)
        # A final non-blocking drain preserves bytes emitted just before exit.
        for _ in range(4):
            if not selector.get_map() or not read_ready(0):
                break
    finally:
        selector.close()
        stream.close()
    return BoundedProcessResult(
        process.returncode,
        timed_out,
        bytes(output),
        output_truncated,
    )


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name != "nt" and hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name != "nt" and hasattr(os, "killpg"):
            os.killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        else:
            process.kill()
    except ProcessLookupError:
        pass
