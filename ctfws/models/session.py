"""Operator session models."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionTransport(StrEnum):
    """Transport used by an operator session."""

    SSH = "ssh"
    TMUX = "tmux"
    LIGOLO_AGENT = "ligolo-agent"
    CHISEL = "chisel"
    LOCAL = "local"
    OTHER = "other"


class SessionStatus(StrEnum):
    """Lifecycle state of a session record."""

    PLANNED = "planned"
    CONNECTING = "connecting"
    ACTIVE = "active"
    DEGRADED = "degraded"
    LOST = "lost"
    CLOSED = "closed"
    ERROR = "error"


class SessionCreate(BaseModel):
    """Input for registering a session owned by the operator."""

    name: str = Field(min_length=1, max_length=120)
    host_id: int | None = None
    transport: SessionTransport = SessionTransport.OTHER
    user: str | None = Field(default=None, max_length=120)
    endpoint: str | None = Field(default=None, max_length=500)
    terminal: str | None = Field(default=None, max_length=120)
    pid: int | None = Field(default=None, ge=1)
    tmux_session: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionRead(SessionCreate):
    """Persisted session record."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    status: SessionStatus
    health: str | None
    last_checked_at: datetime | None
    error: str | None
    created_at: datetime
    updated_at: datetime
