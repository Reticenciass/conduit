"""Shell registry repository."""

from __future__ import annotations

import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.shell import ShellCreate, ShellRead, ShellStatus


class ShellRepository:
    """CRUD operations for manually registered shells."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: ShellCreate) -> ShellRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shells(
                    lab_id, host_id, name, type, user, terminal, status, notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.lab_id,
                    data.host_id,
                    data.name,
                    data.type.value,
                    data.user,
                    data.terminal,
                    data.status.value,
                    data.notes,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM shells WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def list(self, status: str | None = None) -> list[ShellRead]:
        query = "SELECT * FROM shells WHERE lab_id = ?"
        params: list[object] = [self.lab_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY id"
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._to_model(row) for row in rows]

    def get(self, identifier: str) -> ShellRead | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM shells WHERE lab_id = ? AND id = ?",
                (self.lab_id, int(identifier)) if identifier.isdigit() else (self.lab_id, -1),
            ).fetchone()
        return self._to_model(row) if row else None

    def update_status(self, shell_id: int, status: ShellStatus) -> ShellRead:
        with self.database.connection() as connection:
            connection.execute(
                "UPDATE shells SET status = ?, updated_at = ? WHERE lab_id = ? AND id = ?",
                (status.value, utc_now(), self.lab_id, shell_id),
            )
            row = connection.execute(
                "SELECT * FROM shells WHERE lab_id = ? AND id = ?", (self.lab_id, shell_id)
            ).fetchone()
        if row is None:
            raise ValueError(f"Shell {shell_id} não encontrada.")
        return self._to_model(row)

    def rename(self, shell_id: int, name: str) -> ShellRead:
        with self.database.connection() as connection:
            connection.execute(
                "UPDATE shells SET name = ?, updated_at = ? WHERE lab_id = ? AND id = ?",
                (name, utc_now(), self.lab_id, shell_id),
            )
            row = connection.execute(
                "SELECT * FROM shells WHERE lab_id = ? AND id = ?", (self.lab_id, shell_id)
            ).fetchone()
        if row is None:
            raise ValueError(f"Shell {shell_id} não encontrada.")
        return self._to_model(row)

    @staticmethod
    def _to_model(row: sqlite3.Row) -> ShellRead:
        return ShellRead.model_validate(dict(row))
