"""Controlled forward-plan service."""

from __future__ import annotations

import shlex

from ctfws.core.errors import EntityNotFoundError
from ctfws.events import Event
from ctfws.models.forward import (
    ExecutionLocation,
    ForwardCreate,
    ForwardKind,
    ForwardRead,
    TransportRole,
)
from ctfws.pivot.adapters import TransportPlan
from ctfws.pivot.tools import build_forward_plan
from ctfws.services.connections import ConnectionService
from ctfws.services.ports import PortLease
from ctfws.services.workspace import WorkspaceService


class PivotService:
    """Generate and store pivot commands without running them."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace

    def preview(self, data: ForwardCreate) -> TransportPlan:
        """Build a complete transport plan without persisting or executing it."""

        _normalized, plan, lease = self._prepare(data)
        if lease is not None:
            self.workspace.port_leases.release(lease.token)
        return plan

    def add_forward(self, data: ForwardCreate) -> ForwardRead:
        data, plan, lease = self._prepare(data)
        data = data.model_copy(
            update={
                "role": TransportRole(plan.role),
                "execution_location": ExecutionLocation(plan.execution_location),
            }
        )
        try:
            forward = self.workspace.forwards.create(data, plan.command, plan.argv)
        except Exception:
            if lease is not None:
                self.workspace.port_leases.release(lease.token)
            raise
        if lease is not None:
            self.workspace.port_leases.attach("forward", forward.id, lease)
        self.workspace._emit(
            Event(
                event_type="FORWARD_PLANNED",
                message=f"Forward {forward.name} planned",
                entity_type="forward",
                entity_id=forward.id,
                payload={"tool": forward.tool, "command": forward.command},
            )
        )
        return forward

    def _prepare(
        self, data: ForwardCreate
    ) -> tuple[ForwardCreate, TransportPlan, PortLease | None]:
        lease = None
        profile = None
        if data.connection_id is not None:
            profile = self.workspace.connections.get(data.connection_id)
            if profile is None:
                raise EntityNotFoundError(
                    f"Conexão {data.connection_id} não encontrada neste laboratório."
                )
        host = self.workspace.hosts.get(str(data.via_host_id))
        if host is None:
            raise EntityNotFoundError(f"Host {data.via_host_id} não encontrado neste laboratório.")
        if data.session_id is not None and self.workspace.sessions.get(data.session_id) is None:
            raise EntityNotFoundError(f"Session {data.session_id} não encontrada.")
        try:
            if data.kind in {ForwardKind.LOCAL, ForwardKind.DYNAMIC}:
                lease = self.workspace.port_leases.reserve(data.local_address, data.local_port)
                data = data.model_copy(update={"local_port": lease.port})
            if profile is not None and data.tool.lower() == "ssh":
                # The profile-aware command builder has the complete jump chain;
                # the adapter still validates role and execution location below.
                plan = build_forward_plan(data, str(host.ip), host.user, profile)
                command = ConnectionService(self.workspace).ssh_forward_command(data, profile.id)
                plan = TransportPlan(
                    plan.tool,
                    command,
                    plan.safety_note,
                    plan.capabilities,
                    plan.role,
                    plan.execution_location,
                    tuple(shlex.split(command)),
                )
            else:
                plan = build_forward_plan(data, str(host.ip), host.user, profile)
        except Exception:
            if lease is not None:
                self.workspace.port_leases.release(lease.token)
            raise
        return data, plan, lease
