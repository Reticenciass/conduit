"""Durable transfer metadata and integrity state."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TransferDirection(StrEnum):
    UPLOAD = "upload"
    DOWNLOAD = "download"


class TransferStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConflictPolicy(StrEnum):
    """Explicit behavior when the destination already exists."""

    CANCEL = "cancel"
    REPLACE = "replace"
    KEEP_BOTH = "keep_both"


class TransferRead(BaseModel):
    """Recorded transfer result without storing file contents."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    connection_id: int
    direction: TransferDirection
    local_path: str
    remote_path: str
    size: int = Field(ge=0)
    bytes_transferred: int = Field(ge=0)
    local_sha256: str | None = None
    remote_sha256: str | None = None
    integrity_verified: bool = False
    status: TransferStatus
    error: str | None = None
    requested_by: str | None = None
    created_at: datetime
    updated_at: datetime
