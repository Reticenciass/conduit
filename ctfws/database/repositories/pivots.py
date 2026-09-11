"""Pivot-candidate repository."""

from __future__ import annotations

import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.pivot import PivotRead


class PivotRepository:
    """Persist hypotheses without starting a tunnel or process."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def save_candidate(
        self,
        host_id: int,
        network_id: int,
        reason: str,
        confidence: int = 70,
    ) -> PivotRead:
        now = utc_now()
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO pivots(
                    lab_id, host_id, network_id, reason, confidence, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?)
                ON CONFLICT(host_id, network_id) DO UPDATE SET
                    reason=excluded.reason, confidence=excluded.confidence,
                    updated_at=excluded.updated_at
                """,
                (self.lab_id, host_id, network_id, reason, confidence, now, now),
            )
            row = connection.execute(
                "SELECT * FROM pivots WHERE host_id = ? AND network_id = ?", (host_id, network_id)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def list(self) -> list[PivotRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM pivots WHERE lab_id = ? ORDER BY confidence DESC, id", (self.lab_id,)
            ).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> PivotRead:
        return PivotRead.model_validate(dict(row))
