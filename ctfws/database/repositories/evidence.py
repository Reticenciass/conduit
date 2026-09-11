"""Evidence metadata repository."""

from __future__ import annotations

import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.evidence import EvidenceCreate, EvidenceRead


class EvidenceRepository:
    """Persist paths and descriptions without copying or executing files."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: EvidenceCreate) -> EvidenceRead:
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO evidence(lab_id, host_id, type, description, path, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self.lab_id, data.host_id, data.type, data.description, data.path, utc_now()),
            )
            row = connection.execute(
                "SELECT * FROM evidence WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def list(self) -> list[EvidenceRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence WHERE lab_id = ? ORDER BY created_at, id", (self.lab_id,)
            ).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> EvidenceRead:
        return EvidenceRead.model_validate(dict(row))
