"""System-facts persistence model."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SystemFactsRead(BaseModel):
    """Basic system facts observed for a host."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    host_id: int
    user: str | None
    uid: int | None
    groups: tuple[str, ...]
    hostname: str | None
    fqdn: str | None
    kernel: str | None
    architecture: str | None
    distribution: str | None
    observed_at: datetime
