"""Command-history records for explicit imports and collectors."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CommandRead(BaseModel):
    """A command and captured output recorded by the operator."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    host_id: int | None
    command: str
    output: str
    created_at: datetime
