"""Named snapshot repository."""

from __future__ import annotations

import json
import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.snapshot import SnapshotCreate, SnapshotRead


class SnapshotRepository:
    """Persist named points in the workspace's observation history."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: SnapshotCreate) -> SnapshotRead:
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO snapshots(lab_id, name, purpose, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self.lab_id,
                    data.name,
                    data.purpose,
                    json.dumps(data.metadata, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM snapshots WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def list(self) -> list[SnapshotRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM snapshots WHERE lab_id = ? ORDER BY created_at DESC, id DESC",
                (self.lab_id,),
            ).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> SnapshotRead:
        raw = dict(row)
        raw["metadata"] = json.loads(raw.pop("metadata_json") or "{}")
        return SnapshotRead.model_validate(raw)
