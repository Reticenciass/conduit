"""Application service for safe, operator-provided enumeration imports."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any

from ctfws.core.errors import EntityNotFoundError
from ctfws.database.repositories import ObservationRepository
from ctfws.events import Event
from ctfws.models.host import HostCreate
from ctfws.models.observation import ObservationSourceKind
from ctfws.parsers import (
    parse_hosts,
    parse_ip_addr,
    parse_ip_neigh,
    parse_ip_route,
    parse_resolv_conf,
    parse_ss,
    parse_system_facts,
)
from ctfws.parsers.resolv import ResolverConfig
from ctfws.services.workspace import WorkspaceService


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Counts returned to CLI and TUI after one import."""

    parser: str
    records: int
    details: dict[str, int]


class EnumerationService:
    """Parse and persist text without executing commands or network activity."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace
        self.observations = ObservationRepository(workspace.database, workspace.lab.id)

    def _host_id(self, identifier: str, network_scope: str = "default") -> int:
        host = self.workspace.hosts.get(identifier, network_scope=network_scope)
        if host is None:
            raise EntityNotFoundError(f"Host {identifier} não encontrado neste laboratório.")
        return host.id

    def _ensure_observed_host(
        self, address: str, name: str | None = None, network_scope: str = "default"
    ) -> None:
        """Materialize a non-local IP found in imported evidence."""

        try:
            parsed = ip_address(address)
        except ValueError:
            return
        if parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast:
            return
        if self.workspace.hosts.get(str(parsed), network_scope=network_scope) is not None:
            return
        known_names = {host.name for host in self.workspace.hosts.list()}
        safe_name = name if name and name not in known_names else None
        self.workspace.add_host(HostCreate(ip=parsed, name=safe_name, network_scope=network_scope))

    def import_ip_addr(
        self,
        identifier: str,
        text: str,
        *,
        source_kind: ObservationSourceKind = ObservationSourceKind.MANUAL_IMPORT,
        snapshot_id: int | None = None,
        network_scope: str = "default",
    ) -> ImportResult:
        host_id = self._host_id(identifier, network_scope)
        records = parse_ip_addr(text)
        source = self.workspace.sources.create_from_text(
            host_id=host_id,
            text=text,
            command="ip addr",
            origin=source_kind.value,
            kind=source_kind,
            snapshot_id=snapshot_id,
            metadata={"parser": "ip_addr"},
        )
        interfaces = self.observations.save_interfaces(
            host_id, records, source.id, network_scope=network_scope
        )
        networks: set[str] = set()
        for record in records:
            for address in record.addresses:
                networks.add(address.network)
                self.observations.save_network(
                    address.network,
                    reachable_via=[host_id],
                    source_id=source.id,
                    scope=network_scope,
                )
        self.observations.save_command(host_id, "ip addr", text, source.id, snapshot_id)
        for network in sorted(networks):
            self.workspace._emit(
                Event(
                    event_type="NETWORK_DISCOVERED",
                    message=f"Network {network} observed on host {identifier}",
                    entity_type="network",
                    payload={"cidr": network, "host_id": host_id},
                )
            )
        self.workspace._emit(
            Event(
                event_type="ENUMERATION_IMPORTED",
                message=f"ip addr imported for {identifier}",
                entity_type="host",
                entity_id=host_id,
                payload={"parser": "ip_addr", "interfaces": interfaces, "networks": len(networks)},
            )
        )
        return ImportResult(
            "ip_addr", interfaces, {"interfaces": interfaces, "networks": len(networks)}
        )

    def import_route(
        self,
        identifier: str,
        text: str,
        *,
        source_kind: ObservationSourceKind = ObservationSourceKind.MANUAL_IMPORT,
        snapshot_id: int | None = None,
        network_scope: str = "default",
    ) -> ImportResult:
        host_id = self._host_id(identifier, network_scope)
        records = parse_ip_route(text)
        source = self.workspace.sources.create_from_text(
            host_id=host_id,
            text=text,
            command="ip route",
            origin=source_kind.value,
            kind=source_kind,
            snapshot_id=snapshot_id,
            metadata={"parser": "ip_route"},
        )
        routes = self.observations.save_routes(host_id, records, source.id)
        networks: set[str] = set()
        for record in records:
            if record.destination != "0.0.0.0/0":
                networks.add(record.destination)
                self.observations.save_network(
                    record.destination,
                    record.via,
                    [host_id],
                    source.id,
                    scope=network_scope,
                )
            if record.via:
                self._ensure_observed_host(record.via, network_scope=network_scope)
        self.observations.save_command(host_id, "ip route", text, source.id, snapshot_id)
        self.workspace._emit(
            Event(
                event_type="ENUMERATION_IMPORTED",
                message=f"ip route imported for {identifier}",
                entity_type="host",
                entity_id=host_id,
                payload={"parser": "ip_route", "routes": routes, "networks": len(networks)},
            )
        )
        return ImportResult("ip_route", routes, {"routes": routes, "networks": len(networks)})

    def import_neigh(
        self,
        identifier: str,
        text: str,
        *,
        source_kind: ObservationSourceKind = ObservationSourceKind.MANUAL_IMPORT,
        snapshot_id: int | None = None,
    ) -> ImportResult:
        host_id = self._host_id(identifier)
        records = parse_ip_neigh(text)
        source = self.workspace.sources.create_from_text(
            host_id=host_id,
            text=text,
            command="ip neigh",
            origin=source_kind.value,
            kind=source_kind,
            snapshot_id=snapshot_id,
            metadata={"parser": "ip_neigh"},
        )
        neighbors = self.observations.save_neighbors(host_id, records, source.id)
        for record in records:
            self._ensure_observed_host(record.ip)
        self.observations.save_command(host_id, "ip neigh", text, source.id, snapshot_id)
        self.workspace._emit(
            Event(
                event_type="ENUMERATION_IMPORTED",
                message=f"ip neigh imported for {identifier}",
                entity_type="host",
                entity_id=host_id,
                payload={"parser": "ip_neigh", "neighbors": neighbors},
            )
        )
        return ImportResult("ip_neigh", neighbors, {"neighbors": neighbors})

    def import_ss(
        self,
        identifier: str,
        text: str,
        *,
        source_kind: ObservationSourceKind = ObservationSourceKind.MANUAL_IMPORT,
        snapshot_id: int | None = None,
    ) -> ImportResult:
        host_id = self._host_id(identifier)
        records = parse_ss(text)
        source = self.workspace.sources.create_from_text(
            host_id=host_id,
            text=text,
            command="ss -tunap",
            origin=source_kind.value,
            kind=source_kind,
            snapshot_id=snapshot_id,
            metadata={"parser": "ss"},
        )
        services, connections = self.observations.save_sockets(host_id, records, source.id)
        for record in records:
            if record.peer_address:
                self._ensure_observed_host(record.peer_address)
        self.observations.save_command(host_id, "ss -tunap", text, source.id, snapshot_id)
        self.workspace._emit(
            Event(
                event_type="ENUMERATION_IMPORTED",
                message=f"ss imported for {identifier}",
                entity_type="host",
                entity_id=host_id,
                payload={"parser": "ss", "services": services, "connections": connections},
            )
        )
        return ImportResult("ss", len(records), {"services": services, "connections": connections})

    def import_hosts(
        self,
        identifier: str,
        text: str,
        *,
        source_kind: ObservationSourceKind = ObservationSourceKind.MANUAL_IMPORT,
        snapshot_id: int | None = None,
    ) -> ImportResult:
        host_id = self._host_id(identifier)
        entries = parse_hosts(text)
        pairs = [(entry.address, name) for entry in entries for name in entry.names]
        for address, name in pairs:
            self._ensure_observed_host(address, name)
        source = self.workspace.sources.create_from_text(
            host_id=host_id,
            text=text,
            command="cat /etc/hosts",
            origin=source_kind.value,
            kind=source_kind,
            snapshot_id=snapshot_id,
            metadata={"parser": "hosts"},
        )
        count = self.observations.save_hosts_entries(host_id, pairs, "/etc/hosts", source.id)
        self.observations.save_command(host_id, "cat /etc/hosts", text, source.id, snapshot_id)
        self.workspace._emit(
            Event(
                event_type="ENUMERATION_IMPORTED",
                message=f"/etc/hosts imported for {identifier}",
                entity_type="host",
                entity_id=host_id,
                payload={"parser": "hosts", "entries": count},
            )
        )
        return ImportResult("hosts", count, {"entries": count})

    def import_resolv(
        self,
        identifier: str,
        text: str,
        *,
        source_kind: ObservationSourceKind = ObservationSourceKind.MANUAL_IMPORT,
        snapshot_id: int | None = None,
    ) -> ImportResult:
        host_id = self._host_id(identifier)
        config = parse_resolv_conf(text)
        source = self.workspace.sources.create_from_text(
            host_id=host_id,
            text=text,
            command="cat /etc/resolv.conf",
            origin=source_kind.value,
            kind=source_kind,
            snapshot_id=snapshot_id,
            metadata={"parser": "resolv"},
        )
        self.observations.save_command(
            host_id, "cat /etc/resolv.conf", text, source.id, snapshot_id
        )
        self.workspace._emit(
            Event(
                event_type="ENUMERATION_IMPORTED",
                message=f"resolv.conf imported for {identifier}",
                entity_type="host",
                entity_id=host_id,
                payload={
                    "parser": "resolv",
                    "nameservers": list(config.nameservers),
                    "search_domains": list(config.search_domains),
                },
            )
        )
        return ImportResult(
            "resolv",
            len(config.nameservers),
            {"nameservers": len(config.nameservers), "search_domains": len(config.search_domains)},
        )

    def import_system(
        self,
        identifier: str,
        text: str,
        *,
        source_kind: ObservationSourceKind = ObservationSourceKind.MANUAL_IMPORT,
        snapshot_id: int | None = None,
    ) -> ImportResult:
        """Import normalized system facts in `KEY=value` format."""

        host_id = self._host_id(identifier)
        facts = parse_system_facts(text)
        source = self.workspace.sources.create_from_text(
            host_id=host_id,
            text=text,
            command="ctfws system facts",
            origin=source_kind.value,
            kind=source_kind,
            snapshot_id=snapshot_id,
            metadata={"parser": "system"},
        )
        self.observations.save_system_facts(host_id, facts, source.id)
        self.workspace.hosts.update_metadata(
            host_id,
            hostname=facts.fqdn or facts.hostname,
            os=facts.distribution,
            user=facts.user,
            status="online",
        )
        self.observations.save_command(host_id, "ctfws system facts", text, source.id, snapshot_id)
        self.workspace._emit(
            Event(
                event_type="SYSTEM_FACTS_IMPORTED",
                message=f"System facts imported for {identifier}",
                entity_type="host",
                entity_id=host_id,
                payload={
                    "parser": "system",
                    "user": facts.user,
                    "hostname": facts.hostname,
                    "distribution": facts.distribution,
                },
            )
        )
        return ImportResult("system", 1, {"facts": 1})

    def import_quick(
        self,
        identifier: str,
        payload: dict[str, Any],
        *,
        snapshot_id: int | None = None,
        network_scope: str = "default",
    ) -> list[ImportResult]:
        """Import a JSON document produced by the safe agent."""

        results: list[ImportResult] = []
        system = payload.get("system")
        if isinstance(system, dict):
            facts_text = "\n".join(f"{key}={value or ''}" for key, value in system.items())
            results.append(
                self.import_system(
                    identifier,
                    facts_text,
                    source_kind=ObservationSourceKind.AGENT,
                    snapshot_id=snapshot_id,
                )
            )
        outputs = payload.get("outputs")
        if not isinstance(outputs, dict):
            return results
        parsers = {
            "ip_addr": self.import_ip_addr,
            "ip_route": self.import_route,
            "ip_neigh": self.import_neigh,
            "ss": self.import_ss,
            "hosts": self.import_hosts,
            "resolv": self.import_resolv,
        }
        for key, importer in parsers.items():
            output = outputs.get(key)
            if isinstance(output, str) and output.strip():
                results.append(
                    importer(
                        identifier,
                        output,
                        source_kind=ObservationSourceKind.AGENT,
                        snapshot_id=snapshot_id,
                        **(
                            {"network_scope": network_scope}
                            if key in {"ip_addr", "ip_route"}
                            else {}
                        ),
                    )
                )
        return results


def summarize_resolver(config: ResolverConfig) -> str:
    """Render resolver data for future report consumers."""

    return ", ".join(config.nameservers) or "none"
