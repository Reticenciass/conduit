"""Explicit network contexts for proxy and routed work."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ContextTransport(StrEnum):
    SOCKS = "socks"
    ROUTED = "routed"


class ContextStatus(StrEnum):
    PLANNED = "planned"
    STARTING = "starting"
    ACTIVE = "active"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    ERROR = "error"


class NetworkContextCreate(BaseModel):
    """Validated request for one isolated network context."""

    name: str = Field(min_length=1, max_length=120)
    transport: ContextTransport
    connection_id: int | None = Field(default=None, ge=1)
    network_cidrs: tuple[str, ...] = ()
    local_address: str = "127.0.0.1"
    local_port: int | None = Field(default=None, ge=1, le=65535)
    dns_mode: str = Field(default="path", max_length=40)


class NetworkContextRead(NetworkContextCreate):
    """Persisted context plan and last operational state."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    status: ContextStatus
    command: tuple[str, ...] = ()
    pid: int | None = None
    process_started_at: datetime | None = None
    process_executable: str | None = None
    process_fingerprint: str | None = None
    engine_id: str | None = None
    namespace_name: str | None = None
    resource_manifest: tuple[str, ...] = ()
    health: str | None = None
    capabilities: tuple[str, ...] = ()
    error: str | None = None
    created_at: datetime
    updated_at: datetime
