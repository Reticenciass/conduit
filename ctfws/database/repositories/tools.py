"""Persistence for the local tool catalog."""

from __future__ import annotations

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.tool import ToolCreate, ToolRead


class ToolRepository:
    """Store tool identity and hash, never an execution result."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: ToolCreate, size: int, sha256: str) -> ToolRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tool_catalog(
                    lab_id, name, path, architecture, version, notes,
                    size, sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.lab_id,
                    data.name,
                    data.path,
                    data.architecture,
                    data.version,
                    data.notes,
                    size,
                    sha256,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM tool_catalog WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return ToolRead.model_validate(dict(row))

    def list(self) -> list[ToolRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_catalog WHERE lab_id = ? ORDER BY name, id",
                (self.lab_id,),
            ).fetchall()
        return [ToolRead.model_validate(dict(row)) for row in rows]

    def get(self, tool_id: int) -> ToolRead | None:
        """Return a tool only when it belongs to this workspace."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM tool_catalog WHERE lab_id = ? AND id = ?",
                (self.lab_id, tool_id),
            ).fetchone()
        return ToolRead.model_validate(dict(row)) if row else None
