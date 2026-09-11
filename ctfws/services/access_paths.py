"""Inference of explainable network access paths."""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from collections import deque
from dataclasses import dataclass

from ctfws.database.repositories import ObservationRepository
from ctfws.events import Event
from ctfws.models.access import (
    AccessPathCreate,
    AccessPathRead,
    AccessPathState,
    AccessVerificationCreate,
)
from ctfws.models.session import SessionStatus
from ctfws.services.workspace import WorkspaceService


@dataclass(frozen=True, slots=True)
class AccessPathRebuildResult:
    """Counts produced by a path inference pass."""

    paths: int
    targets: int
    verified: int


class AccessPathService:
    """Build candidate paths from already imported observations.

    This service never claims that a route is reachable merely because two
    records exist.  It stores the evidence used for each candidate and keeps
    the state conservative until operator sessions provide stronger proof.
    """

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace
        self.observations = ObservationRepository(workspace.database, workspace.lab.id)

    def rebuild(self) -> AccessPathRebuildResult:
        hosts = {host.id: host for host in self.workspace.hosts.list()}
        interfaces = self.observations.list_table("interfaces")
        services = self.observations.list_table("services")
        networks = self.observations.list_table("networks")
        adjacency = self._relationship_graph()
        active_sessions = self._active_sessions_by_host()

        interfaces_by_host: dict[int, list[dict[str, object]]] = {}
        for row in interfaces:
            host_id = int(row["host_id"])
            interfaces_by_host.setdefault(host_id, []).append(row)

        network_by_host: dict[int, set[tuple[str, str]]] = {}
        for row in networks:
            try:
                reachable = json.loads(str(row["reachable_via_json"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                reachable = []
            for host_id in reachable:
                network_by_host.setdefault(int(host_id), set()).add(
                    (str(row.get("scope", "default")), str(row["cidr"]))
                )

        created = 0
        verified = 0
        target_ids: set[int] = set()
        for target_id, target_interfaces in interfaces_by_host.items():
            target = hosts.get(target_id)
            if target is None:
                continue
            target_ids.add(target_id)
            service_rows = [row for row in services if int(row["host_id"]) == target_id]
            target_networks = {
                (str(row.get("network_scope", "default")), str(row["network"]))
                for row in target_interfaces
                if row.get("network")
            }
            addresses = {str(row["ip"]) for row in target_interfaces if row.get("ip")}
            addresses.add(str(target.ip))

            for target_address in sorted(addresses):
                matching_networks = self._matching_networks(target_address, target_networks)
                for network_scope, network_cidr in matching_networks:
                    for source_host_id in sorted(
                        host_id
                        for host_id, cidrs in network_by_host.items()
                        if (network_scope, network_cidr) in cidrs
                    ):
                        if source_host_id not in hosts:
                            continue
                        host_path = self._host_path(source_host_id, target_id, adjacency)
                        if not host_path:
                            continue
                        session_ids = tuple(
                            active_sessions[host_id]
                            for host_id in host_path
                            if host_id in active_sessions
                        )
                        state = (
                            AccessPathState.READY
                            if len(host_path) == 1
                            else AccessPathState.CANDIDATE
                        )
                        confidence = min(
                            100,
                            85 if len(host_path) == 1 else 65 if len(host_path) == 2 else 50,
                        )
                        reason = (
                            f"{network_scope}:{network_cidr} observed via host path "
                            f"{' -> '.join(str(item) for item in host_path)}"
                        )
                        path = self.workspace.access_paths.upsert(
                            AccessPathCreate(
                                target_host_id=target_id,
                                target_address=target_address,
                                network_scope=network_scope,
                                hop_host_ids=tuple(host_path),
                                hop_session_ids=session_ids,
                                state=state,
                                confidence=confidence,
                                reason=reason,
                                source="network-observation",
                            )
                        )
                        check = self.workspace.access_verifications.latest_valid(path.id)
                        if check is not None:
                            path = self.workspace.access_paths.mark_verified(
                                path.id, check.id, check.checked_at.isoformat()
                            )
                        created += 1
                        verified += int(path.state == AccessPathState.VERIFIED)

                        for service in service_rows:
                            port = service.get("port")
                            if not isinstance(port, int):
                                continue
                            service_path = self.workspace.access_paths.upsert(
                                AccessPathCreate(
                                    target_host_id=target_id,
                                    target_address=target_address,
                                    network_scope=network_scope,
                                    target_port=port,
                                    service_id=int(service["id"]),
                                    hop_host_ids=tuple(host_path),
                                    hop_session_ids=session_ids,
                                    state=state,
                                    confidence=max(0, confidence - 5),
                                    reason=f"{reason}; service {service.get('protocol')}:{port}",
                                    source="network-observation",
                                )
                            )
                            check = self.workspace.access_verifications.latest_valid(
                                service_path.id
                            )
                            if check is not None:
                                service_path = self.workspace.access_paths.mark_verified(
                                    service_path.id, check.id, check.checked_at.isoformat()
                                )
                            created += 1
                            verified += int(service_path.state == AccessPathState.VERIFIED)

        if created:
            self.workspace._emit(
                Event(
                    event_type="ACCESS_PATHS_REBUILT",
                    message=f"Rebuilt {created} access path projections",
                    entity_type="access_path",
                    payload={"paths": created, "targets": len(target_ids), "verified": verified},
                )
            )
        return AccessPathRebuildResult(created, len(target_ids), verified)

    def verify(self, path_id: int) -> AccessPathRead:
        """Probe exactly one declared endpoint and persist the proof."""

        path = self.workspace.access_paths.get(path_id)
        if path is None:
            raise ValueError(f"Caminho {path_id} não encontrado neste laboratório.")
        if path.target_port is None:
            raise ValueError("Somente caminhos com uma porta de destino podem ser verificados.")
        started = time.monotonic()
        status = "unreachable"
        error: str | None = None
        try:
            with socket.create_connection((path.target_address, path.target_port), timeout=2.0):
                status = "reachable"
        except OSError as exc:
            error = str(exc)
        checked = self.workspace.access_verifications.create(
            AccessVerificationCreate(
                path_id=path.id,
                target_address=path.target_address,
                target_port=path.target_port,
                context="motor",
            ),
            status=status,
            result={
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "protocol": "tcp",
                "scope": "explicit-endpoint",
            },
            error=error,
        )
        if status == "reachable":
            result = self.workspace.access_paths.mark_verified(
                path.id, checked.id, checked.checked_at.isoformat()
            )
        else:
            result = self.workspace.access_paths.mark_failed(path.id, checked.id)
        self.workspace._emit(
            Event(
                event_type="ACCESS_PATH_VERIFIED",
                message=f"Endpoint {path.target_address}:{path.target_port} checked",
                entity_type="access_path",
                entity_id=path.id,
                payload={
                    "verification_id": checked.id,
                    "status": status,
                    "target_port": path.target_port,
                },
            )
        )
        return result

    def list_paths(self, target_host_id: int | None = None) -> list[AccessPathRead]:
        return self.workspace.access_paths.list(target_host_id)

    def _relationship_graph(self) -> dict[int, set[int]]:
        graph: dict[int, set[int]] = {}
        for row in self.workspace.intelligence.relationships():
            source = int(row["source_host_id"])
            target = row.get("target_host_id")
            if target is None:
                continue
            target_id = int(target)
            graph.setdefault(source, set()).add(target_id)
            graph.setdefault(target_id, set()).add(source)
        return graph

    def _active_sessions_by_host(self) -> dict[int, int]:
        result: dict[int, int] = {}
        for session in self.workspace.sessions.list():
            if session.host_id is None or session.status != SessionStatus.ACTIVE:
                continue
            result.setdefault(session.host_id, session.id)
        return result

    @staticmethod
    def _host_path(
        source_host_id: int, target_host_id: int, adjacency: dict[int, set[int]]
    ) -> list[int] | None:
        if source_host_id == target_host_id:
            return [source_host_id]
        queue: deque[tuple[int, list[int]]] = deque([(source_host_id, [source_host_id])])
        visited = {source_host_id}
        while queue:
            current, path = queue.popleft()
            for neighbor in sorted(adjacency.get(current, ())):
                if neighbor in visited:
                    continue
                candidate = [*path, neighbor]
                if neighbor == target_host_id:
                    return candidate
                visited.add(neighbor)
                queue.append((neighbor, candidate))
        return [source_host_id, target_host_id]

    @staticmethod
    def _matching_networks(
        address: str, known_networks: set[tuple[str, str]]
    ) -> set[tuple[str, str]]:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return set()
        matching: set[tuple[str, str]] = set()
        for scope, cidr in known_networks:
            try:
                if parsed in ipaddress.ip_network(cidr, strict=False):
                    matching.add((scope, cidr))
            except ValueError:
                continue
        return matching
