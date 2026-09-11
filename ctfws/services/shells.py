"""Shell registration and helper services."""

from __future__ import annotations

from ctfws.core.errors import EntityNotFoundError
from ctfws.events import Event
from ctfws.models.shell import ShellCreate, ShellRead, ShellStatus
from ctfws.services.workspace import WorkspaceService


class ShellService:
    """Manage metadata for shells that already exist in a lab."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace

    def add(self, data: ShellCreate) -> ShellRead:
        if self.workspace.hosts.get(str(data.host_id)) is None:
            raise EntityNotFoundError(f"Host {data.host_id} não encontrado neste laboratório.")
        shell = self.workspace.shells.create(data)
        self.workspace._emit(
            Event(
                event_type="SHELL_CONNECTED",
                message=f"Shell {shell.name} registered",
                entity_type="shell",
                entity_id=shell.id,
                payload={"host_id": shell.host_id, "type": shell.type.value},
            )
        )
        return shell

    def update_status(self, shell_id: int, status: ShellStatus) -> ShellRead:
        shell = self.workspace.shells.update_status(shell_id, status)
        event_type = (
            "SHELL_LOST" if status in {ShellStatus.LOST, ShellStatus.CLOSED} else "SHELL_CONNECTED"
        )
        self.workspace._emit(
            Event(
                event_type=event_type,
                message=f"Shell {shell.name} status: {status.value}",
                entity_type="shell",
                entity_id=shell.id,
            )
        )
        return shell

    def rename(self, shell_id: int, name: str) -> ShellRead:
        return self.workspace.shells.rename(shell_id, name)
