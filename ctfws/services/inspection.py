"""Fixed read-only inspection over a saved SSH profile."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import shlex
import socket
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ctfws.database.repositories import ObservationRepository
from ctfws.models.collection import CollectionStatus
from ctfws.models.connection import ConnectionProfileRead
from ctfws.models.host import HostCreate, HostRead
from ctfws.models.observation import ObservationSourceKind
from ctfws.models.snapshot import SnapshotCreate
from ctfws.services.connections import ConnectionService
from ctfws.services.enumeration import EnumerationService
from ctfws.services.workspace import WorkspaceService


@dataclass(frozen=True, slots=True)
class InspectionResult:
    """Counts and host identity produced by one inspection pass."""

    host_id: int
    host_name: str
    networks: int
    services: int
    connections: int
    completed_steps: int = 0
    failed_steps: tuple[str, ...] = ()
    snapshot_id: int | None = None
    collection_id: int | None = None


class RemoteInspectionService:
    """Run a fixed allowlist of read-only commands through OpenSSH."""

    COMMANDS = {
        "ip_addr": ("ip", "addr"),
        "ip_route": ("ip", "route"),
        "ip_neigh": ("ip", "neigh"),
        "ss": ("ss", "-tunap"),
        "hosts": ("cat", "/etc/hosts"),
        "resolv": ("cat", "/etc/resolv.conf"),
    }

    def __init__(self, workspace: WorkspaceService, ssh_manager: Any | None = None) -> None:
        self.workspace = workspace
        self.ssh_manager = ssh_manager

    @property
    def async_available(self) -> bool:
        """Whether the fixed collector can use the shared AsyncSSH manager."""

        return self.ssh_manager is not None and bool(self.ssh_manager.available)

    def inspect(
        self,
        connection_id: int,
        progress: Callable[[int, str], None] | None = None,
    ) -> InspectionResult:
        profile, host, snapshot, collection = self._prepare(connection_id)
        outputs: dict[str, str] = {}
        failed_steps: list[str] = []
        step_details: dict[str, dict[str, object]] = {}
        for completed_steps, (name, args) in enumerate(self.COMMANDS.items(), start=1):
            try:
                outputs[name] = self._run(profile, args)
                step_details[name] = {
                    "status": "succeeded",
                    "command": shlex.join(args),
                    "bytes": len(outputs[name].encode("utf-8")),
                }
            except Exception as error:
                failed_steps.append(name)
                step_details[name] = {
                    "status": "failed",
                    "command": shlex.join(args),
                    "error": str(error)[:500],
                }
            self.workspace.collections.update(
                collection.id,
                status=CollectionStatus.RUNNING,
                completed_steps=completed_steps,
                result={"steps": step_details, "outputs": outputs},
            )
            if progress is not None:
                progress(completed_steps, name)
        return self._finalize(
            profile, host, snapshot, collection, outputs, failed_steps, step_details
        )

    async def inspect_async(
        self,
        connection_id: int,
        progress: Callable[[int, str], None] | None = None,
    ) -> InspectionResult:
        """Inspect through the workspace's reusable AsyncSSH connection."""

        if self.ssh_manager is None or not self.ssh_manager.available:
            return await asyncio.to_thread(self.inspect, connection_id, progress)
        manager = self.ssh_manager
        profile, host, snapshot, collection = self._prepare(connection_id)
        outputs: dict[str, str] = {}
        failed_steps: list[str] = []
        step_details: dict[str, dict[str, object]] = {}
        for completed_steps, (name, args) in enumerate(self.COMMANDS.items(), start=1):
            try:
                outputs[name] = await self._run_async(manager, profile, args)
                step_details[name] = {
                    "status": "succeeded",
                    "command": shlex.join(args),
                    "bytes": len(outputs[name].encode("utf-8")),
                }
            except Exception as error:
                failed_steps.append(name)
                step_details[name] = {
                    "status": "failed",
                    "command": shlex.join(args),
                    "error": str(error)[:500],
                }
            self.workspace.collections.update(
                collection.id,
                status=CollectionStatus.RUNNING,
                completed_steps=completed_steps,
                result={"steps": step_details, "outputs": outputs},
            )
            if progress is not None:
                progress(completed_steps, name)
        return self._finalize(
            profile, host, snapshot, collection, outputs, failed_steps, step_details
        )

    def _prepare(self, connection_id: int) -> tuple[ConnectionProfileRead, HostRead, Any, Any]:
        profile = ConnectionService(self.workspace).get(connection_id)
        host = self._ensure_host(profile)
        if profile.host_id != host.id:
            self.workspace.connections.bind_host(profile.id, host.id)
        snapshot = self.workspace.snapshots.create(
            SnapshotCreate(
                name=f"inspection-{uuid.uuid4().hex[:12]}",
                purpose=f"Coleta remota de {profile.name}",
                metadata={"connection_id": profile.id, "host_id": host.id},
            )
        )
        collection = self.workspace.collections.create(
            host.id, len(self.COMMANDS), snapshot_id=snapshot.id
        )
        return profile, host, snapshot, collection

    def _finalize(
        self,
        profile: ConnectionProfileRead,
        host: HostRead,
        snapshot: Any,
        collection: Any,
        outputs: dict[str, str],
        failed_steps: list[str],
        step_details: dict[str, dict[str, object]],
    ) -> InspectionResult:
        if not outputs:
            detail = ", ".join(failed_steps) or "nenhum comando executado"
            self.workspace.collections.update(
                collection.id,
                status=CollectionStatus.FAILED,
                completed_steps=len(self.COMMANDS),
                result={"steps": step_details, "failed_steps": failed_steps},
            )
            raise RuntimeError(f"Inspeção SSH não produziu dados: {detail}")
        enum = EnumerationService(self.workspace)
        identifier = host.name or str(host.id)
        sockets = None
        importers = (
            ("ip_addr", enum.import_ip_addr),
            ("ip_route", enum.import_route),
            ("ip_neigh", enum.import_neigh),
            ("ss", enum.import_ss),
            ("hosts", enum.import_hosts),
            ("resolv", enum.import_resolv),
        )
        for name, importer in importers:
            if name not in outputs:
                continue
            try:
                result = importer(
                    identifier,
                    outputs[name],
                    source_kind=ObservationSourceKind.API,
                    snapshot_id=snapshot.id,
                )
            except Exception:
                if name not in failed_steps:
                    failed_steps.append(name)
                details = step_details.get(name, {})
                step_details[name] = {
                    **details,
                    "status": "failed",
                    "error": "A saída foi coletada, mas não pôde ser normalizada.",
                }
                continue
            if name == "ss":
                sockets = result
        if sockets is None:
            services = connections = 0
        else:
            services = sockets.details.get("services", 0)
            connections = sockets.details.get("connections", 0)
        result_status = CollectionStatus.PARTIAL if failed_steps else CollectionStatus.SUCCEEDED
        self.workspace.collections.update(
            collection.id,
            status=result_status,
            completed_steps=len(self.COMMANDS),
            result={
                "steps": step_details,
                "failed_steps": failed_steps,
                "snapshot_id": snapshot.id,
                "collection_id": collection.id,
                "outputs": outputs,
                "networks": len(self._networks_for_host(host.id)),
                "services": services,
                "connections": connections,
            },
        )
        return InspectionResult(
            host.id,
            identifier,
            len(self._networks_for_host(host.id)),
            services,
            connections,
            len(outputs),
            tuple(failed_steps),
            snapshot.id,
            collection.id,
        )

    def _ensure_host(self, profile: ConnectionProfileRead) -> HostRead:
        if profile.host_id is not None:
            existing = self.workspace.hosts.get(str(profile.host_id))
            if existing is None:
                raise ValueError("O host associado ao perfil não existe neste workspace.")
            return existing
        existing = self.workspace.hosts.get(profile.host)
        if existing is not None:
            return existing
        try:
            parsed = ipaddress.ip_address(profile.host)
            hostname = None
        except ValueError:
            addresses = self._resolve_profile_host(profile.host)
            if not addresses:
                raise ValueError(
                    "O hostname do perfil não pôde ser resolvido; associe o perfil a uma "
                    "máquina cadastrada ou use um endereço IP."
                ) from None
            parsed = addresses[0]
            hostname = profile.host
        existing = self.workspace.hosts.get(str(parsed))
        if existing is not None:
            if hostname and existing.hostname != hostname:
                return self.workspace.hosts.update_metadata(existing.id, hostname=hostname)
            return existing
        name = profile.name
        named = self.workspace.hosts.get(name)
        if named is not None and str(named.ip) != str(parsed):
            name = f"{profile.name}-{str(parsed).replace(':', '-').replace('.', '-')}"[:120]
        return self.workspace.add_host(HostCreate(name=name, ip=parsed, hostname=hostname))

    @staticmethod
    def _resolve_profile_host(value: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """Resolve a profile hostname without using it as a host identity."""

        try:
            records = socket.getaddrinfo(value, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return []
        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for record in records:
            try:
                address = ipaddress.ip_address(str(record[4][0]))
            except (IndexError, ValueError):
                continue
            if address not in addresses:
                addresses.append(address)
        return addresses

    def _networks_for_host(self, host_id: int) -> list[dict[str, Any]]:
        rows = ObservationRepository(self.workspace.database, self.workspace.lab.id).list_table(
            "networks"
        )
        return [
            row for row in rows if host_id in json.loads(str(row["reachable_via_json"] or "[]"))
        ]

    def _run(self, profile: ConnectionProfileRead, args: tuple[str, ...]) -> str:
        command = ConnectionService(self.workspace).ssh_command(profile.id, *args, batch_mode=True)
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=15, check=False
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"Não foi possível inspecionar a conexão SSH: {error}") from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "sem saída").strip()
            raise RuntimeError(f"Inspeção SSH falhou: {detail}")
        return completed.stdout

    async def _run_async(
        self, manager: Any, profile: ConnectionProfileRead, args: tuple[str, ...]
    ) -> str:
        command = shlex.join(args)
        try:
            result = await manager.run(profile.id, f"LC_ALL=C {command}", timeout=15)
        except Exception as error:
            raise RuntimeError(f"Não foi possível inspecionar a conexão SSH: {error}") from error
        returncode = getattr(result, "exit_status", getattr(result, "returncode", 0))
        stdout = getattr(result, "stdout", "")
        stderr = getattr(result, "stderr", "")
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if returncode != 0:
            detail = (stderr or stdout or "sem saída").strip()
            raise RuntimeError(f"Inspeção SSH falhou: {detail}")
        return str(stdout)
