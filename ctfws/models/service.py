"""Socket and service observation models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ServiceRead(BaseModel):
    """A local listening socket classified as a service."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    host_id: int
    address: str
    port: int | None = Field(default=None, ge=0, le=65535)
    protocol: str
    state: str
    process: str | None
    description: str | None
    observed_at: datetime


class ConnectionRead(BaseModel):
    """An established or non-listening socket connection."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    host_id: int
    protocol: str
    local_address: str
    local_port: int | None = Field(default=None, ge=0, le=65535)
    remote_address: str | None
    remote_port: int | None = Field(default=None, ge=0, le=65535)
    state: str
    process: str | None
    observed_at: datetime
