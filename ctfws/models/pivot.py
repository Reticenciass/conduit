"""Pivot-candidate models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PivotStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    IGNORED = "ignored"


class PivotRead(BaseModel):
    """A possible pivot relationship inferred from observed data."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    host_id: int
    network_id: int
    reason: str
    confidence: int = Field(ge=0, le=100)
    status: PivotStatus
    created_at: datetime
    updated_at: datetime
