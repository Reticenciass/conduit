"""Host repository."""

from __future__ import annotations

import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.host import HostCreate, HostRead


class HostRepository:
    """Persistence operations for hosts."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: HostCreate) -> HostRead:
        now = utc_now()
        name = data.name or f"host-{str(data.ip).replace(':', '-').replace('.', '-')}"
        with self.database.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO hosts(lab_id, name, ip, network_scope, hostname, os, user_name, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.lab_id,
                    name,
                    str(data.ip),
                    data.network_scope,
                    data.hostname,
                    data.os,
                    data.user,
                    data.status.value,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM hosts WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def get(self, identifier: str, network_scope: str = "default") -> HostRead | None:
        with self.database.connection() as connection:
            if identifier.isdigit():
                row = connection.execute(
                    "SELECT * FROM hosts WHERE lab_id = ? AND id = ?",
                    (self.lab_id, int(identifier)),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM hosts WHERE lab_id = ? AND "
                    "((ip = ? AND network_scope = ?) OR name = ?)",
                    (self.lab_id, identifier, network_scope, identifier),
                ).fetchone()
        return self._to_model(row) if row else None

    def list(self, status: str | None = None) -> list[HostRead]:
        with self.database.connection() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM hosts WHERE lab_id = ? AND status = ? ORDER BY name",
                    (self.lab_id, status),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM hosts WHERE lab_id = ? ORDER BY name", (self.lab_id,)
                ).fetchall()
        return [self._to_model(row) for row in rows]

    def update_metadata(
        self,
        host_id: int,
        *,
        hostname: str | None = None,
        os: str | None = None,
        user: str | None = None,
        status: str | None = None,
    ) -> HostRead:
        """Merge newly imported facts into an existing host record."""

        values = (hostname, os, user, status, utc_now(), self.lab_id, host_id)
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE hosts SET
                    hostname=COALESCE(?, hostname), os=COALESCE(?, os),
                    user_name=COALESCE(?, user_name), status=COALESCE(?, status),
                    updated_at=?
                WHERE lab_id=? AND id=?
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM hosts WHERE lab_id = ? AND id = ?", (self.lab_id, host_id)
            ).fetchone()
        if row is None:
            raise ValueError(f"Host {host_id} não encontrado.")
        return self._to_model(row)

    @staticmethod
    def _to_model(row: sqlite3.Row) -> HostRead:
        raw = dict(row)
        raw["user"] = raw.pop("user_name")
        return HostRead.model_validate(raw)
