"""Alias and relationship correlation with explainable evidence."""

from __future__ import annotations

from ctfws.database.repositories import IntelligenceRepository, ObservationRepository
from ctfws.services.workspace import WorkspaceService


class IntelligenceService:
    """Build projections from already-imported observations."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace
        self.observations = ObservationRepository(workspace.database, workspace.lab.id)
        self.repository = IntelligenceRepository(workspace.database, workspace.lab.id)

    def sync_aliases(self) -> int:
        """Link `/etc/hosts` names to existing hosts with the same IP."""

        hosts = {str(host.ip): host.id for host in self.workspace.hosts.list()}
        count = 0
        for row in self.observations.list_table("dns_entries"):
            host_id = hosts.get(str(row["address"]))
            if host_id is not None:
                self.repository.add_alias(host_id, str(row["hostname"]), str(row["source"]))
                count += 1
        return count

    def rebuild_relationships(self) -> int:
        """Link imported socket connections to known host identities."""

        hosts = {str(host.ip): host.id for host in self.workspace.hosts.list()}
        count = 0
        for row in self.observations.list_table("connections"):
            remote = row["remote_address"]
            target_id = hosts.get(str(remote)) if remote else None
            if target_id is None or target_id == row["host_id"]:
                continue
            label = f"{row['protocol']}:{row['remote_port'] or '-'}"
            self.repository.add_relationship(
                source_host_id=int(row["host_id"]),
                target_host_id=target_id,
                relation_type="connects_to",
                label=label,
                confidence=90,
                evidence={"connection_id": row["id"], "state": row["state"]},
            )
            count += 1
        return count
