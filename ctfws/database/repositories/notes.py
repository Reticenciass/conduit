"""Note repository."""

from __future__ import annotations

import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.note import NoteCreate, NoteRead


class NoteRepository:
    """Persistence operations for notes."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: NoteCreate) -> NoteRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO notes(lab_id, entity_type, entity_id, body, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self.lab_id, data.entity_type.value, data.entity_id, data.body, now, now),
            )
            row = connection.execute(
                "SELECT * FROM notes WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def list(self, entity_type: str | None = None, entity_id: int | None = None) -> list[NoteRead]:
        query = "SELECT * FROM notes WHERE lab_id = ?"
        params: list[object] = [self.lab_id]
        if entity_type:
            query += " AND entity_type = ?"
            params.append(entity_type)
        if entity_id is not None:
            query += " AND entity_id = ?"
            params.append(entity_id)
        query += " ORDER BY created_at, id"
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> NoteRead:
        return NoteRead.model_validate(dict(row))
