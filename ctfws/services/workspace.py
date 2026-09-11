"""Application services for workspace operations."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from pathlib import Path

from ctfws.core.errors import DuplicateEntityError, EntityNotFoundError
from ctfws.core.paths import WorkspacePaths
from ctfws.database.db import Database
from ctfws.database.repositories import (
    AccessPathRepository,
    AccessVerificationRepository,
    AuditRepository,
    CollectionRepository,
    ConnectionRepository,
    ContextRepository,
    EventRepository,
    EvidenceRepository,
    ForwardRepository,
    HostRepository,
    IntelligenceRepository,
    LabRepository,
    MembershipRepository,
    NoteRepository,
    ObservationSourceRepository,
    SessionRepository,
    ShellRepository,
    SnapshotRepository,
    TagRepository,
    TaskRepository,
    TerminalRepository,
    ToolRepository,
    TransferRepository,
)
from ctfws.events import Event, EventBus
from ctfws.models.host import HostCreate, HostRead
from ctfws.models.lab import LabCreate, LabRead
from ctfws.models.note import NoteCreate, NoteRead
from ctfws.services.ports import PortLeaseRegistry


class WorkspaceService:
    """Coordinate validated models, repositories and domain events."""

    def __init__(self, paths: WorkspacePaths, bus: EventBus | None = None) -> None:
        self.paths = paths
        # Older workspaces may have a valid database but no operational
        # directories yet. Recreate the safe layout before any service uses it.
        self.paths.ensure_layout()
        self.database = Database(paths.database)
        self.database.initialize()
        lab = LabRepository(self.database).get()
        if lab is None:
            raise EntityNotFoundError("O workspace não possui um registro de laboratório.")
        self.lab = lab
        self.engine_id = uuid.uuid4().hex
        # Runtime-only reservations coordinate plans created by HTTP, CLI and
        # TUI services in this motor. They are deliberately not restored from
        # SQLite; startup revalidates persisted plans before binding them.
        self.port_leases = PortLeaseRegistry()
        self.bus = bus or EventBus()
        self.events = EventRepository(self.database, self.lab.id)
        self.hosts = HostRepository(self.database, self.lab.id)
        self.notes = NoteRepository(self.database, self.lab.id)
        self.sources = ObservationSourceRepository(self.database, self.lab.id)
        self.snapshots = SnapshotRepository(self.database, self.lab.id)
        self.sessions = SessionRepository(self.database, self.lab.id)
        self.access_paths = AccessPathRepository(self.database, self.lab.id)
        self.access_verifications = AccessVerificationRepository(self.database, self.lab.id)
        self.audit = AuditRepository(self.database, self.lab.id)
        self.collections = CollectionRepository(self.database, self.lab.id)
        self.contexts = ContextRepository(self.database, self.lab.id)
        self.transfers = TransferRepository(self.database, self.lab.id)
        self.tools = ToolRepository(self.database, self.lab.id)
        self.connections = ConnectionRepository(self.database, self.lab.id)
        self.terminals = TerminalRepository(self.database, self.lab.id)
        self.tasks = TaskRepository(self.database, self.lab.id)
        self.shells = ShellRepository(self.database, self.lab.id)
        self.forwards = ForwardRepository(self.database, self.lab.id)
        self.evidence = EvidenceRepository(self.database, self.lab.id)
        self.memberships = MembershipRepository(self.database, self.lab.id)
        self.tags = TagRepository(self.database, self.lab.id)
        self.intelligence = IntelligenceRepository(self.database, self.lab.id)
        self.logger = logging.getLogger("ctfws.workspace")

    def add_host(self, data: HostCreate) -> HostRead:
        """Add a host and emit a persisted HOST_ADDED event."""

        try:
            host = self.hosts.create(data)
        except sqlite3.IntegrityError as error:
            raise DuplicateEntityError(
                f"O host {data.ip} ou o nome {data.name or '(automático)'} já existe."
            ) from error
        self._emit(
            Event(
                event_type="HOST_ADDED",
                message=f"Host {host.name} added",
                entity_type="host",
                entity_id=host.id,
                payload={
                    "ip": str(host.ip),
                    "name": host.name,
                    "network_scope": host.network_scope,
                },
            )
        )
        return host

    def add_note(self, data: NoteCreate) -> NoteRead:
        """Add a note and emit a persisted NOTE_ADDED event."""

        if data.entity_type.value == "host" and self.hosts.get(str(data.entity_id)) is None:
            raise EntityNotFoundError(f"Host {data.entity_id} não encontrado neste laboratório.")
        note = self.notes.create(data)
        self._emit(
            Event(
                event_type="NOTE_ADDED",
                message=f"Note added to {data.entity_type.value} {data.entity_id}",
                entity_type=data.entity_type.value,
                entity_id=data.entity_id,
            )
        )
        return note

    def mark_connection_dependents(self, connection_ids: set[int], reason: str) -> None:
        """Propagate a lost owned transport to resources that explicitly depend on it."""

        if not connection_ids:
            return
        profiles = self.connections.mark_degraded(connection_ids, reason)
        forwards = self.forwards.mark_connection_lost(connection_ids, reason)
        contexts = self.contexts.mark_connection_lost(connection_ids, reason)
        for profile_id in profiles:
            self._emit(
                Event(
                    event_type="CONNECTION_DEGRADED",
                    message=f"Connection {profile_id} degraded: {reason}",
                    entity_type="connection",
                    entity_id=profile_id,
                )
            )
        for forward_id in forwards:
            self._emit(
                Event(
                    event_type="FORWARD_DEGRADED",
                    message=f"Forward {forward_id} degraded: connection lost",
                    entity_type="forward",
                    entity_id=forward_id,
                )
            )
        for context_id in contexts:
            self._emit(
                Event(
                    event_type="NETWORK_CONTEXT_DEGRADED",
                    message=f"Context {context_id} degraded: connection lost",
                    entity_type="network_context",
                    entity_id=context_id,
                )
            )

    def _emit(self, event: Event) -> None:
        """Persist first, then notify in-process consumers."""

        self.events.append(event)
        self.bus.publish(event)
        self.logger.info(
            "event=%s entity=%s id=%s", event.event_type, event.entity_type, event.entity_id
        )


def create_lab(root: Path, data: LabCreate) -> tuple[WorkspacePaths, LabRead]:
    """Create a lab directory, initialize SQLite and register its lab row."""

    paths = WorkspacePaths.create(root)
    database = Database(paths.database)
    database.initialize()
    lab = LabRepository(database).create(data, paths.root)
    EventRepository(database, lab.id).append(
        Event(
            event_type="LAB_CREATED",
            message=f"Lab {lab.name} created",
            entity_type="lab",
            entity_id=lab.id,
        )
    )
    return paths, lab
