"""Global workspace search and command-output diff."""

from __future__ import annotations

from ctfws.database.repositories import ObservationRepository
from ctfws.services.workspace import WorkspaceService


class SearchService:
    """Search indexed workspace entities with parameterized SQL."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace

    def search(self, query: str) -> list[dict[str, str]]:
        pattern = f"%{query}%"
        result: list[dict[str, str]] = []
        with self.workspace.database.connection() as connection:
            searches = [
                (
                    "Host",
                    "SELECT id, name, ip, hostname FROM hosts WHERE lab_id = ? "
                    "AND (name LIKE ? OR ip LIKE ? OR hostname LIKE ? OR os LIKE ?)",
                    (pattern, pattern, pattern, pattern),
                ),
                (
                    "Network",
                    "SELECT id, cidr, gateway FROM networks WHERE lab_id = ? "
                    "AND (cidr LIKE ? OR gateway LIKE ?)",
                    (pattern, pattern),
                ),
                (
                    "Note",
                    "SELECT id, entity_type, entity_id, body FROM notes WHERE lab_id = ? "
                    "AND body LIKE ?",
                    (pattern,),
                ),
                (
                    "Event",
                    "SELECT id, event_type, message FROM events WHERE lab_id = ? "
                    "AND message LIKE ?",
                    (pattern,),
                ),
                (
                    "Command",
                    "SELECT id, command, output FROM commands WHERE lab_id = ? "
                    "AND (command LIKE ? OR output LIKE ?)",
                    (pattern, pattern),
                ),
            ]
            for kind, query_sql, params in searches:
                for row in connection.execute(query_sql, (self.workspace.lab.id, *params)):
                    raw = dict(row)
                    values = ", ".join(f"{key}={value}" for key, value in raw.items())
                    result.append({"kind": kind, "label": str(raw.get("id", "")), "detail": values})
        return result


class DiffService:
    """Compare two stored command outputs for one host."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace
        self.observations = ObservationRepository(workspace.database, workspace.lab.id)

    def command_diff(self, host_id: int, command: str) -> dict[str, list[str]]:
        rows = [
            row
            for row in self.observations.list_table("commands", host_id)
            if row["command"] == command
        ]
        if len(rows) < 2:
            return {"added": [], "removed": [], "message": ["at least two snapshots are required"]}
        before = set(str(rows[-2]["output"]).splitlines())
        after = set(str(rows[-1]["output"]).splitlines())
        return {
            "added": sorted(after - before),
            "removed": sorted(before - after),
            "message": [],
        }
