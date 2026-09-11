"""Workspace membership and effective-role models."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceRole(StrEnum):
    """Roles assignable to a subject inside one workspace."""

    ADMIN = "admin"
    OPERATOR = "operator"
    OBSERVER = "observer"


class MembershipCreate(BaseModel):
    """Input for an explicit workspace membership."""

    subject: str = Field(min_length=1, max_length=240)
    role: WorkspaceRole = WorkspaceRole.OBSERVER


class MembershipRead(MembershipCreate):
    """Persisted workspace membership."""

    model_config = ConfigDict(extra="ignore")

    id: int
    lab_id: int
    created_at: datetime
    updated_at: datetime
