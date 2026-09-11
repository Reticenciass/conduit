"""Controlled port-forward and pivot-session models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ForwardKind(StrEnum):
    LOCAL = "local"
    DYNAMIC = "dynamic"
    REMOTE = "remote"


class TransportRole(StrEnum):
    """Role played by an external transport process."""

    CLIENT = "client"
    SERVER = "server"
    CONTROL = "control"


class ExecutionLocation(StrEnum):
    """Where a reviewed transport plan is intended to run."""

    MOTOR = "motor"
    REMOTE = "remote"
    CONTEXT = "context"


class ForwardStatus(StrEnum):
    PLANNED = "planned"
    STARTING = "starting"
    ACTIVE = "active"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    ERROR = "error"


class ForwardCreate(BaseModel):
    """Input for a user-requested forward plan."""

    name: str = Field(min_length=1, max_length=120)
    via_host_id: int
    connection_id: int | None = Field(default=None, ge=1)
    kind: ForwardKind = ForwardKind.LOCAL
    local_address: str = "127.0.0.1"
    local_port: int = Field(default=0, ge=0, le=65535)
    target_address: str | None = None
    target_port: int | None = Field(default=None, ge=1, le=65535)
    user: str | None = Field(default=None, max_length=120)
    tool: str = "ssh"
    endpoint: str | None = Field(default=None, max_length=500)
    fingerprint: str | None = Field(default=None, max_length=200)
    auth_ref: str | None = Field(default=None, max_length=200)
    session_id: int | None = None
    dependency_ids: tuple[int, ...] = ()
    role: TransportRole | None = None
    execution_location: ExecutionLocation = ExecutionLocation.MOTOR


class ForwardRead(ForwardCreate):
    """Persisted forward plan."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    status: ForwardStatus
    pid: int | None = None
    command: str
    command_argv: tuple[str, ...] = ()
    health: str | None = None
    last_checked_at: datetime | None = None
    error: str | None = None
    exit_code: int | None = None
    listener_state: str | None = None
    destination_state: str | None = None
    process_started_at: datetime | None = None
    process_executable: str | None = None
    process_fingerprint: str | None = None
    engine_id: str | None = None
    created_at: datetime
    updated_at: datetime
