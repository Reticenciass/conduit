"""Workspace membership persistence."""

from __future__ import annotations

import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.membership import MembershipCreate, MembershipRead


class MembershipRepository:
    """Store role assignments scoped to one lab/workspace."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def upsert(self, data: MembershipCreate) -> MembershipRead:
        now = utc_now()
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO workspace_memberships(lab_id, subject, role, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lab_id, subject) DO UPDATE SET
                    role=excluded.role, updated_at=excluded.updated_at
                """,
                (self.lab_id, data.subject, data.role.value, now, now),
            )
            row = connection.execute(
                """
                SELECT * FROM workspace_memberships
                WHERE lab_id = ? AND subject = ?
                """,
                (self.lab_id, data.subject),
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def get(self, subject: str) -> MembershipRead | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM workspace_memberships
                WHERE lab_id = ? AND subject = ?
                """,
                (self.lab_id, subject),
            ).fetchone()
        return self._to_model(row) if row else None

    def list(self) -> list[MembershipRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workspace_memberships
                WHERE lab_id = ? ORDER BY subject
                """,
                (self.lab_id,),
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def delete(self, subject: str) -> bool:
        with self.database.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM workspace_memberships WHERE lab_id = ? AND subject = ?",
                (self.lab_id, subject),
            )
        return cursor.rowcount > 0

    def count_role(self, role: str) -> int:
        """Count assignments for a role inside this workspace."""

        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total FROM workspace_memberships
                WHERE lab_id = ? AND role = ?
                """,
                (self.lab_id, role),
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    @staticmethod
    def _to_model(row: sqlite3.Row) -> MembershipRead:
        return MembershipRead.model_validate(dict(row))
