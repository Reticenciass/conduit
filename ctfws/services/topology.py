"""Correlation and export of observed workspace topology."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ctfws.core.time import utc_now
from ctfws.database.repositories import ObservationRepository, PivotRepository
from ctfws.events import Event
from ctfws.services.workspace import WorkspaceService


class TopologyService:
    """Build a conservative map from imported interfaces and connections."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace
        self.observations = ObservationRepository(workspace.database, workspace.lab.id)
        self.pivots = PivotRepository(workspace.database, workspace.lab.id)

    def detect_pivots(self) -> int:
        """Record candidates for hosts that expose multiple observed networks."""

        detected = 0
        networks = {
            (str(row.get("scope", "default")), str(row["cidr"])): row["id"]
            for row in self.observations.list_table("networks")
        }
        for host in self.workspace.hosts.list():
            interfaces = self.observations.list_table("interfaces", host.id)
            cidrs = sorted(
                {
                    (str(row.get("network_scope", "default")), str(row["network"]))
                    for row in interfaces
                    if not str(row["network"]).startswith("127.")
                }
            )
            if len(cidrs) < 2:
                continue
            for scope, cidr in cidrs[1:]:
                network_id = networks.get((scope, cidr))
                if network_id is None:
                    network_id = self.observations.save_network(
                        cidr, reachable_via=[host.id], scope=scope
                    )
                    networks[(scope, cidr)] = network_id
                self.pivots.save_candidate(
                    host_id=host.id,
                    network_id=network_id,
                    reason="host has interfaces on multiple observed networks",
                    confidence=80,
                )
                self.workspace._emit(
                    Event(
                        event_type="PIVOT_DETECTED",
                        message=f"Possible pivot: {host.name} reaches {scope}:{cidr}",
                        entity_type="host",
                        entity_id=host.id,
                        payload={"network": cidr, "scope": scope, "confidence": 80},
                    )
                )
                detected += 1
        return detected

    def render_text(self) -> str:
        """Render a deterministic human-readable topology view."""

        observations = self.observations
        hosts = self.workspace.hosts.list()
        networks = observations.list_table("networks")
        interfaces = observations.list_table("interfaces")
        lines = ["ATTACKER"]
        for index, network in enumerate(networks):
            branch = "└─" if index == len(networks) - 1 else "├─"
            label = _network_label(network)
            lines.append(f"  {branch} {label}")
            host_rows = [
                row
                for row in interfaces
                if row["network"] == network["cidr"]
                and row.get("network_scope", "default") == network.get("scope", "default")
            ]
            for host in hosts:
                rows = [row for row in host_rows if row["host_id"] == host.id]
                if rows:
                    addresses = ", ".join(str(row["ip"]) for row in rows)
                    lines.append(f"      └─ {host.name} ({addresses})")
        if len(lines) == 1:
            lines.append("  (no imported interfaces or networks)")
        lines.append("")
        lines.append(f"Generated at {utc_now()}")
        return "\n".join(lines)

    def graph(self) -> dict[str, object]:
        """Return an evidence-oriented graph for interactive clients.

        Numeric IDs are namespaced by entity type, so a repeated address or
        database ID cannot accidentally merge a host, network and service in
        the frontend. Edges remain labelled as observations; they are not
        promoted to verified access paths.
        """

        observations = self.observations
        hosts = self.workspace.hosts.list()
        networks = observations.list_table("networks")
        interfaces = observations.list_table("interfaces")
        services = observations.list_table("services")
        nodes: list[dict[str, object]] = [
            {"id": "attacker", "kind": "operator", "label": "Conduit / Kali"}
        ]
        edges: list[dict[str, object]] = []
        network_ids: dict[tuple[str, str], str] = {}
        for network in networks:
            scope = str(network.get("scope", "default"))
            cidr = str(network["cidr"])
            node_id = f"network:{network['id']}"
            network_ids[(scope, cidr)] = node_id
            nodes.append(
                {
                    "id": node_id,
                    "kind": "network",
                    "label": _network_label(network),
                    "scope": scope,
                    "cidr": cidr,
                    "evidence": network.get("source_id"),
                }
            )
            edges.append({"source": "attacker", "target": node_id, "relation": "observed"})
        for host in hosts:
            host_id = f"host:{host.id}"
            nodes.append(
                {
                    "id": host_id,
                    "kind": "host",
                    "label": str(host.name or host.ip),
                    "address": str(host.ip),
                    "status": str(host.status),
                }
            )
        for interface in interfaces:
            host_id = f"host:{interface['host_id']}"
            network_id = network_ids.get(
                (
                    str(interface.get("network_scope", "default")),
                    str(interface["network"]),
                )
            )
            if network_id is not None:
                edges.append(
                    {
                        "source": network_id,
                        "target": host_id,
                        "relation": "interface_observed",
                        "address": interface.get("ip"),
                    }
                )
        for service in services:
            host_id = f"host:{service['host_id']}"
            service_id = f"service:{service['id']}"
            address = str(service.get("address") or "*")
            port = service.get("port")
            label = f"{address}:{port}" if port is not None else address
            nodes.append(
                {
                    "id": service_id,
                    "kind": "service",
                    "label": label,
                    "protocol": service.get("protocol"),
                    "state": service.get("state"),
                }
            )
            edges.append({"source": host_id, "target": service_id, "relation": "service_observed"})
        return {"generated_at": utc_now(), "nodes": nodes, "edges": edges}

    def render_dot(self) -> str:
        """Render Graphviz DOT without requiring Graphviz to be installed."""

        observations = self.observations
        hosts = self.workspace.hosts.list()
        networks = observations.list_table("networks")
        interfaces = observations.list_table("interfaces")
        lines = ["digraph ctfws {", '  rankdir="TB";', '  attacker [label="ATTACKER", shape=box];']
        for network in networks:
            node = _dot_id("network", network["id"])
            lines.append(f'  {node} [label="{_dot_escape(_network_label(network))}", shape=oval];')
            lines.append(f"  attacker -> {node};")
        for host in hosts:
            node = _dot_id("host", host.id)
            label = f"{host.name or 'host-' + str(host.id)}\\n{host.ip}"
            lines.append(f'  {node} [label="{_dot_escape(label)}"];')
        for row in interfaces:
            network_id = next(
                (
                    item["id"]
                    for item in networks
                    if item["cidr"] == row["network"]
                    and item.get("scope", "default") == row.get("network_scope", "default")
                ),
                None,
            )
            if network_id is not None:
                network_node = _dot_id("network", network_id)
                host_node = _dot_id("host", row["host_id"])
                lines.append(f"  {network_node} -> {host_node};")
        lines.append("}")
        return "\n".join(lines) + "\n"

    def export(self, output_dir: Path) -> list[Path]:
        """Write text and DOT maps and render PNG when `dot` is available."""

        output_dir = output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        text_path = output_dir / "topology.txt"
        dot_path = output_dir / "topology.dot"
        text_path.write_text(self.render_text(), encoding="utf-8")
        dot_path.write_text(self.render_dot(), encoding="utf-8")
        paths = [text_path, dot_path]
        dot = shutil.which("dot")
        if dot:
            png_path = output_dir / "topology.png"
            subprocess.run(
                [dot, str(dot_path), "-Tpng", "-o", str(png_path)],
                check=False,
                capture_output=True,
                timeout=30,
            )
            if png_path.is_file():
                paths.append(png_path)
        return paths


def _dot_id(prefix: str, value: object) -> str:
    """Create a safe identifier from internal numeric IDs."""

    return f"{prefix}_{value}"


def _dot_escape(value: str) -> str:
    """Escape user-controlled labels for DOT strings."""

    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _network_label(network: dict[str, object]) -> str:
    """Make isolation visible while keeping legacy default labels concise."""

    cidr = str(network["cidr"])
    scope = str(network.get("scope", "default"))
    return cidr if scope == "default" else f"{cidr} · scope={scope}"
