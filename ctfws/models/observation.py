"""Provenance models for imported or collected observations."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ObservationSourceKind(StrEnum):
    """How an observation entered the workspace."""

    MANUAL_IMPORT = "manual_import"
    AGENT = "agent"
    LOCAL_COLLECTION = "local_collection"
    FILE = "file"
    API = "api"


class ObservationSourceCreate(BaseModel):
    """Input for one provenance record."""

    host_id: int | None = None
    snapshot_id: int | None = None
    kind: ObservationSourceKind = ObservationSourceKind.MANUAL_IMPORT
    command: str | None = Field(default=None, max_length=500)
    origin: str | None = Field(default=None, max_length=500)
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime | None = None


class ObservationSourceRead(ObservationSourceCreate):
    """Persisted observation provenance."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    collected_at: datetime
