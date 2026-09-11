"""Durable asynchronous workspace task models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TaskStatus(StrEnum):
    """Lifecycle states for operations which may outlive an HTTP request."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class TaskCreate(BaseModel):
    """Input used to enqueue one workspace operation."""

    kind: str = Field(min_length=1, max_length=120)
    resource_type: str | None = Field(default=None, max_length=80)
    resource_id: int | None = Field(default=None, ge=1)
    total_steps: int = Field(default=0, ge=0, le=10000)
    idempotency_key: str | None = Field(default=None, max_length=200)
    requested_by: str | None = Field(default=None, max_length=200)


class TaskRead(TaskCreate):
    """Persisted task status exposed to clients."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    status: TaskStatus
    progress: int = Field(ge=0, le=100)
    current_step: str | None
    completed_steps: int = Field(ge=0)
    result: dict[str, object]
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
