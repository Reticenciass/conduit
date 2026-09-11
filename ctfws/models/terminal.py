"""Managed terminal models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TerminalKind(StrEnum):
    LOCAL = "local"
    SSH = "ssh"


class TerminalStatus(StrEnum):
    STARTING = "starting"
    ACTIVE = "active"
    EXITED = "exited"
    ERROR = "error"
    CLOSED = "closed"


class TerminalSharing(StrEnum):
    """Whether observers may attach to a terminal output stream."""

    PRIVATE = "private"
    SHARED = "shared"


class TerminalCreate(BaseModel):
    """Request to open one managed terminal."""

    name: str = Field(min_length=1, max_length=120)
    kind: TerminalKind = TerminalKind.LOCAL
    connection_id: int | None = None
    cwd: str | None = Field(default=None, max_length=500)
    sharing: TerminalSharing = TerminalSharing.PRIVATE


class TerminalRead(TerminalCreate):
    """Public terminal state."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    status: TerminalStatus
    pid: int | None = None
    exit_code: int | None = None
    context_label: str
    owner_subject: str | None = None
    engine_id: str | None = None
    runtime_available: bool = False
    reconnectable: bool = False
    availability_reason: str | None = None
    created_at: datetime
    updated_at: datetime
