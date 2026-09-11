"""Immutable audit records for operator and gateway activity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditCreate(BaseModel):
    """Input for one audit record; secret values must never be placed in details."""

    actor: str = Field(min_length=1, max_length=240)
    action: str = Field(min_length=1, max_length=240)
    resource_type: str = Field(min_length=1, max_length=80)
    resource_id: int | None = Field(default=None, ge=1)
    result: str = Field(min_length=1, max_length=40)
    correlation_id: str = Field(min_length=1, max_length=120)
    details: dict[str, Any] = Field(default_factory=dict)


class AuditRead(AuditCreate):
    """Persisted immutable audit record."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    created_at: datetime
