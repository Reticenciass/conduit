"""Host models and validation."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress


class HostStatus(StrEnum):
    """Controlled host lifecycle values used by the workspace."""

    DISCOVERED = "discovered"
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class HostCreate(BaseModel):
    """Input for adding a host discovered by the operator."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    ip: IPvAnyAddress
    network_scope: str = Field(default="default", min_length=1, max_length=120)
    hostname: str | None = Field(default=None, max_length=255)
    os: str | None = Field(default=None, max_length=120)
    user: str | None = Field(default=None, max_length=120)
    status: HostStatus = HostStatus.DISCOVERED


class HostRead(HostCreate):
    """Persisted host record."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    created_at: datetime
    updated_at: datetime
