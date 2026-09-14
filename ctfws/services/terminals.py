"""Managed local and SSH terminals with PTY and multi-viewer support."""

from __future__ import annotations

import asyncio
import os
import queue
import shutil
import signal
import sqlite3
import subprocess
import threading
import uuid
from builtins import list as builtin_list
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from ctfws.core.errors import EntityNotFoundError
from ctfws.models.connection import ConnectionProfileRead
from ctfws.models.terminal import (
    TerminalCreate,
    TerminalKind,
    TerminalRead,
    TerminalSharing,
    TerminalStatus,
)
from ctfws.services.connections import ConnectionService
from ctfws.services.workspace import WorkspaceService

_pty: Any
try:
    import pty as _pty
except ImportError:  # pragma: no cover - pty is Unix-only
    _pty = None
pty: Any = _pty
_QueueItem = TypeVar("_QueueItem")


@dataclass(slots=True)
class RuntimeTerminal:
    """Process handles and bounded output queues kept out of SQLite."""

    terminal_id: int
    process: Any
    output: queue.Queue[bytes] = field(default_factory=lambda: queue.Queue(maxsize=512))
    reader: threading.Thread | None = None
    master_fd: int | None = None
    subscribers: dict[str, queue.Queue[TerminalFrame]] = field(default_factory=dict)
    control_owner: str | None = None
    eof: bool = False
    stopping: bool = False
    sequence: int = 0
    history: deque[tuple[int, bytes]] = field(default_factory=deque)
    history_bytes: int = 0
    remote_process: Any | None = None
    remote_task: asyncio.Task[None] | None = None
    loop: asyncio.AbstractEventLoop | None = None


@dataclass(frozen=True, slots=True)
class TerminalFrame:
    """One bounded output frame with a monotonic sequence number."""

    sequence: int
    data: bytes
    gap: bool = False


class TerminalManager:
    """Own interactive processes and expose independent byte-oriented views."""

    def __init__(self, workspace: WorkspaceService, ssh_manager: Any | None = None) -> None:
        self.workspace = workspace
        self.ssh_manager = ssh_manager
        self._terminals: dict[int, RuntimeTerminal] = {}
        self._lock = threading.RLock()

    def recover(self) -> None:
        """Mark terminals from a previous motor as exited after lock acquisition."""

        for terminal in self.workspace.terminals.list():
            if terminal.status in {TerminalStatus.STARTING, TerminalStatus.ACTIVE}:
                self.workspace.terminals.update_runtime(
                    terminal.id,
                    status=TerminalStatus.EXITED,
                    pid=None,
                    exit_code=None,
                    clear_engine_id=True,
                )

    def list(self) -> list[TerminalRead]:
        self._reconcile()
        return self.workspace.terminals.list()

    def get(self, terminal_id: int) -> TerminalRead:
        self._reconcile()
        terminal = self.workspace.terminals.get(terminal_id)
        if terminal is None:
            raise EntityNotFoundError(f"Terminal {terminal_id} não encontrado.")
        return terminal

    def runtime_available(self, terminal_id: int) -> bool:
        """Return whether this motor still owns the live terminal session."""

        with self._lock:
            return terminal_id in self._terminals

    async def start_async(
        self, data: TerminalCreate, owner_subject: str | None = None
    ) -> TerminalRead:
        """Start a terminal, using a shared AsyncSSH PTY when available."""

        if (
            data.kind == TerminalKind.SSH
            and self.ssh_manager is not None
            and self.ssh_manager.available
        ):
            return await self._start_remote(data, owner_subject)
        return self.start(data, owner_subject=owner_subject)

    def start(self, data: TerminalCreate, owner_subject: str | None = None) -> TerminalRead:
        profile: ConnectionProfileRead | None = None
        if data.kind == TerminalKind.SSH:
            if data.connection_id is None:
                raise ValueError("Um terminal SSH exige connection_id.")
            profile = ConnectionService(self.workspace).get(data.connection_id)
            context_label = f"REMOTO → {profile.user}@{profile.host}:{profile.port}"
        else:
            runtime_name = "Windows" if os.name == "nt" else "Kali/Linux"
            context_label = f"LOCAL → {runtime_name} / motor do workspace"

        terminal = self.workspace.terminals.create(
            data, context_label, owner_subject, self.workspace.engine_id
        )
        command = self._command(data, profile)
        cwd = self._cwd(data.cwd)
        master_fd: int | None = None
        slave_fd: int | None = None
        try:
            if os.name != "nt" and pty is not None:
                master_fd, slave_fd = pty.openpty()
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdin=slave_fd if slave_fd is not None else subprocess.PIPE,
                stdout=slave_fd if slave_fd is not None else subprocess.PIPE,
                stderr=slave_fd if slave_fd is not None else subprocess.STDOUT,
                bufsize=0,
                start_new_session=(os.name != "nt"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
        except Exception as error:
            for descriptor in (master_fd, slave_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            self.workspace.terminals.update_runtime(
                terminal.id, status=TerminalStatus.ERROR, exit_code=None, clear_engine_id=True
            )
            raise RuntimeError(f"Não foi possível abrir o terminal: {error}") from error
        if slave_fd is not None:
            os.close(slave_fd)

        runtime = RuntimeTerminal(terminal.id, process, master_fd=master_fd)
        runtime.reader = threading.Thread(
            target=self._pump_output,
            args=(runtime,),
            name=f"ctfws-terminal-{terminal.id}",
            daemon=True,
        )
        with self._lock:
            self._terminals[terminal.id] = runtime
        self.workspace.terminals.update_runtime(
            terminal.id,
            status=TerminalStatus.ACTIVE,
            pid=process.pid,
            engine_id=self.workspace.engine_id,
        )
        runtime.reader.start()
        return self.get(terminal.id)

    async def _start_remote(
        self, data: TerminalCreate, owner_subject: str | None = None
    ) -> TerminalRead:
        if data.connection_id is None:
            raise ValueError("Um terminal SSH exige connection_id.")
        profile = ConnectionService(self.workspace).get(data.connection_id)
        terminal = self.workspace.terminals.create(
            data,
            f"REMOTO → {profile.user}@{profile.host}:{profile.port}",
            owner_subject,
            self.workspace.engine_id,
        )
        try:
            manager = self.ssh_manager
            if manager is None:
                raise RuntimeError("O gerenciador SSH não está disponível.")
            connection = await manager.connect(profile.id)
            process = await connection.create_process(
                term_type="xterm",
                term_size=(80, 24),
                encoding=None,
            )
        except Exception as error:
            self.workspace.terminals.update_runtime(
                terminal.id, status=TerminalStatus.ERROR, exit_code=None, clear_engine_id=True
            )
            raise RuntimeError(f"Não foi possível abrir o terminal SSH: {error}") from error
        runtime = RuntimeTerminal(
            terminal.id,
            process,
            remote_process=process,
            loop=asyncio.get_running_loop(),
        )
        runtime.remote_task = asyncio.create_task(
            self._pump_remote_output(runtime), name=f"ctfws-ssh-terminal-{terminal.id}"
        )
        with self._lock:
            self._terminals[terminal.id] = runtime
        self.workspace.terminals.update_runtime(
            terminal.id,
            status=TerminalStatus.ACTIVE,
            pid=None,
            engine_id=self.workspace.engine_id,
        )
        return self.get(terminal.id)

    def set_sharing(self, terminal_id: int, shared: bool) -> TerminalRead:
        """Publish or hide the terminal from observer WebSocket sessions."""

        return self.workspace.terminals.set_sharing(
            terminal_id,
            TerminalSharing.SHARED if shared else TerminalSharing.PRIVATE,
        )

    def rename(self, terminal_id: int, name: str) -> TerminalRead:
        """Rename a terminal label while preserving its process and output."""

        try:
            return self.workspace.terminals.rename(terminal_id, name)
        except sqlite3.IntegrityError as error:
            raise ValueError("Já existe outro terminal com esse nome neste workspace.") from error

    def write(self, terminal_id: int, data: bytes, owner: str | None = None) -> None:
        runtime = self._runtime(terminal_id)
        if owner is not None and runtime.control_owner not in {None, owner}:
            raise PermissionError("Outro cliente controla a entrada deste terminal.")
        if owner is not None:
            runtime.control_owner = owner
        if runtime.remote_process is not None:
            if runtime.eof or runtime.stopping:
                raise RuntimeError("O terminal já encerrou.")
            writer = getattr(runtime.remote_process, "stdin", None)
            if writer is None:
                raise RuntimeError("O terminal SSH não aceita entrada.")
            writer.write(data)
            return
        if runtime.master_fd is not None:
            if runtime.eof:
                raise RuntimeError("O terminal já encerrou.")
            os.write(runtime.master_fd, data)
            return
        if runtime.process.stdin is None:
            raise RuntimeError("O terminal não aceita entrada.")
        if runtime.process.poll() is not None:
            raise RuntimeError("O terminal já encerrou.")
        runtime.process.stdin.write(data)
        runtime.process.stdin.flush()

    async def write_async(self, terminal_id: int, data: bytes, owner: str | None = None) -> None:
        """Write input and honor AsyncSSH flow control when a remote PTY is used."""

        self.write(terminal_id, data, owner=owner)
        runtime = self._runtime(terminal_id)
        if runtime.remote_process is None:
            return
        writer = getattr(runtime.remote_process, "stdin", None)
        drain = getattr(writer, "drain", None)
        if drain is None:
            return
        result = drain()
        if hasattr(result, "__await__"):
            await result

    def read(self, terminal_id: int, timeout: float = 0.1) -> bytes | None:
        runtime = self._runtime(terminal_id)
        return self._read_queue(runtime.output, timeout)

    def subscribe(self, terminal_id: int, after_sequence: int = 0) -> str:
        """Create an independent output queue for one viewer."""

        runtime = self._runtime(terminal_id)
        token = uuid.uuid4().hex
        subscriber: queue.Queue[TerminalFrame] = queue.Queue(maxsize=512)
        with self._lock:
            runtime.subscribers[token] = subscriber
            pending_history = [
                (sequence, data) for sequence, data in runtime.history if sequence > after_sequence
            ]
            first_sequence = pending_history[0][0] if pending_history else runtime.sequence + 1
            gap = after_sequence > 0 and first_sequence > after_sequence + 1
            for sequence, data in pending_history:
                try:
                    subscriber.put_nowait(
                        TerminalFrame(sequence, data, gap=gap and sequence == first_sequence)
                    )
                except queue.Full:
                    # The history and subscriber queue have the same bound;
                    # this guard keeps the contract safe if that changes.
                    subscriber.get_nowait()
                    subscriber.put_nowait(TerminalFrame(sequence, data, gap=True))
                    break
        return token

    def unsubscribe(self, terminal_id: int, token: str) -> None:
        """Remove one viewer and release its input control if held."""

        runtime = self._runtime(terminal_id)
        with self._lock:
            runtime.subscribers.pop(token, None)
            if runtime.control_owner == token:
                runtime.control_owner = None

    def read_subscriber(self, terminal_id: int, token: str, timeout: float = 0.1) -> bytes | None:
        """Read only the queue belonging to one viewer."""

        frame = self.read_subscriber_frame(terminal_id, token, timeout)
        return frame.data if frame is not None else None

    def read_subscriber_frame(
        self, terminal_id: int, token: str, timeout: float = 0.1
    ) -> TerminalFrame | None:
        """Read a sequenced frame for one viewer without sharing its queue."""

        runtime = self._runtime(terminal_id)
        with self._lock:
            output = runtime.subscribers.get(token)
        if output is None:
            raise EntityNotFoundError("Visualização do terminal não encontrada.")
        return self._read_queue(output, timeout)

    def acquire_control(self, terminal_id: int, token: str) -> bool:
        """Claim exclusive terminal input for one viewer."""

        runtime = self._runtime(terminal_id)
        with self._lock:
            if runtime.control_owner in {None, token}:
                runtime.control_owner = token
                return True
            return False

    def has_control(self, terminal_id: int, token: str) -> bool:
        """Return whether a viewer currently owns input control."""

        runtime = self._runtime(terminal_id)
        with self._lock:
            return runtime.control_owner == token

    def resize(self, terminal_id: int, columns: int, rows: int) -> None:
        """Resize a POSIX PTY; Windows pipe terminals ignore the request."""

        if not 1 <= columns <= 500 or not 1 <= rows <= 500:
            raise ValueError("O tamanho do terminal precisa estar entre 1 e 500.")
        runtime = self._runtime(terminal_id)
        if runtime.remote_process is not None:
            resize = getattr(runtime.remote_process, "change_terminal_size", None)
            if resize is not None:
                resize(columns, rows)
            return
        if runtime.master_fd is None or os.name == "nt":
            return
        import fcntl as _fcntl
        import struct
        import termios as _termios

        fcntl: Any = _fcntl
        termios: Any = _termios

        fcntl.ioctl(
            runtime.master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, columns, 0, 0),
        )

    def is_drained(self, terminal_id: int) -> bool:
        """Return whether the default and viewer queues are empty."""

        return self._is_drained_runtime(self._runtime(terminal_id))

    def close(self, terminal_id: int) -> TerminalRead:
        try:
            runtime = self._runtime(terminal_id)
        except EntityNotFoundError:
            # A historical terminal has no process to kill. Marking its
            # record closed keeps the UI action idempotent and preserves the
            # original lifecycle in the database.
            current = self.workspace.terminals.get(terminal_id)
            if current is None:
                raise
            return self.workspace.terminals.update_runtime(
                terminal_id,
                status=TerminalStatus.CLOSED,
                pid=None,
                exit_code=current.exit_code,
                clear_engine_id=True,
            )
        runtime.stopping = True
        if runtime.remote_process is not None:
            self._close_remote_sync(runtime)
            return self.workspace.terminals.update_runtime(
                terminal_id,
                status=TerminalStatus.CLOSED,
                pid=None,
                exit_code=None,
                clear_engine_id=True,
            )
        process = runtime.process
        if process.poll() is None:
            if os.name == "nt":
                process.terminate()
            else:
                killpg = getattr(os, "killpg", None)
                if killpg is not None:
                    try:
                        killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        exit_code = process.poll()
        if runtime.reader is not None and runtime.reader is not threading.current_thread():
            runtime.reader.join(timeout=1)
        if runtime.master_fd is not None:
            try:
                os.close(runtime.master_fd)
            except OSError:
                pass
            runtime.master_fd = None
        return self.workspace.terminals.update_runtime(
            terminal_id,
            status=TerminalStatus.CLOSED,
            pid=None,
            exit_code=exit_code,
            clear_engine_id=True,
        )

    def _runtime(self, terminal_id: int) -> RuntimeTerminal:
        with self._lock:
            runtime = self._terminals.get(terminal_id)
        if runtime is None:
            raise EntityNotFoundError(f"Terminal {terminal_id} não está ativo no motor.")
        return runtime

    def _reconcile(self) -> None:
        with self._lock:
            items = list(self._terminals.items())
        for terminal_id, runtime in items:
            if runtime.remote_process is not None:
                current = self.workspace.terminals.get(terminal_id)
                if (
                    runtime.eof
                    and current is not None
                    and not runtime.subscribers
                    and self._is_drained_runtime(runtime)
                ):
                    with self._lock:
                        self._terminals.pop(terminal_id, None)
                continue
            exit_code = runtime.process.poll()
            if exit_code is None:
                continue
            # The reader may still have bytes in the PTY after poll() reports
            # the child exit.  Keep ownership until EOF so the final output
            # can be delivered to every viewer.
            if not runtime.eof:
                continue
            current = self.workspace.terminals.get(terminal_id)
            if current is not None and current.status in {
                TerminalStatus.STARTING,
                TerminalStatus.ACTIVE,
            }:
                self.workspace.terminals.update_runtime(
                    terminal_id,
                    status=TerminalStatus.EXITED,
                    pid=None,
                    exit_code=exit_code,
                    clear_engine_id=True,
                )
            if not runtime.subscribers and self._is_drained_runtime(runtime):
                with self._lock:
                    self._terminals.pop(terminal_id, None)

    @staticmethod
    def _read_queue(output: queue.Queue[_QueueItem], timeout: float) -> _QueueItem | None:
        try:
            return output.get(timeout=timeout)
        except queue.Empty:
            return None

    @staticmethod
    def _record_history(runtime: RuntimeTerminal, frame: TerminalFrame) -> None:
        """Keep a bounded byte history without letting frame count hide size."""

        runtime.history.append((frame.sequence, frame.data))
        runtime.history_bytes += len(frame.data)
        while runtime.history and runtime.history_bytes > 4 * 1024 * 1024:
            _sequence, data = runtime.history.popleft()
            runtime.history_bytes -= len(data)

    @staticmethod
    def _is_drained_runtime(runtime: RuntimeTerminal) -> bool:
        if runtime.subscribers:
            return all(output.empty() for output in runtime.subscribers.values())
        return runtime.output.empty()

    def _pump_output(self, runtime: RuntimeTerminal) -> None:
        try:
            while True:
                if runtime.master_fd is not None:
                    try:
                        chunk = os.read(runtime.master_fd, 4096)
                    except OSError:
                        chunk = b""
                else:
                    stream = runtime.process.stdout
                    chunk = stream.read(4096) if stream is not None else b""
                if not chunk:
                    break
                with self._lock:
                    runtime.sequence += 1
                    frame = TerminalFrame(runtime.sequence, chunk)
                    self._record_history(runtime, frame)
                    subscribers = list(runtime.subscribers.items())
                if not subscribers:
                    self._put_with_backpressure(runtime, chunk)
                else:
                    for token, output in subscribers:
                        self._put_subscriber_with_backpressure(runtime, token, output, frame)
        finally:
            with self._lock:
                runtime.eof = True
            if runtime.master_fd is not None:
                try:
                    os.close(runtime.master_fd)
                except OSError:
                    pass
                runtime.master_fd = None
            current = self.workspace.terminals.get(runtime.terminal_id)
            if current is not None and current.status in {
                TerminalStatus.STARTING,
                TerminalStatus.ACTIVE,
            }:
                self.workspace.terminals.update_runtime(
                    runtime.terminal_id,
                    status=TerminalStatus.EXITED,
                    pid=None,
                    exit_code=runtime.process.poll(),
                    clear_engine_id=True,
                )

    async def _pump_remote_output(self, runtime: RuntimeTerminal) -> None:
        """Read an AsyncSSH PTY without blocking the event loop."""

        stream = getattr(runtime.remote_process, "stdout", None)
        try:
            while stream is not None:
                chunk = await stream.read(4096)
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                if not chunk:
                    break
                with self._lock:
                    runtime.sequence += 1
                    frame = TerminalFrame(runtime.sequence, chunk)
                    self._record_history(runtime, frame)
                    subscribers = list(runtime.subscribers.items())
                if not subscribers:
                    await self._put_async_with_backpressure(runtime, chunk)
                else:
                    for token, output in subscribers:
                        await self._put_async_subscriber(runtime, token, output, frame)
        finally:
            runtime.eof = True
            current = self.workspace.terminals.get(runtime.terminal_id)
            if current is not None and current.status in {
                TerminalStatus.STARTING,
                TerminalStatus.ACTIVE,
            }:
                self.workspace.terminals.update_runtime(
                    runtime.terminal_id,
                    status=TerminalStatus.EXITED,
                    pid=None,
                    exit_code=getattr(runtime.remote_process, "exit_status", None),
                    clear_engine_id=True,
                )

    async def _put_async_subscriber(
        self,
        runtime: RuntimeTerminal,
        token: str,
        output: queue.Queue[TerminalFrame],
        frame: TerminalFrame,
    ) -> None:
        try:
            output.put_nowait(frame)
        except queue.Full:
            self._drop_slow_subscriber_frame(runtime, token, output, frame)

    async def _put_async_with_backpressure(self, runtime: RuntimeTerminal, chunk: bytes) -> None:
        self._put_with_backpressure(runtime, chunk)

    async def _close_remote(self, runtime: RuntimeTerminal) -> None:
        process = runtime.remote_process
        if process is None:
            return
        close = getattr(process, "close", None)
        if close is not None:
            close()
        wait_closed = getattr(process, "wait_closed", None)
        if wait_closed is not None:
            result = wait_closed()
            if hasattr(result, "__await__"):
                await result

    def _close_remote_sync(self, runtime: RuntimeTerminal) -> None:
        loop = runtime.loop
        if loop is None or not loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._close_remote(runtime), loop)
        try:
            future.result(timeout=3)
        except Exception:
            future.cancel()

    def _put_subscriber_with_backpressure(
        self,
        runtime: RuntimeTerminal,
        token: str,
        output: queue.Queue[TerminalFrame],
        frame: TerminalFrame,
    ) -> None:
        """Deliver without allowing one slow viewer to block the PTY reader."""

        try:
            output.put_nowait(frame)
        except queue.Full:
            self._drop_slow_subscriber_frame(runtime, token, output, frame)

    def _drop_slow_subscriber_frame(
        self,
        runtime: RuntimeTerminal,
        token: str,
        output: queue.Queue[TerminalFrame],
        frame: TerminalFrame,
    ) -> None:
        """Replace old data with a gap marker when a viewer cannot keep up."""

        with self._lock:
            # The queue identity is checked before dropping anything.  The
            # caller can race with unsubscribe, and a stale queue must never
            # receive more data after that point.
            if runtime.subscribers.get(token) is not output:
                return
            try:
                output.get_nowait()
            except queue.Empty:
                pass
            try:
                output.put_nowait(TerminalFrame(frame.sequence, frame.data, gap=True))
            except queue.Full:
                pass

    @staticmethod
    def _put_with_backpressure(runtime: RuntimeTerminal, chunk: bytes) -> None:
        """Keep the legacy queue bounded without blocking the PTY reader."""

        try:
            runtime.output.put_nowait(chunk)
        except queue.Full:
            try:
                runtime.output.get_nowait()
            except queue.Empty:
                pass
            try:
                runtime.output.put_nowait(chunk)
            except queue.Full:
                pass

    @staticmethod
    def _cwd(value: str | None) -> Path:
        if not value:
            return Path.cwd()
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"Diretório de trabalho não existe: {path}")
        return path

    def _command(
        self, data: TerminalCreate, profile: ConnectionProfileRead | None
    ) -> builtin_list[str]:
        if data.kind == TerminalKind.LOCAL:
            if os.name == "nt":
                return [shutil.which("pwsh") or "powershell.exe", "-NoLogo", "-NoProfile"]
            return [shutil.which("bash") or "/bin/sh", "-i"]
        assert profile is not None
        return ConnectionService(self.workspace).ssh_command(profile.id, pty=True)
