"""Evidence registry models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EvidenceCreate(BaseModel):
    """Input for a file, screenshot, output or other evidence record."""

    host_id: int | None = None
    type: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    path: str = Field(min_length=1, max_length=1000)


class EvidenceRead(EvidenceCreate):
    """Persisted evidence metadata."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    created_at: datetime
