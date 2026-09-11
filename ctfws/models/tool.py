"""Local tool catalog metadata."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ToolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=1, max_length=1000)
    architecture: str = Field(default="unknown", max_length=40)
    version: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=1000)


class ToolRead(ToolCreate):
    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    size: int = 0
    sha256: str
    created_at: datetime
    updated_at: datetime
