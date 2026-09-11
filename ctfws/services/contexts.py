"""Safe planning and lifecycle for SOCKS and gated routed contexts."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import shlex
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ctfws.core.process import process_identity, process_matches
from ctfws.events import Event
from ctfws.models.context import (
    ContextStatus,
    ContextTransport,
    NetworkContextCreate,
    NetworkContextRead,
)
from ctfws.pivot.manifest import ligolo_manifest_status
from ctfws.services.connections import ConnectionService
from ctfws.services.ligolo_runtime import LigoloRuntime
from ctfws.services.ports import PortLease
from ctfws.services.routed_helper import (
    RoutedCommandSpec,
    RoutedNamespaceHelper,
    RoutedProxyStopSpec,
)
from ctfws.services.workspace import WorkspaceService


class NetworkContextService:
    """Create explicit context plans and own only their local process."""

    def __init__(self, workspace: WorkspaceService, ssh_manager: Any | None = None) -> None:
        self.workspace = workspace
        self.ssh_manager = ssh_manager
        self._listeners: dict[int, Any] = {}
        self._routed_sessions: dict[int, Any] = {}
        self._operation_locks: dict[int, asyncio.Lock] = {}
        self.routed_helper = RoutedNamespaceHelper(workspace)

    @property
    def async_available(self) -> bool:
        """Whether SOCKS contexts can use the shared AsyncSSH connection."""

        return self.ssh_manager is not None and bool(self.ssh_manager.available)

    async def start_async(self, context_id: int) -> NetworkContextRead:
        """Serialize concurrent starts for one context in this motor."""

        async with self._operation_lock(context_id):
            return await self._start_async_unlocked(context_id)

    async def _start_async_unlocked(self, context_id: int) -> NetworkContextRead:
        """Start a SOCKS listener owned by this motor when AsyncSSH is available."""

        context = self._get(context_id)
        if context.transport == ContextTransport.ROUTED:
            return await self._start_routed_async(context)
        if (
            not self.async_available
            or context.transport != ContextTransport.SOCKS
            or context.connection_id is None
        ):
            return await asyncio.to_thread(self.start, context_id)
        if context.status == ContextStatus.ACTIVE and context_id in self._listeners:
            return context
        manager = self.ssh_manager
        if manager is None:
            raise RuntimeError("O gerenciador SSH não está disponível.")
        self.workspace.contexts.update_runtime(
            context_id,
            status=ContextStatus.STARTING,
            error=None,
            health="starting",
            capabilities=(),
            engine_id=self.workspace.engine_id,
        )
        try:
            connection = await manager.connect(context.connection_id)
            if context.local_port is None:
                raise ValueError("O contexto SOCKS não possui porta local.")
            self.workspace.port_leases.release_for("context", context_id)
            self.workspace.port_leases.ensure_available(context.local_address, context.local_port)
            listener = await connection.forward_socks(context.local_address, context.local_port)
        except Exception as error:
            return self.workspace.contexts.update_runtime(
                context_id,
                status=ContextStatus.ERROR,
                error=str(error)[:1000],
                health="listener_failed",
                capabilities=(),
                engine_id=self.workspace.engine_id,
            )
        self._listeners[context_id] = listener
        result = self.workspace.contexts.update_runtime(
            context_id,
            status=ContextStatus.ACTIVE,
            pid=None,
            error=None,
            health="listener_open;scope_unprobed",
            capabilities=("socks", "tcp"),
            engine_id=self.workspace.engine_id,
        )
        self.workspace._emit(
            Event(
                event_type="NETWORK_CONTEXT_STARTED",
                message=f"Context {context.name} started",
                entity_type="network_context",
                entity_id=context.id,
                payload={"transport": "asyncssh", "listener": "open"},
            )
        )
        return result

    async def stop_async(self, context_id: int) -> NetworkContextRead:
        """Serialize concurrent stops for one context in this motor."""

        async with self._operation_lock(context_id):
            return await self._stop_async_unlocked(context_id)

    async def _stop_async_unlocked(self, context_id: int) -> NetworkContextRead:
        """Stop only the listener owned by the selected SOCKS context."""

        context = self._get(context_id)
        if context.transport == ContextTransport.ROUTED:
            session = self._routed_sessions.pop(context_id, None)
            if session is None:
                return await asyncio.to_thread(self.stop, context_id)
            try:
                await LigoloRuntime(self.workspace, self.ssh_manager, self.routed_helper).stop(
                    session
                )
            except Exception as error:
                return self.workspace.contexts.update_runtime(
                    context_id,
                    status=ContextStatus.ERROR,
                    health="routed_stop_unconfirmed",
                    error=str(error)[:1000],
                )
            stopped = self.workspace.contexts.update_runtime(
                context_id,
                status=ContextStatus.STOPPED,
                pid=None,
                error=None,
                health="stopped;runtime_cleaned",
                capabilities=(),
                clear_engine_id=True,
                clear_process_identity=True,
                clear_namespace=True,
                resource_manifest=(),
            )
            self.workspace._emit(
                Event(
                    event_type="NETWORK_CONTEXT_STOPPED",
                    message=f"Context {context.name} stopped",
                    entity_type="network_context",
                    entity_id=context.id,
                )
            )
            return stopped
        listener = self._listeners.pop(context_id, None)
        if listener is None:
            return await asyncio.to_thread(self.stop, context_id)
        try:
            listener.close()
            wait_closed = getattr(listener, "wait_closed", None)
            if wait_closed is not None:
                result = wait_closed()
                if hasattr(result, "__await__"):
                    await result
        except Exception as error:
            return self.workspace.contexts.update_runtime(
                context_id,
                status=ContextStatus.ERROR,
                error=str(error)[:1000],
                health="stop_unconfirmed",
            )
        stopped = self.workspace.contexts.update_runtime(
            context_id,
            status=ContextStatus.STOPPED,
            pid=None,
            error=None,
            health="stopped",
            capabilities=(),
            clear_engine_id=True,
            clear_process_identity=True,
        )
        self.workspace.port_leases.release_for("context", context_id)
        self.workspace._emit(
            Event(
                event_type="NETWORK_CONTEXT_STOPPED",
                message=f"Context {context.name} stopped",
                entity_type="network_context",
                entity_id=context.id,
            )
        )
        return stopped

    async def close_all(self) -> None:
        """Close all shared SOCKS listeners before SSH connections."""

        for context_id in list(self._listeners):
            await self.stop_async(context_id)
        for context_id in list(self._routed_sessions):
            await self.stop_async(context_id)

    def capabilities(self) -> dict[str, object]:
        helper = self.routed_helper.capabilities()
        manifest = ligolo_manifest_status()
        ligolo_agent_path = manifest.agent_path or shutil.which("ligolo-agent")
        ligolo_proxy_path = manifest.proxy_path or shutil.which("ligolo-proxy")
        ligolo_agent_available = ligolo_agent_path is not None and Path(ligolo_agent_path).is_file()
        ligolo_proxy_available = ligolo_proxy_path is not None and Path(ligolo_proxy_path).is_file()
        runtime = helper.get("runtime")
        runtime_available = isinstance(runtime, dict) and bool(runtime.get("available"))
        proxy_capable = isinstance(runtime, dict) and bool(runtime.get("proxy_net_admin"))
        routed_runtime_supported = runtime_available and proxy_capable
        routed_reason = "runtime isolado pronto"
        if os.getenv("CTFWS_ENABLE_ROUTED_CONTEXTS") != "1":
            routed_runtime_supported = False
            routed_reason = "habilite CTFWS_ENABLE_ROUTED_CONTEXTS=1 após revisar o helper"
        elif not runtime_available:
            routed_reason = "helper privilegiado indisponível; habilite ctfws-namespace-helper"
        elif not proxy_capable:
            routed_reason = "ligolo-proxy não possui CAP_NET_ADMIN no runtime gerenciado"
        elif not manifest.valid or not manifest.binary_verified:
            routed_reason = manifest.reason
        return {
            "socks": True,
            "routed": {
                "enabled": (
                    os.name != "nt"
                    and os.getenv("CTFWS_ENABLE_ROUTED_CONTEXTS") == "1"
                    and bool(helper["available"])
                    and bool(helper["privileged"])
                    and ligolo_agent_available
                    and ligolo_proxy_available
                    and manifest.valid
                    and manifest.binary_verified
                    and routed_runtime_supported
                ),
                "requires": [
                    "pinned Ligolo-ng 0.9.1",
                    "checksums verificados",
                    "ligolo-agent",
                    "ligolo-proxy",
                    "namespace-helper",
                ],
                "contract": "ctfws-routed-context-v1",
                "runtime_supported": routed_runtime_supported,
                "reason": routed_reason,
                "note": "Rotas globais e DNS da Kali nunca são alterados.",
                "helper": helper,
                "ligolo_agent_available": ligolo_agent_available,
                "ligolo_proxy_available": ligolo_proxy_available,
                "ligolo_agent_path": ligolo_agent_path,
                "ligolo_proxy_path": ligolo_proxy_path,
                "manifest": manifest.as_dict(),
            },
            "proxychains": {
                "available": shutil.which("proxychains") is not None
                or shutil.which("proxychains4") is not None,
                "scope": "TCP de binários dinamicamente ligados",
            },
        }

    async def _start_routed_async(self, context: NetworkContextRead) -> NetworkContextRead:
        """Start the real isolated Ligolo agent/proxy workflow."""

        if context.status == ContextStatus.ACTIVE and context.id in self._routed_sessions:
            return context
        routed = self.capabilities()["routed"]
        if not isinstance(routed, dict) or routed.get("enabled") is not True:
            reason = (
                routed.get("reason") if isinstance(routed, dict) else "diagnóstico indisponível"
            )
            return self.workspace.contexts.update_runtime(
                context.id,
                status=ContextStatus.ERROR,
                health="routed_unavailable",
                capabilities=(),
                error=f"Contexto roteado indisponível: {reason}",
            )
        self.workspace.contexts.update_runtime(
            context.id,
            status=ContextStatus.STARTING,
            error=None,
            health="preparing_runtime",
            capabilities=(),
            engine_id=self.workspace.engine_id,
        )
        runtime = LigoloRuntime(self.workspace, self.ssh_manager, self.routed_helper)
        try:
            session = await runtime.start(context)
        except Exception as error:
            return self.workspace.contexts.update_runtime(
                context.id,
                status=ContextStatus.ERROR,
                health="routed_start_failed",
                capabilities=(),
                error=str(error)[:1000],
                engine_id=self.workspace.engine_id,
            )
        self._routed_sessions[context.id] = session
        identity = process_identity(session.proxy.pid)
        return self.workspace.contexts.update_runtime(
            context.id,
            status=ContextStatus.ACTIVE,
            pid=session.proxy.pid,
            process_started_at=(
                datetime.fromtimestamp(identity.create_time, UTC).isoformat()
                if identity is not None and identity.create_time is not None
                else None
            ),
            process_executable=session.proxy.executable,
            process_fingerprint=session.proxy.command_fingerprint,
            namespace_name=session.spec.namespace,
            resource_manifest=self.routed_helper.runtime_resources(context.id),
            health="proxy_ready;agent_connected;tun_ready;routes_installed",
            capabilities=("routed", "ligolo", "namespace", "routes", "dns-context"),
            error=None,
            engine_id=self.workspace.engine_id,
        )

    def launcher_plan(
        self,
        context_id: int,
        program: str,
        arguments: tuple[str, ...] = (),
        *,
        launcher: str = "environment",
    ) -> dict[str, object]:
        """Build a copyable SOCKS launcher without executing the selected program."""

        context = self._get(context_id)
        self._validate_launcher_token(program, "programa")
        for argument in arguments:
            self._validate_launcher_token(argument, "argumento")
        if context.transport == ContextTransport.ROUTED:
            if launcher != "namespace":
                raise ValueError("Um contexto roteado exige o launcher namespace.")
            namespace = self.routed_helper.namespace_for(context.id)
            if context.namespace_name != namespace:
                raise ValueError("O namespace do contexto ainda não foi preparado pelo helper.")
            if context.status not in {ContextStatus.ACTIVE, ContextStatus.DEGRADED}:
                raise ValueError("Prepare e inicie o contexto roteado antes de gerar um launcher.")
            self.routed_helper._validate_command_spec(  # noqa: SLF001 - shared safety boundary
                RoutedCommandSpec(
                    self.workspace.lab.id,
                    context.id,
                    namespace,
                    program,
                    arguments,
                )
            )
            argv = (program, *arguments)
            return {
                "context_id": context.id,
                "context_name": context.name,
                "launcher": launcher,
                "argv": list(argv),
                "command": shlex.join(argv),
                "environment": {},
                "config_path": None,
                "endpoint": None,
                "execution": "context-worker",
                "limitations": [
                    "somente ferramentas de rede suportadas pelo worker contextual",
                    "a ferramenta é executada como ctfws, sem privilégios elevados",
                ],
            }
        if context.transport != ContextTransport.SOCKS:
            raise ValueError("Tipo de contexto não suporta launcher.")
        if context.status != ContextStatus.ACTIVE or context.local_port is None:
            raise ValueError("Inicie o contexto SOCKS antes de gerar um launcher.")
        if launcher not in {"environment", "proxychains"}:
            raise ValueError("Launcher inválido; escolha environment ou proxychains.")

        endpoint = f"socks5h://{context.local_address}:{context.local_port}"
        limitations = [
            "somente programas que respeitam proxy SOCKS ou variáveis de ambiente",
            "não encaminha automaticamente UDP, ICMP ou binários estaticamente ligados",
        ]
        config_path: Path | None = None
        if launcher == "environment":
            argv = (program, *arguments)
            environment = {
                "ALL_PROXY": endpoint,
                "HTTP_PROXY": endpoint,
                "HTTPS_PROXY": endpoint,
                "NO_PROXY": "",
            }
        else:
            executable = shutil.which("proxychains4") or shutil.which("proxychains")
            if executable is None:
                raise RuntimeError("proxychains-ng não está instalado no motor.")
            config_path = self._write_proxychains_config(context)
            argv = (executable, "-q", "-f", str(config_path), program, *arguments)
            environment = {}
            limitations.append(
                "proxychains-ng cobre TCP de binários dinamicamente ligados; não é isolamento total"
            )
        return {
            "context_id": context.id,
            "context_name": context.name,
            "launcher": launcher,
            "argv": list(argv),
            "command": shlex.join(argv),
            "environment": environment,
            "config_path": str(config_path) if config_path is not None else None,
            "endpoint": endpoint,
            "limitations": limitations,
        }

    def execute_launcher(
        self,
        context_id: int,
        program: str,
        arguments: tuple[str, ...] = (),
        *,
        launcher: str = "environment",
        timeout_seconds: int = 60,
    ) -> dict[str, object]:
        """Execute one explicitly selected program without invoking a shell.

        The launcher preview and execution share the exact same argv. Output is
        written to a context-scoped runtime file first, then capped before it
        is returned to the task API. This keeps the button-driven workflow
        useful without turning the motor into a free-form shell endpoint.
        """

        if not 1 <= timeout_seconds <= 3600:
            raise ValueError("O tempo limite precisa estar entre 1 e 3600 segundos.")
        plan = self.launcher_plan(context_id, program, arguments, launcher=launcher)
        context = self._get(context_id)
        if context.transport == ContextTransport.ROUTED:
            result = self.routed_helper.execute_command(
                RoutedCommandSpec(
                    self.workspace.lab.id,
                    context.id,
                    self.routed_helper.namespace_for(context.id),
                    program,
                    arguments,
                    timeout_seconds,
                )
            )
            return {
                "context_id": context_id,
                "launcher": launcher,
                "argv": list(arguments),
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "output": result.output,
                "output_truncated": result.output_truncated,
                "failed_steps": (
                    ["execution"] if result.timed_out or result.returncode not in {0, None} else []
                ),
                "limitations": plan.get("limitations", []),
            }
        raw_argv = plan.get("argv")
        raw_environment = plan.get("environment")
        if not isinstance(raw_argv, list) or not all(isinstance(item, str) for item in raw_argv):
            raise ValueError("O launcher não produziu argumentos executáveis válidos.")
        environment = os.environ.copy()
        if isinstance(raw_environment, dict):
            environment.update({str(key): str(value) for key, value in raw_environment.items()})

        run_dir = self.workspace.paths.root / "runtime" / "contexts" / str(context_id) / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        output_path = run_dir / f"run-{uuid.uuid4().hex}.log"
        process: subprocess.Popen[bytes] | None = None
        timed_out = False
        try:
            with output_path.open("wb") as output:
                process = subprocess.Popen(
                    raw_argv,
                    cwd=str(self.workspace.paths.root),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                )
                deadline = time.monotonic() + timeout_seconds
                while process.poll() is None:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        if os.name != "nt":
                            kill_group = getattr(os, "killpg", None)
                            if callable(kill_group):
                                kill_group(process.pid, 15)
                            else:
                                process.terminate()
                        else:
                            process.terminate()
                        break
                    time.sleep(0.05)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        kill_group = getattr(os, "killpg", None)
                        if callable(kill_group):
                            kill_group(process.pid, 9)
                        else:
                            process.kill()
                    else:
                        process.kill()
                    process.wait(timeout=2)
        finally:
            try:
                size = output_path.stat().st_size
                with output_path.open("rb") as output:
                    data = output.read(1024 * 1024 + 1)
                truncated = size > 1024 * 1024
                text = data[: 1024 * 1024].decode("utf-8", errors="replace")
            finally:
                output_path.unlink(missing_ok=True)

        return {
            "context_id": context_id,
            "launcher": launcher,
            "argv": raw_argv,
            "returncode": process.returncode if process is not None else None,
            "timed_out": timed_out,
            "output": text,
            "output_truncated": truncated,
            "failed_steps": (
                ["execution"] if timed_out or process is None or process.returncode != 0 else []
            ),
            "limitations": plan.get("limitations", []),
        }

    def plan(self, data: NetworkContextCreate) -> NetworkContextRead:
        self._validate_networks(data.network_cidrs)
        if data.transport == ContextTransport.ROUTED and not data.network_cidrs:
            raise ValueError("Um contexto roteado exige ao menos uma rede selecionada.")
        lease: PortLease | None = None
        if data.transport == ContextTransport.SOCKS:
            if data.connection_id is None:
                raise ValueError("Um contexto SOCKS exige uma conexão SSH.")
            profile = ConnectionService(self.workspace).get(data.connection_id)
            try:
                lease = self.workspace.port_leases.reserve(data.local_address, data.local_port)
                port = lease.port
                command = self._socks_command(profile.id, data.local_address, port)
                data = data.model_copy(update={"local_port": port})
            except Exception:
                if lease is not None:
                    self.workspace.port_leases.release(lease.token)
                raise
        else:
            command = ("routed-context", *data.network_cidrs)
        try:
            context = self.workspace.contexts.create(data, command)
        except Exception:
            if lease is not None:
                self.workspace.port_leases.release(lease.token)
            raise
        if lease is not None:
            self.workspace.port_leases.attach("context", context.id, lease)
        self.workspace._emit(
            Event(
                event_type="NETWORK_CONTEXT_PLANNED",
                message=f"Context {context.name} planned",
                entity_type="network_context",
                entity_id=context.id,
                payload={"transport": context.transport.value, "networks": data.network_cidrs},
            )
        )
        return context

    def start(self, context_id: int) -> NetworkContextRead:
        context = self._get(context_id)
        if context.status == ContextStatus.ACTIVE:
            return context
        if context.transport == ContextTransport.ROUTED:
            if self.async_available:
                try:
                    return asyncio.run(self.start_async(context_id))
                except RuntimeError as error:
                    if "cannot be called from a running event loop" not in str(error):
                        return self.workspace.contexts.update_runtime(
                            context_id,
                            status=ContextStatus.ERROR,
                            health="routed_start_failed",
                            capabilities=(),
                            error=str(error)[:1000],
                        )
            routed = self.capabilities()["routed"]
            assert isinstance(routed, dict)
            return self.workspace.contexts.update_runtime(
                context_id,
                status=ContextStatus.ERROR,
                health="routed_unavailable",
                capabilities=(),
                error=(
                    "Contexto roteado indisponível: "
                    f"{routed.get('reason', 'consulte o diagnóstico do adaptador')}"
                ),
            )

        self.workspace.contexts.update_runtime(
            context_id,
            status=ContextStatus.STARTING,
            error=None,
            health="starting",
            capabilities=(),
            engine_id=self.workspace.engine_id,
        )
        log_path = self.workspace.paths.root / "logs" / f"context-{context_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if context.local_port is None:
                raise ValueError("O contexto SOCKS não possui porta local.")
            self.workspace.port_leases.release_for("context", context_id)
            self.workspace.port_leases.ensure_available(context.local_address, context.local_port)
            with log_path.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    list(context.command),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=os.name != "nt",
                )
        except (OSError, ValueError) as error:
            return self.workspace.contexts.update_runtime(
                context_id,
                status=ContextStatus.ERROR,
                error=str(error),
                health="spawn_failed",
                capabilities=(),
                engine_id=self.workspace.engine_id,
            )
        if process.poll() is not None:
            return self.workspace.contexts.update_runtime(
                context_id,
                status=ContextStatus.ERROR,
                health="process_exited",
                capabilities=(),
                error="O processo do contexto terminou imediatamente; consulte o log.",
                engine_id=self.workspace.engine_id,
            )
        identity = process_identity(process.pid)
        result = self.workspace.contexts.update_runtime(
            context_id,
            status=ContextStatus.ACTIVE,
            pid=process.pid,
            error=None,
            health="process_alive;scope_unprobed",
            capabilities=("socks", "tcp"),
            process_started_at=(
                datetime.fromtimestamp(identity.create_time, UTC).isoformat()
                if identity is not None and identity.create_time is not None
                else None
            ),
            process_executable=identity.executable if identity is not None else None,
            process_fingerprint=identity.command_fingerprint if identity is not None else None,
            engine_id=self.workspace.engine_id,
        )
        self.workspace._emit(
            Event(
                event_type="NETWORK_CONTEXT_STARTED",
                message=f"Context {context.name} started",
                entity_type="network_context",
                entity_id=context.id,
                payload={
                    "pid": process.pid,
                    "executable": identity.executable if identity else None,
                },
            )
        )
        return result

    def namespace_plan(
        self, context_id: int, *, device: str | None = None
    ) -> list[dict[str, object]]:
        """Return a reviewable typed namespace plan without changing the host network."""

        context = self._get(context_id)
        if context.transport != ContextTransport.ROUTED:
            raise ValueError("Somente contextos roteados possuem plano de namespace.")
        return [
            {
                "workspace_id": operation.workspace_id,
                "context_id": operation.context_id,
                "action": operation.action,
                "namespace": operation.namespace,
                "cidr": operation.cidr,
                "device": operation.device,
            }
            for operation in self.routed_helper.plan(
                context.id, context.network_cidrs, device=device
            )
        ]

    def namespace_cleanup_plan(
        self, context_id: int, *, device: str | None = None
    ) -> list[dict[str, object]]:
        """Return the exact reverse plan used to remove context-owned routes."""

        context = self._get(context_id)
        if context.transport != ContextTransport.ROUTED:
            raise ValueError("Somente contextos roteados possuem plano de limpeza.")
        return [
            {
                "workspace_id": operation.workspace_id,
                "context_id": operation.context_id,
                "action": operation.action,
                "namespace": operation.namespace,
                "cidr": operation.cidr,
                "device": operation.device,
            }
            for operation in self.routed_helper.cleanup_plan(
                context.id, context.network_cidrs, device=device
            )
        ]

    def prepare_namespace(
        self, context_id: int, *, device: str | None = None
    ) -> NetworkContextRead:
        """Apply the reviewed namespace plan through the restricted helper only."""

        context = self._get(context_id)
        if context.transport != ContextTransport.ROUTED:
            raise ValueError("Somente contextos roteados possuem namespace.")
        if context.resource_manifest:
            raise ValueError("O namespace já foi preparado; remova-o antes de preparar outro.")
        operations = self.routed_helper.plan(context.id, context.network_cidrs, device=device)
        result = self.routed_helper.apply(operations)
        updated = self.workspace.contexts.update_runtime(
            context.id,
            status=ContextStatus.PLANNED,
            namespace_name=result.namespace,
            resource_manifest=result.resources,
            health="namespace_prepared;transport_unstarted",
            capabilities=("namespace", "routes"),
            error=None,
            engine_id=self.workspace.engine_id,
        )
        self.workspace._emit(
            Event(
                event_type="NETWORK_NAMESPACE_PREPARED",
                message=f"Namespace for {context.name} prepared",
                entity_type="network_context",
                entity_id=context.id,
                payload={"namespace": result.namespace, "resources": result.resources},
            )
        )
        return updated

    def remove_namespace(self, context_id: int) -> NetworkContextRead:
        """Remove exactly the resources recorded by a previous helper response."""

        context = self._get(context_id)
        if context.transport != ContextTransport.ROUTED:
            raise ValueError("Somente contextos roteados possuem namespace.")
        if not context.resource_manifest:
            raise ValueError("Este contexto não possui recursos de namespace registrados.")
        operations = self.routed_helper.cleanup_from_manifest(context.id, context.resource_manifest)
        self.routed_helper.remove(operations)
        updated = self.workspace.contexts.update_runtime(
            context.id,
            status=ContextStatus.PLANNED,
            clear_namespace=True,
            resource_manifest=(),
            health="namespace_removed;transport_unstarted",
            capabilities=(),
            error=None,
            clear_engine_id=True,
        )
        self.workspace._emit(
            Event(
                event_type="NETWORK_NAMESPACE_REMOVED",
                message=f"Namespace for {context.name} removed",
                entity_type="network_context",
                entity_id=context.id,
            )
        )
        return updated

    def stop(self, context_id: int) -> NetworkContextRead:
        context = self._get(context_id)
        if context.transport == ContextTransport.ROUTED:
            if context.pid is not None:
                manifest = ligolo_manifest_status()
                try:
                    self.routed_helper.stop_proxy(
                        RoutedProxyStopSpec(
                            self.workspace.lab.id,
                            context.id,
                            context.namespace_name or self.routed_helper.namespace_for(context.id),
                            context.pid,
                            (
                                context.process_started_at.timestamp()
                                if context.process_started_at is not None
                                else None
                            ),
                            context.process_executable or manifest.proxy_path,
                            context.process_fingerprint,
                        )
                    )
                except Exception as error:
                    return self.workspace.contexts.update_runtime(
                        context_id,
                        status=ContextStatus.ERROR,
                        health="routed_stop_unconfirmed",
                        error=str(error)[:1000],
                    )
            if context.resource_manifest:
                try:
                    self.routed_helper.remove_runtime(context.id, context.resource_manifest)
                except Exception as error:
                    return self.workspace.contexts.update_runtime(
                        context_id,
                        status=ContextStatus.ERROR,
                        health="routed_cleanup_pending",
                        error=str(error)[:1000],
                    )
            return self.workspace.contexts.update_runtime(
                context_id,
                status=ContextStatus.STOPPED,
                pid=None,
                error=None,
                health="stopped;runtime_cleaned",
                capabilities=(),
                clear_engine_id=True,
                clear_process_identity=True,
                clear_namespace=True,
                resource_manifest=(),
            )
        if context.pid is not None:
            identity = process_identity(context.pid)
            expected_start = (
                context.process_started_at.timestamp()
                if context.process_started_at is not None
                else None
            )
            if not process_matches(
                identity,
                create_time=expected_start,
                executable=context.process_executable,
                command_fingerprint=context.process_fingerprint,
            ):
                return self.workspace.contexts.update_runtime(
                    context_id,
                    status=ContextStatus.ERROR,
                    pid=None,
                    health="ownership_lost",
                    capabilities=(),
                    error="O PID do contexto não está mais ativo; ownership não confirmado.",
                )
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(context.pid), "/T", "/F"],
                        check=False,
                        capture_output=True,
                    )
                else:
                    os.kill(context.pid, 15)
            except OSError as error:
                return self.workspace.contexts.update_runtime(
                    context_id,
                    status=ContextStatus.ERROR,
                    health="stop_failed",
                    error=str(error),
                )
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and process_identity(context.pid) is not None:
                time.sleep(0.05)
            if process_identity(context.pid) is not None:
                return self.workspace.contexts.update_runtime(
                    context_id,
                    status=ContextStatus.ERROR,
                    health="stop_unconfirmed",
                    error="O processo do contexto não confirmou encerramento.",
                )
        result = self.workspace.contexts.update_runtime(
            context_id,
            status=ContextStatus.STOPPED,
            pid=None,
            error=None,
            health="stopped",
            capabilities=(),
            clear_engine_id=True,
            clear_process_identity=True,
        )
        self.workspace.port_leases.release_for("context", context_id)
        self.workspace._emit(
            Event(
                event_type="NETWORK_CONTEXT_STOPPED",
                message=f"Context {context.name} stopped",
                entity_type="network_context",
                entity_id=context.id,
            )
        )
        return result

    def _get(self, context_id: int) -> NetworkContextRead:
        context = self.workspace.contexts.get(context_id)
        if context is None:
            raise ValueError(f"Contexto {context_id} não encontrado neste laboratório.")
        return context

    def _operation_lock(self, context_id: int) -> asyncio.Lock:
        lock = self._operation_locks.get(context_id)
        if lock is None:
            lock = asyncio.Lock()
            self._operation_locks[context_id] = lock
        return lock

    @staticmethod
    def _validate_launcher_token(value: str, label: str) -> None:
        if not value or len(value) > 4096 or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"O {label} é obrigatório e não pode conter quebras de linha.")

    def _write_proxychains_config(self, context: NetworkContextRead) -> Path:
        """Write a context-scoped, secret-free proxychains configuration atomically."""

        if context.local_port is None:
            raise ValueError("O contexto SOCKS não possui porta local.")
        directory = self.workspace.paths.root / "runtime" / "contexts" / str(context.id)
        directory.mkdir(parents=True, exist_ok=True)
        config = directory / "proxychains.conf"
        temporary = directory / f".proxychains-{uuid.uuid4().hex}.part"
        content = (
            "strict_chain\n"
            "proxy_dns\n"
            "[ProxyList]\n"
            f"socks5 {context.local_address} {context.local_port}\n"
        )
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, config)
        return config

    @staticmethod
    def _validate_networks(networks: tuple[str, ...]) -> None:
        for value in networks:
            network = ipaddress.ip_network(value, strict=False)
            if network.prefixlen == 0:
                raise ValueError("Um contexto não pode substituir a rota padrão.")

    def _socks_command(self, profile_id: int, address: str, port: int) -> tuple[str, ...]:
        base = ConnectionService(self.workspace).ssh_command(profile_id, batch_mode=True)
        target = base.pop()
        return tuple(
            [
                *base,
                "-N",
                "-o",
                "ExitOnForwardFailure=yes",
                "-D",
                f"{address}:{port}",
                target,
            ]
        )
