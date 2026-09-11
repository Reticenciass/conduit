"""Network observation models used by imports and future TUI views."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress


class InterfaceRead(BaseModel):
    """An interface address associated with a host."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    host_id: int
    name: str
    ip: IPvAnyAddress
    prefix: int = Field(ge=0, le=128)
    family: str
    network: str
    network_scope: str = "default"
    mac: str | None
    state: str | None
    observed_at: datetime


class NetworkRead(BaseModel):
    """A CIDR network observed in the workspace."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    cidr: str
    scope: str = "default"
    gateway: str | None
    reachable_via: tuple[int, ...] = ()
    observed_at: datetime


class RouteRead(BaseModel):
    """A route observed on a host."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    host_id: int
    destination: str
    via: str | None
    dev: str | None
    metric: int | None
    source: str | None
    observed_at: datetime


class NeighborRead(BaseModel):
    """A neighbor entry observed on a host."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    host_id: int
    ip: IPvAnyAddress
    dev: str | None
    mac: str | None
    state: str | None
    observed_at: datetime
