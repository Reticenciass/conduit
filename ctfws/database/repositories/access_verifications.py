"""Persistence for explicit connectivity checks on access paths."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.access import AccessVerificationCreate, AccessVerificationRead


class AccessVerificationRepository:
    """Keep proof records separate from inferred topology."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(
        self,
        data: AccessVerificationCreate,
        *,
        status: str,
        result: dict[str, object],
        error: str | None = None,
        ttl_seconds: int = 300,
    ) -> AccessVerificationRead:
        checked_at = utc_now()
        checked = datetime.fromisoformat(checked_at).replace(tzinfo=UTC)
        expires = datetime.fromtimestamp(checked.timestamp() + ttl_seconds, UTC).isoformat()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO access_path_checks(
                    lab_id, path_id, target_address, target_port, context,
                    timeout_seconds, status, result_json, error, checked_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.lab_id,
                    data.path_id,
                    data.target_address,
                    data.target_port,
                    data.context,
                    data.timeout_seconds,
                    status,
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    error,
                    checked_at,
                    expires,
                ),
            )
            row = connection.execute(
                "SELECT * FROM access_path_checks WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def latest_valid(self, path_id: int) -> AccessVerificationRead | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM access_path_checks
                WHERE lab_id = ? AND path_id = ? AND status = 'reachable'
                    AND expires_at > ?
                ORDER BY checked_at DESC, id DESC LIMIT 1
                """,
                (self.lab_id, path_id, utc_now()),
            ).fetchone()
        return self._to_model(row) if row else None

    def list_for_path(self, path_id: int, limit: int = 20) -> list[AccessVerificationRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM access_path_checks
                WHERE lab_id = ? AND path_id = ?
                ORDER BY checked_at DESC, id DESC LIMIT ?
                """,
                (self.lab_id, path_id, max(1, min(limit, 100))),
            ).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> AccessVerificationRead:
        raw = dict(row)
        raw["result"] = json.loads(raw.pop("result_json") or "{}")
        return AccessVerificationRead.model_validate(raw)
