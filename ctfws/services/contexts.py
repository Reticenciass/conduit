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
from ctfws.services.ports import PortLease
from ctfws.services.routed_helper import RoutedNamespaceHelper
from ctfws.services.workspace import WorkspaceService


class NetworkContextService:
    """Create explicit context plans and own only their local process."""

    def __init__(self, workspace: WorkspaceService, ssh_manager: Any | None = None) -> None:
        self.workspace = workspace
        self.ssh_manager = ssh_manager
        self._listeners: dict[int, Any] = {}
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

        listener = self._listeners.pop(context_id, None)
        if listener is None:
            return await asyncio.to_thread(self.stop, context_id)
        context = self._get(context_id)
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

    def capabilities(self) -> dict[str, object]:
        helper = self.routed_helper.capabilities()
        manifest = ligolo_manifest_status()
        ligolo_agent_available = shutil.which("ligolo-agent") is not None
        ligolo_proxy_available = shutil.which("ligolo-proxy") is not None
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
                ),
                "requires": ["ligolo-agent", "ligolo-proxy", "namespace-helper"],
                "contract": "ctfws-routed-context-v1",
                "note": "Rotas globais e DNS da Kali nunca são alterados.",
                "helper": helper,
                "ligolo_agent_available": ligolo_agent_available,
                "ligolo_proxy_available": ligolo_proxy_available,
                "manifest": manifest.as_dict(),
            },
            "proxychains": {
                "available": shutil.which("proxychains") is not None
                or shutil.which("proxychains4") is not None,
                "scope": "TCP de binários dinamicamente ligados",
            },
        }

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
            argv = ("ip", "netns", "exec", namespace, program, *arguments)
            return {
                "context_id": context.id,
                "context_name": context.name,
                "launcher": launcher,
                "argv": list(argv),
                "command": shlex.join(argv),
                "environment": {},
                "config_path": None,
                "endpoint": None,
                "limitations": [
                    "o namespace e suas rotas precisam ter sido preparados pelo helper tipado",
                    "a ferramenta é executada sem privilégios elevados após o preparo",
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

    def plan(self, data: NetworkContextCreate) -> NetworkContextRead:
        self._validate_networks(data.network_cidrs)
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
            routed = self.capabilities()["routed"]
            assert isinstance(routed, dict)
            return self.workspace.contexts.update_runtime(
                context_id,
                status=ContextStatus.ERROR,
                health="routed_disabled",
                capabilities=(),
                error=(
                    "Contexto roteado desabilitado: instale o helper tipado, ligolo-proxy e "
                    f"ative CTFWS_ENABLE_ROUTED_CONTEXTS (capabilities={routed})."
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
