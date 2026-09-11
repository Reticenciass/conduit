"""Markdown and HTML reports generated from workspace state."""

from __future__ import annotations

from html import escape
from pathlib import Path

from ctfws.database.repositories import ObservationRepository, PivotRepository
from ctfws.services.topology import TopologyService
from ctfws.services.workspace import WorkspaceService


class ReportService:
    """Generate reproducible reports without changing the workspace."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace
        self.observations = ObservationRepository(workspace.database, workspace.lab.id)

    def markdown(self) -> str:
        lab = self.workspace.lab
        hosts = self.workspace.hosts.list()
        networks = self.observations.list_table("networks")
        interfaces = self.observations.list_table("interfaces")
        routes = self.observations.list_table("routes")
        services = self.observations.list_table("services")
        connections = self.observations.list_table("connections")
        commands = self.observations.list_table("commands")
        shells = self.workspace.shells.list()
        sessions = self.workspace.sessions.list()
        access_paths = self.workspace.access_paths.list()
        snapshots = self.workspace.snapshots.list()
        sources = self.workspace.sources.list()
        forwards = self.workspace.forwards.list()
        pivots = PivotRepository(self.workspace.database, lab.id).list()
        notes = self.workspace.notes.list()
        evidence = self.workspace.evidence.list()
        events = list(reversed(self.workspace.events.list(limit=1000)))
        lines = [
            f"# {lab.name}",
            "",
            f"- Platform: {lab.platform}",
            f"- Status: {lab.status}",
            f"- Hosts: {len(hosts)}",
            f"- Networks: {len(networks)}",
            f"- Sessions: {len(sessions)}",
            f"- Access paths: {len(access_paths)}",
            "",
            "## Hosts",
            "",
            "| ID | Name | IP | User | OS | Status | Tags |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for host in hosts:
            lines.append(
                f"| {host.id} | {host.name or '-'} | {host.ip} | {host.user or '-'} | "
                f"{host.os or '-'} | {host.status.value} | "
                f"{', '.join(self.workspace.tags.list_for_host(host.id)) or '-'} |"
            )
        lines.extend(["", "## Networks", ""])
        for network in networks:
            lines.append(f"- `{network['cidr']}` via {network['gateway'] or 'direct'}")
        lines.extend(
            [
                "",
                "## Network topology",
                "",
                "```text",
                TopologyService(self.workspace).render_text(),
                "```",
            ]
        )
        lines.extend(["", "## Interfaces", ""])
        for row in interfaces:
            lines.append(
                f"- Host {row['host_id']}: `{row['name']}` {row['ip']}/{row['prefix']} "
                f"({row['network']})"
            )
        lines.extend(["", "## Routes", ""])
        for row in routes:
            lines.append(
                f"- Host {row['host_id']}: `{row['destination']}` via {row['via'] or 'direct'} "
                f"dev {row['dev'] or '-'}"
            )
        lines.extend(["", "## Services", ""])
        for row in services:
            lines.append(
                f"- Host {row['host_id']}: `{row['address']}:{row['port'] or '-'}` "
                f"{row['protocol']} {row['description'] or ''}".rstrip()
            )
        lines.extend(["", "## Connections", ""])
        for row in connections:
            lines.append(
                f"- Host {row['host_id']}: `{row['local_address']}:{row['local_port'] or '-'}` -> "
                f"`{row['remote_address'] or '-'}:{row['remote_port'] or '-'}` ({row['state']})"
            )
        lines.extend(["", "## Pivots", ""])
        for pivot in pivots:
            lines.append(
                f"- Host {pivot.host_id} -> network {pivot.network_id}: "
                f"{pivot.reason} ({pivot.confidence}%, {pivot.status.value})"
            )
        lines.extend(["", "## Shells", ""])
        for shell in shells:
            lines.append(
                f"- `{shell.id}` {shell.name} on host {shell.host_id} ({shell.status.value})"
            )
        lines.extend(["", "## Port forwards", ""])
        for forward in forwards:
            lines.append(
                f"- `{forward.name}` [{forward.status.value}/{forward.health or 'unknown'}]: "
                f"`{forward.command}`"
            )
        lines.extend(["", "## Sessions", ""])
        for session in sessions:
            lines.append(
                f"- `{session.name}` [{session.status.value}/{session.health or 'unknown'}] "
                f"transport={session.transport.value} host={session.host_id or '-'}"
            )
        lines.extend(["", "## Access paths", ""])
        for path in access_paths:
            hosts_path = " -> ".join(str(item) for item in path.hop_host_ids) or "-"
            lines.append(
                f"- `{path.target_address}:{path.target_port or '-'}` [{path.state.value}, "
                f"{path.confidence}%] hosts={hosts_path}: {path.reason}"
            )
        lines.extend(["", "## Notes", ""])
        for note in notes:
            lines.append(f"- {note.entity_type.value}:{note.entity_id} — {note.body}")
        lines.extend(["", "## Evidence", ""])
        for item in evidence:
            lines.append(f"- [{item.type}] {item.description}: `{item.path}`")
        lines.extend(["", "## Timeline", ""])
        for event in events:
            lines.append(f"- {event['created_at']} — {event['message']}")
        lines.extend(["", "## Command history", ""])
        for command in commands:
            lines.append(f"- Host {command['host_id'] or '-'}: `{command['command']}`")
        lines.extend(["", "## Snapshots", ""])
        for snapshot in snapshots:
            lines.append(f"- `{snapshot.name}`: {snapshot.purpose or '-'} ({snapshot.created_at})")
        lines.extend(["", "## Observation provenance", ""])
        for source in sources:
            lines.append(
                f"- {source.collected_at} host={source.host_id or '-'} "
                f"kind={source.kind.value} command={source.command or '-'} "
                f"sha256={source.content_hash}"
            )
        return "\n".join(lines) + "\n"

    def html(self) -> str:
        content = escape(self.markdown())
        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<title>CTF Workspace report</title><style>body{font-family:system-ui;"
            "max-width:1100px;margin:2rem auto;background:#10141c;color:#e5e7eb}"
            "pre{white-space:pre-wrap;background:#1e293b;padding:1.5rem;border-radius:8px}"
            "</style></head><body><pre>"
            f"{content}</pre></body></html>"
        )

    def write(self, output_dir: Path, formats: set[str]) -> list[Path]:
        """Write requested report formats and return their paths."""

        output_dir = output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        if "md" in formats:
            path = output_dir / "report.md"
            path.write_text(self.markdown(), encoding="utf-8")
            paths.append(path)
        if "html" in formats:
            path = output_dir / "report.html"
            path.write_text(self.html(), encoding="utf-8")
            paths.append(path)
        return paths
