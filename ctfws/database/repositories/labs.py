"""Lab repository."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.lab import LabCreate, LabRead


class LabRepository:
    """Persistence operations for the current workspace's lab row."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create(self, data: LabCreate, path: Path) -> LabRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO labs(name, path, platform, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (data.name, str(path), data.platform, data.status, now, now),
            )
            row = connection.execute(
                "SELECT * FROM labs WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def get(self) -> LabRead | None:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM labs ORDER BY id LIMIT 1").fetchone()
        return self._to_model(row) if row else None

    @staticmethod
    def _to_model(row: sqlite3.Row) -> LabRead:
        return LabRead.model_validate(dict(row))
