"""Explicitly-confirmed local process lifecycle for forward plans."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

from ctfws.core.errors import EntityNotFoundError
from ctfws.core.process import process_identity, process_matches
from ctfws.models.forward import ExecutionLocation, ForwardKind, ForwardRead, ForwardStatus
from ctfws.services.workspace import WorkspaceService


class ForwardProcessService:
    """Start/stop only a stored command after the CLI obtains confirmation."""

    def __init__(self, workspace: WorkspaceService, ssh_manager: Any | None = None) -> None:
        self.workspace = workspace
        self.ssh_manager = ssh_manager
        self._listeners: dict[int, Any] = {}
        self._operation_locks: dict[int, asyncio.Lock] = {}

    @property
    def async_available(self) -> bool:
        """Whether SSH forwards can be owned as listeners by the motor."""

        return self.ssh_manager is not None and bool(self.ssh_manager.available)

    async def start_async(self, forward_id: int) -> ForwardRead:
        """Serialize concurrent starts for one forward in this motor."""

        async with self._operation_lock(forward_id):
            return await self._start_async_unlocked(forward_id)

    async def _start_async_unlocked(self, forward_id: int) -> ForwardRead:
        """Start an SSH forward on the shared AsyncSSH connection when available."""

        forward = self.workspace.forwards.get(forward_id)
        if forward is None:
            raise EntityNotFoundError(f"Forward {forward_id} não encontrado.")
        self._ensure_local_execution(forward)
        if (
            not self.async_available
            or forward.tool.lower() != "ssh"
            or forward.connection_id is None
        ):
            return await asyncio.to_thread(self.start, forward_id)
        if forward.status == ForwardStatus.ACTIVE and forward_id in self._listeners:
            return forward
        manager = self.ssh_manager
        if manager is None:
            raise RuntimeError("O gerenciador SSH não está disponível.")
        self.workspace.forwards.set_process(
            forward_id,
            None,
            ForwardStatus.STARTING,
            health="starting",
            error=None,
            exit_code=None,
            engine_id=self.workspace.engine_id,
        )
        try:
            connection = await manager.connect(forward.connection_id)
            self._prepare_listener_port(forward)
            if forward.kind == ForwardKind.DYNAMIC:
                listener = await connection.forward_socks(forward.local_address, forward.local_port)
            elif forward.kind == ForwardKind.LOCAL:
                if forward.target_address is None or forward.target_port is None:
                    raise ValueError("Forward SSH exige um destino completo.")
                listener = await connection.forward_local_port(
                    forward.local_address,
                    forward.local_port,
                    forward.target_address,
                    forward.target_port,
                )
            elif forward.kind == ForwardKind.REMOTE:
                if forward.target_address is None or forward.target_port is None:
                    raise ValueError("Forward SSH exige um destino completo.")
                listener = await connection.forward_remote_port(
                    forward.local_address,
                    forward.local_port,
                    forward.target_address,
                    forward.target_port,
                )
            else:  # pragma: no cover - ForwardKind is exhaustive
                raise ValueError("Tipo de forward não suportado pelo AsyncSSH.")
        except Exception as error:
            self.workspace.forwards.set_process(
                forward_id,
                None,
                ForwardStatus.ERROR,
                health="listener_failed",
                error=str(error)[:1000],
                engine_id=self.workspace.engine_id,
            )
            raise
        self._listeners[forward_id] = listener
        listener_state = "remote_unprobed" if forward.kind == ForwardKind.REMOTE else "open"
        return self.workspace.forwards.update_health(
            forward_id,
            status=ForwardStatus.ACTIVE,
            health="listener_open;destination_unprobed",
            listener_state=listener_state,
            destination_state="not_probed",
            error=None,
        )

    async def check_async(self, forward_id: int) -> ForwardRead:
        """Refresh the state of a listener owned by the AsyncSSH motor."""

        forward = self.workspace.forwards.get(forward_id)
        if forward is None:
            raise EntityNotFoundError(f"Forward {forward_id} não encontrado.")
        if forward_id not in self._listeners:
            if (
                forward.pid is None
                and forward.connection_id is not None
                and forward.tool.lower() == "ssh"
                and forward.status
                in {ForwardStatus.STARTING, ForwardStatus.ACTIVE, ForwardStatus.DEGRADED}
            ):
                return self.workspace.forwards.update_health(
                    forward_id,
                    status=ForwardStatus.DEGRADED,
                    health="listener_not_owned_by_current_motor",
                    listener_state="unconfirmed",
                    destination_state="unknown",
                    error=(
                        "O listener não está associado ao motor atual; "
                        "retomada explícita necessária."
                    ),
                )
            return await asyncio.to_thread(self.check, forward_id)
        listener_state = "remote_unprobed" if forward.kind == ForwardKind.REMOTE else "open"
        return self.workspace.forwards.update_health(
            forward_id,
            status=ForwardStatus.ACTIVE,
            health="listener_open;destination_unprobed",
            listener_state=listener_state,
            destination_state="not_probed",
            error=None,
        )

    async def stop_async(self, forward_id: int) -> ForwardRead:
        """Serialize concurrent stops for one forward in this motor."""

        async with self._operation_lock(forward_id):
            return await self._stop_async_unlocked(forward_id)

    async def _stop_async_unlocked(self, forward_id: int) -> ForwardRead:
        """Close only the AsyncSSH listener owned by this forward."""

        listener = self._listeners.pop(forward_id, None)
        if listener is None:
            return await asyncio.to_thread(self.stop, forward_id)
        try:
            listener.close()
            wait_closed = getattr(listener, "wait_closed", None)
            if wait_closed is not None:
                result = wait_closed()
                if hasattr(result, "__await__"):
                    await result
        except Exception as error:
            return self.workspace.forwards.update_health(
                forward_id,
                status=ForwardStatus.ERROR,
                health="stop_unconfirmed",
                error=str(error)[:1000],
            )
        stopped = self.workspace.forwards.set_process(
            forward_id,
            None,
            ForwardStatus.STOPPED,
            health="stopped",
            error=None,
            listener_state="closed",
            clear_engine_id=True,
        )
        self.workspace.port_leases.release_for("forward", forward_id)
        return stopped

    async def close_all(self) -> None:
        """Close every AsyncSSH listener before the shared connections stop."""

        for forward_id in list(self._listeners):
            await self.stop_async(forward_id)

    def start(self, forward_id: int) -> ForwardRead:
        forward = self.workspace.forwards.get(forward_id)
        if forward is None:
            raise EntityNotFoundError(f"Forward {forward_id} não encontrado.")
        self._ensure_local_execution(forward)
        self._ensure_structured_command(forward)
        if forward.status == ForwardStatus.ACTIVE:
            return forward
        self.workspace.forwards.set_process(
            forward_id,
            None,
            ForwardStatus.STARTING,
            health="starting",
            error=None,
            exit_code=None,
            engine_id=self.workspace.engine_id,
        )
        try:
            self._prepare_listener_port(forward)
        except ValueError as error:
            return self.workspace.forwards.set_process(
                forward_id,
                None,
                ForwardStatus.ERROR,
                health="listener_reservation_failed",
                error=str(error),
                engine_id=self.workspace.engine_id,
            )
        log_path = self.workspace.paths.root / "logs" / f"forward-{forward_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                list(forward.command_argv),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            log_handle.close()
            self.workspace.forwards.set_process(
                forward_id,
                None,
                ForwardStatus.ERROR,
                health="spawn_failed",
                error="Não foi possível iniciar o processo do forward.",
                engine_id=self.workspace.engine_id,
            )
            raise
        log_handle.close()
        return_code = process.poll()
        if return_code is not None:
            return self.workspace.forwards.set_process(
                forward_id,
                None,
                ForwardStatus.ERROR,
                health="process_exited",
                error="O processo terminou imediatamente; consulte o log do forward.",
                exit_code=return_code,
                engine_id=self.workspace.engine_id,
            )
        identity = process_identity(process.pid)
        started = self.workspace.forwards.set_process(
            forward_id,
            process.pid,
            ForwardStatus.STARTING,
            health="process_running",
            error=None,
            exit_code=None,
            process_started_at=(
                datetime.fromtimestamp(identity.create_time, UTC).isoformat()
                if identity is not None and identity.create_time is not None
                else None
            ),
            process_executable=identity.executable if identity is not None else None,
            process_fingerprint=identity.command_fingerprint if identity is not None else None,
            engine_id=self.workspace.engine_id,
        )
        # A live PID is not enough to claim that a forward is usable.  Reuse
        # the same health probe exposed by `forward check` before presenting
        # the resource as active.
        del started
        return self.check(forward_id)

    def check(self, forward_id: int) -> ForwardRead:
        """Refresh process and, when possible, local listener health."""

        forward = self.workspace.forwards.get(forward_id)
        if forward is None:
            raise EntityNotFoundError(f"Forward {forward_id} não encontrado.")
        if forward.pid is None:
            return self.workspace.forwards.update_health(
                forward_id,
                status=forward.status,
                health="unmanaged",
                error="Nenhum PID associado a este forward.",
            )
        identity = process_identity(forward.pid)
        expected_started = self._expected_start(forward.process_started_at)
        if not process_matches(
            identity,
            create_time=expected_started,
            executable=forward.process_executable,
            command_fingerprint=forward.process_fingerprint,
        ):
            return self.workspace.forwards.update_health(
                forward_id,
                status=ForwardStatus.STOPPED,
                health="process_missing_or_changed",
                error=f"Processo {forward.pid} não está ativo ou mudou de identidade.",
            )

        health = "process_alive"
        listener_open = False
        destination_state: str | None = None
        if forward.kind in {ForwardKind.LOCAL, ForwardKind.DYNAMIC}:
            listener_open = self._listener_open(forward.local_address, forward.local_port)
            if listener_open:
                health = "process_alive;listener_open"
            else:
                health = "process_alive;listener_unconfirmed"
            destination_state = "not_probed" if forward.target_address else None
        elif forward.kind == ForwardKind.REMOTE:
            listener_open = True
            health = "process_alive;remote_listener_unprobed"
            destination_state = "not_probed" if forward.target_address else None
        status = ForwardStatus.ACTIVE
        if forward.kind in {ForwardKind.LOCAL, ForwardKind.DYNAMIC} and not listener_open:
            status = ForwardStatus.DEGRADED
        return self.workspace.forwards.update_health(
            forward_id,
            status=status,
            health=health,
            listener_state=(
                ("open" if listener_open else "unconfirmed")
                if forward.kind in {ForwardKind.LOCAL, ForwardKind.DYNAMIC}
                else "remote_unprobed"
            ),
            destination_state=destination_state,
            error=None,
        )

    def stop(self, forward_id: int) -> ForwardRead:
        forward = self.workspace.forwards.get(forward_id)
        if forward is None:
            raise EntityNotFoundError(f"Forward {forward_id} não encontrado.")
        if forward.pid is not None:
            identity = process_identity(forward.pid)
            expected_started = self._expected_start(forward.process_started_at)
            if not process_matches(
                identity,
                create_time=expected_started,
                executable=forward.process_executable,
                command_fingerprint=forward.process_fingerprint,
            ):
                return self.workspace.forwards.update_health(
                    forward_id,
                    status=ForwardStatus.ERROR,
                    health="ownership_lost",
                    error="O processo atual não corresponde à identidade registrada.",
                )
            try:
                os.kill(forward.pid, signal.SIGTERM)
            except ProcessLookupError:
                # The owned process is already gone; the postcondition below
                # still confirms that it cannot be observed anymore.
                pass
            except PermissionError as error:
                return self.workspace.forwards.update_health(
                    forward_id,
                    status=ForwardStatus.ERROR,
                    health="stop_permission_denied",
                    error=f"Permissão negada ao encerrar o processo: {error}",
                )
            except OSError as error:
                if os.name != "nt":
                    return self.workspace.forwards.update_health(
                        forward_id,
                        status=ForwardStatus.ERROR,
                        health="stop_failed",
                        error=f"Falha ao enviar sinal de encerramento: {error}",
                    )
                completed = subprocess.run(
                    ["taskkill", "/PID", str(forward.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
                if completed.returncode != 0:
                    return self.workspace.forwards.update_health(
                        forward_id,
                        status=ForwardStatus.ERROR,
                        health="stop_failed",
                        error="O Windows recusou o encerramento do processo.",
                    )
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and process_identity(forward.pid) is not None:
                time.sleep(0.05)
            if process_identity(forward.pid) is not None:
                return self.workspace.forwards.update_health(
                    forward_id,
                    status=ForwardStatus.ERROR,
                    health="stop_unconfirmed",
                    error="O processo não confirmou encerramento.",
                )
        stopped = self.workspace.forwards.set_process(
            forward_id,
            None,
            ForwardStatus.STOPPED,
            health="stopped",
            error=None,
            clear_engine_id=True,
        )
        if stopped.pid is not None or stopped.status != ForwardStatus.STOPPED:
            return self.workspace.forwards.update_health(
                forward_id,
                status=ForwardStatus.ERROR,
                health="stop_unconfirmed",
                error="O processo não confirmou encerramento.",
            )
        self.workspace.port_leases.release_for("forward", forward_id)
        return stopped

    @staticmethod
    def _expected_start(value: datetime | str | None) -> float | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value.timestamp()
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None

    def _ensure_local_execution(self, forward: ForwardRead) -> None:
        """Never run an agent or a context-bound plan in the motor process."""

        unsupported = forward.execution_location != ExecutionLocation.MOTOR
        ligolo_agent = forward.tool.lower() in {"ligolo", "ligolo-ng"}
        if not unsupported and not ligolo_agent:
            return
        reason = (
            "O plano precisa ser executado no local indicado e ainda não possui um executor "
            "remoto/contextual gerenciado pelo motor."
        )
        self.workspace.forwards.update_health(
            forward.id,
            status=ForwardStatus.ERROR,
            health="execution_location_unmanaged",
            listener_state="unconfirmed",
            destination_state="unknown",
            error=reason,
        )
        raise RuntimeError(reason)

    def _operation_lock(self, forward_id: int) -> asyncio.Lock:
        lock = self._operation_locks.get(forward_id)
        if lock is None:
            lock = asyncio.Lock()
            self._operation_locks[forward_id] = lock
        return lock

    def _ensure_structured_command(self, forward: ForwardRead) -> None:
        """Refuse legacy free-form commands until the operator reviews a new plan."""

        if forward.command_argv:
            return
        reason = (
            "Este forward foi criado antes do armazenamento de argumentos estruturados; "
            "revise e recrie o plano antes de iniciar o processo."
        )
        self.workspace.forwards.update_health(
            forward.id,
            status=ForwardStatus.ERROR,
            health="legacy_command_review_required",
            listener_state="unconfirmed",
            destination_state="unknown",
            error=reason,
        )
        raise RuntimeError(reason)

    def _prepare_listener_port(self, forward: ForwardRead) -> None:
        """Release the review lease and revalidate its exact endpoint at start."""

        if forward.kind not in {ForwardKind.LOCAL, ForwardKind.DYNAMIC}:
            return
        if forward.local_port <= 0:
            raise ValueError("O forward exige uma porta local válida antes de iniciar.")
        # Plans survive motor restarts, but their in-memory socket lease does
        # not. Closing our lease immediately before the real listener is
        # created avoids keeping a stale reservation after a failed start.
        self.workspace.port_leases.release_for("forward", forward.id)
        self.workspace.port_leases.ensure_available(forward.local_address, forward.local_port)

    @staticmethod
    def _listener_open(address: str, port: int) -> bool:
        try:
            with socket.create_connection((address, port), timeout=0.2):
                return True
        except OSError:
            return False
