"""Persistence for managed terminal records."""

from __future__ import annotations

import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.terminal import TerminalCreate, TerminalRead, TerminalSharing, TerminalStatus


class TerminalRepository:
    """Store terminal identity and last known state."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(
        self,
        data: TerminalCreate,
        context_label: str,
        owner_subject: str | None = None,
        engine_id: str | None = None,
    ) -> TerminalRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO terminal_sessions(
                    lab_id, name, kind, connection_id, cwd, status, context_label,
                    owner_subject, sharing, engine_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'starting', ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.lab_id,
                    data.name,
                    data.kind.value,
                    data.connection_id,
                    data.cwd,
                    context_label,
                    owner_subject,
                    data.sharing.value,
                    engine_id,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM terminal_sessions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def set_sharing(self, terminal_id: int, sharing: TerminalSharing) -> TerminalRead:
        """Change visibility without changing the running process."""

        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE terminal_sessions
                SET sharing = ?, updated_at = ?
                WHERE lab_id = ? AND id = ?
                """,
                (sharing.value, utc_now(), self.lab_id, terminal_id),
            )
            row = connection.execute(
                "SELECT * FROM terminal_sessions WHERE lab_id = ? AND id = ?",
                (self.lab_id, terminal_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Terminal {terminal_id} não encontrado.")
        return self._to_model(row)

    def rename(self, terminal_id: int, name: str) -> TerminalRead:
        """Rename the record without touching the managed process."""

        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE terminal_sessions
                SET name = ?, updated_at = ?
                WHERE lab_id = ? AND id = ?
                """,
                (name, utc_now(), self.lab_id, terminal_id),
            )
            row = connection.execute(
                "SELECT * FROM terminal_sessions WHERE lab_id = ? AND id = ?",
                (self.lab_id, terminal_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Terminal {terminal_id} não encontrado.")
        return self._to_model(row)

    def list(self) -> list[TerminalRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM terminal_sessions WHERE lab_id = ? ORDER BY id",
                (self.lab_id,),
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def get(self, terminal_id: int) -> TerminalRead | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM terminal_sessions WHERE lab_id = ? AND id = ?",
                (self.lab_id, terminal_id),
            ).fetchone()
        return self._to_model(row) if row else None

    def update_runtime(
        self,
        terminal_id: int,
        *,
        status: TerminalStatus,
        pid: int | None = None,
        exit_code: int | None = None,
        engine_id: str | None = None,
        clear_engine_id: bool = False,
    ) -> TerminalRead:
        engine_sql = "NULL" if clear_engine_id else "COALESCE(?, engine_id)"
        params: list[object] = [status.value, pid, exit_code]
        if not clear_engine_id:
            params.append(engine_id)
        params.extend([utc_now(), self.lab_id, terminal_id])
        with self.database.connection() as connection:
            connection.execute(
                f"""
                UPDATE terminal_sessions
                SET status = ?, pid = ?, exit_code = ?, engine_id = {engine_sql}, updated_at = ?
                WHERE lab_id = ? AND id = ?
                """,
                tuple(params),
            )
            row = connection.execute(
                "SELECT * FROM terminal_sessions WHERE lab_id = ? AND id = ?",
                (self.lab_id, terminal_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Terminal {terminal_id} não encontrado.")
        return self._to_model(row)

    @staticmethod
    def _to_model(row: sqlite3.Row) -> TerminalRead:
        return TerminalRead.model_validate(dict(row))
