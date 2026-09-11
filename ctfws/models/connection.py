"""Connection profiles used by the guided workspace UI.

Secrets are intentionally represented by references only.  The workspace never
stores an SSH password and never accepts a raw shell command as a connection
profile.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ConnectionTransport(StrEnum):
    SSH = "ssh"


class ConnectionState(StrEnum):
    """Lifecycle of the reusable transport connection."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    DEGRADED = "degraded"
    ERROR = "error"


class ConnectionProfileCreate(BaseModel):
    """Validated connection profile input."""

    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1, max_length=120)
    transport: ConnectionTransport = ConnectionTransport.SSH
    identity_file: str | None = Field(default=None, max_length=500)
    known_hosts_file: str | None = Field(default=None, max_length=500)
    auth_ref: str | None = Field(default=None, max_length=200)
    host_id: int | None = Field(default=None, ge=1)
    jump_profile_ids: tuple[int, ...] = ()
    auth_method: str = Field(default="agent_or_key", max_length=40)
    tags: tuple[str, ...] = ()
    metadata: dict[str, str] = Field(default_factory=dict)


class ConnectionProfileUpdate(BaseModel):
    """Editable non-secret profile fields guarded by a revision."""

    name: str = Field(min_length=1, max_length=120)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1, max_length=120)
    identity_file: str | None = Field(default=None, max_length=500)
    known_hosts_file: str | None = Field(default=None, max_length=500)
    auth_ref: str | None = Field(default=None, max_length=200)
    host_id: int | None = Field(default=None, ge=1)
    jump_profile_ids: tuple[int, ...] = ()
    auth_method: str = Field(default="agent_or_key", max_length=40)
    tags: tuple[str, ...] = ()
    metadata: dict[str, str] = Field(default_factory=dict)


class ConnectionProfileRead(ConnectionProfileCreate):
    """Persisted connection profile."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    state: ConnectionState = ConnectionState.DISCONNECTED
    last_checked_at: datetime | None = None
    last_error: str | None = None
    revision: int = 1
    generation: int = 0
    capabilities: tuple[str, ...] = ()
    created_at: datetime
    updated_at: datetime
