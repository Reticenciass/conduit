"""Named workspace snapshot models."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SnapshotCreate(BaseModel):
    """Input for a named observation snapshot."""

    name: str = Field(min_length=1, max_length=120)
    purpose: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SnapshotRead(SnapshotCreate):
    """Persisted snapshot."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    created_at: datetime
