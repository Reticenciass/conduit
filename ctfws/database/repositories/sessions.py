"""Operator session repository."""

from __future__ import annotations

import json
import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.session import SessionCreate, SessionRead, SessionStatus


class SessionRepository:
    """Persist sessions independently from shell presentation metadata."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: SessionCreate) -> SessionRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sessions(
                    lab_id, host_id, name, transport, user, endpoint, terminal, pid,
                    tmux_session, status, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
                """,
                (
                    self.lab_id,
                    data.host_id,
                    data.name,
                    data.transport.value,
                    data.user,
                    data.endpoint,
                    data.terminal,
                    data.pid,
                    data.tmux_session,
                    json.dumps(data.metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def list(self, status: SessionStatus | None = None) -> list[SessionRead]:
        query = "SELECT * FROM sessions WHERE lab_id = ?"
        params: list[object] = [self.lab_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY id"
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._to_model(row) for row in rows]

    def get(self, session_id: int) -> SessionRead | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE lab_id = ? AND id = ?",
                (self.lab_id, session_id),
            ).fetchone()
        return self._to_model(row) if row else None

    def update_health(
        self,
        session_id: int,
        *,
        status: SessionStatus,
        health: str | None,
        error: str | None = None,
    ) -> SessionRead:
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE sessions SET status = ?, health = ?, error = ?,
                    last_checked_at = ?, updated_at = ?
                WHERE lab_id = ? AND id = ?
                """,
                (
                    status.value,
                    health,
                    error,
                    utc_now(),
                    utc_now(),
                    self.lab_id,
                    session_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM sessions WHERE lab_id = ? AND id = ?",
                (self.lab_id, session_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Session {session_id} não encontrada.")
        return self._to_model(row)

    @staticmethod
    def _to_model(row: sqlite3.Row) -> SessionRead:
        raw = dict(row)
        raw["metadata"] = json.loads(raw.pop("metadata_json") or "{}")
        return SessionRead.model_validate(raw)
