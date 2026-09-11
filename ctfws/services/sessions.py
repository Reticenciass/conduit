"""Session registration and liveness checks."""

from __future__ import annotations

from ctfws.core.errors import EntityNotFoundError
from ctfws.core.process import is_process_alive
from ctfws.events import Event
from ctfws.models.session import SessionCreate, SessionRead, SessionStatus
from ctfws.services.workspace import WorkspaceService


class SessionService:
    """Keep operator sessions visible and independently health-checkable."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace

    def add(self, data: SessionCreate) -> SessionRead:
        if data.host_id is not None and self.workspace.hosts.get(str(data.host_id)) is None:
            raise EntityNotFoundError(f"Host {data.host_id} não encontrado neste laboratório.")
        session = self.workspace.sessions.create(data)
        self.workspace._emit(
            Event(
                event_type="SESSION_REGISTERED",
                message=f"Session {session.name} registered",
                entity_type="session",
                entity_id=session.id,
                payload={"transport": session.transport.value, "host_id": session.host_id},
            )
        )
        return session

    def list(self, status: SessionStatus | None = None) -> list[SessionRead]:
        return self.workspace.sessions.list(status)

    def check(self, session_id: int) -> SessionRead:
        session = self.workspace.sessions.get(session_id)
        if session is None:
            raise EntityNotFoundError(f"Session {session_id} não encontrada.")
        previous = session.status
        if session.status == SessionStatus.CLOSED:
            return session

        if session.pid is None:
            checked = self.workspace.sessions.update_health(
                session_id,
                status=(
                    SessionStatus.DEGRADED
                    if session.status in {SessionStatus.ACTIVE, SessionStatus.CONNECTING}
                    else session.status
                ),
                health="unmanaged",
                error="Nenhum PID associado; verificação de processo indisponível.",
            )
        elif is_process_alive(session.pid):
            checked = self.workspace.sessions.update_health(
                session_id,
                status=SessionStatus.ACTIVE,
                health="process_alive",
                error=None,
            )
        else:
            checked = self.workspace.sessions.update_health(
                session_id,
                status=SessionStatus.LOST,
                health="process_dead",
                error=f"Processo {session.pid} não está ativo.",
            )

        if checked.status != previous:
            self.workspace._emit(
                Event(
                    event_type="SESSION_HEALTH_CHANGED",
                    message=f"Session {checked.name} is {checked.status.value}",
                    entity_type="session",
                    entity_id=checked.id,
                    payload={
                        "previous_status": previous.value,
                        "status": checked.status.value,
                        "health": checked.health,
                    },
                )
            )
        return checked

    def close(self, session_id: int) -> SessionRead:
        session = self.workspace.sessions.get(session_id)
        if session is None:
            raise EntityNotFoundError(f"Session {session_id} não encontrada.")
        if session.status == SessionStatus.CLOSED:
            return session
        closed = self.workspace.sessions.update_health(
            session_id,
            status=SessionStatus.CLOSED,
            health="closed",
            error=None,
        )
        self.workspace._emit(
            Event(
                event_type="SESSION_CLOSED",
                message=f"Session {closed.name} closed",
                entity_type="session",
                entity_id=closed.id,
            )
        )
        return closed
