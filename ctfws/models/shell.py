"""Shell and terminal-session models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ShellType(StrEnum):
    REVERSE = "reverse"
    BIND = "bind"
    SSH = "ssh"
    TMUX = "tmux"
    OTHER = "other"


class ShellStatus(StrEnum):
    ACTIVE = "active"
    DETACHED = "detached"
    LOST = "lost"
    CLOSED = "closed"


class ShellCreate(BaseModel):
    """Input for registering an existing shell."""

    host_id: int
    name: str = Field(min_length=1, max_length=120)
    type: ShellType = ShellType.OTHER
    user: str | None = Field(default=None, max_length=120)
    terminal: str | None = Field(default=None, max_length=120)
    status: ShellStatus = ShellStatus.ACTIVE
    notes: str | None = Field(default=None, max_length=10000)


class ShellRead(ShellCreate):
    """Persisted shell record."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    created_at: datetime
    updated_at: datetime
