"""Durable metadata for one inspection snapshot run."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CollectionStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class CollectionRunRead(BaseModel):
    """Collection progress and normalized result counts."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    host_id: int | None = None
    snapshot_id: int | None = None
    status: CollectionStatus
    parser_version: str
    total_steps: int = Field(ge=0)
    completed_steps: int = Field(ge=0)
    result: dict[str, object] = {}
    created_at: datetime
    updated_at: datetime
