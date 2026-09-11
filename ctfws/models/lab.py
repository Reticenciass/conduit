"""Lab models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LabCreate(BaseModel):
    """Input required to register a new lab."""

    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(default="unknown", max_length=80)
    status: str = Field(default="active", max_length=40)


class LabRead(LabCreate):
    """Persisted lab record."""

    model_config = ConfigDict(extra="ignore")

    id: int
    path: str
    created_at: datetime
    updated_at: datetime
