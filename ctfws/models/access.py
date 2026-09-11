"""Explainable access-path models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class AccessPathState(StrEnum):
    """Evidence state of a path to a host or service."""

    CANDIDATE = "candidate"
    READY = "ready"
    VERIFIED = "verified"
    STALE = "stale"
    FAILED = "failed"


class AccessPathCreate(BaseModel):
    """Input for one ordered path through operator sessions."""

    target_host_id: int | None = None
    target_address: str
    network_scope: str = "default"
    target_port: int | None = Field(default=None, ge=1, le=65535)
    service_id: int | None = None
    state: AccessPathState = AccessPathState.CANDIDATE
    confidence: int = Field(default=50, ge=0, le=100)
    hop_host_ids: tuple[int, ...] = ()
    hop_session_ids: tuple[int, ...] = ()
    reason: str = Field(min_length=1, max_length=1000)
    source: str = Field(default="inferred", max_length=120)


class AccessVerificationCreate(BaseModel):
    """One explicit, time-bounded proof for a concrete path endpoint."""

    path_id: int = Field(ge=1)
    target_address: str = Field(min_length=1, max_length=255)
    target_port: int = Field(ge=1, le=65535)
    context: str = Field(default="motor", min_length=1, max_length=120)
    timeout_seconds: float = Field(default=2.0, gt=0, le=30)


class AccessVerificationRead(AccessVerificationCreate):
    """Persisted connectivity proof and its expiration."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    status: str
    result: dict[str, object] = {}
    error: str | None = None
    checked_at: datetime
    expires_at: datetime


class AccessPathRead(AccessPathCreate):
    """Persisted access path."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    created_at: datetime
    updated_at: datetime
    verification_id: int | None = None
    verified_at: datetime | None = None
