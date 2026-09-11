"""Notes attached to workspace entities."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class NoteEntityType(StrEnum):
    """Entity types supported by the polymorphic notes table."""

    LAB = "lab"
    HOST = "host"
    NETWORK = "network"
    SHELL = "shell"
    SERVICE = "service"
    PIVOT = "pivot"


class NoteCreate(BaseModel):
    """Input for a note."""

    entity_type: NoteEntityType
    entity_id: int = Field(gt=0)
    body: str = Field(min_length=1, max_length=10000)


class NoteRead(NoteCreate):
    """Persisted note."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    created_at: datetime
    updated_at: datetime
