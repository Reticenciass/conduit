"""Port-forward plan repository."""

from __future__ import annotations

import json
import sqlite3
from builtins import list as builtin_list

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.forward import ForwardCreate, ForwardRead, ForwardStatus


class ForwardRepository:
    """Persist user-approved plans and their generated commands."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(
        self,
        data: ForwardCreate,
        command: str,
        command_argv: tuple[str, ...] = (),
    ) -> ForwardRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO forwards(
                    lab_id, name, via_host_id, kind, local_address, local_port,
                    target_address, target_port, user, tool, endpoint, fingerprint, auth_ref,
                    session_id, connection_id, dependency_ids_json, role, execution_location,
                    status, command, health,
                    command_argv_json,
                    listener_state, destination_state, process_started_at, process_executable,
                    process_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, NULL,
                    ?, NULL, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    self.lab_id,
                    data.name,
                    data.via_host_id,
                    data.kind.value,
                    data.local_address,
                    data.local_port,
                    data.target_address,
                    data.target_port,
                    data.user,
                    data.tool,
                    data.endpoint,
                    data.fingerprint,
                    data.auth_ref,
                    data.session_id,
                    data.connection_id,
                    json.dumps(list(data.dependency_ids)),
                    data.role.value if data.role is not None else "client",
                    data.execution_location.value,
                    command,
                    json.dumps(list(command_argv), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM forwards WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def list(self) -> list[ForwardRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM forwards WHERE lab_id = ? ORDER BY id", (self.lab_id,)
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def get(self, forward_id: int) -> ForwardRead | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM forwards WHERE lab_id = ? AND id = ?", (self.lab_id, forward_id)
            ).fetchone()
        return self._to_model(row) if row else None

    def update_status(self, forward_id: int, status: ForwardStatus) -> ForwardRead:
        with self.database.connection() as connection:
            connection.execute(
                "UPDATE forwards SET status = ?, updated_at = ? WHERE lab_id = ? AND id = ?",
                (status.value, utc_now(), self.lab_id, forward_id),
            )
            row = connection.execute(
                "SELECT * FROM forwards WHERE lab_id = ? AND id = ?", (self.lab_id, forward_id)
            ).fetchone()
        if row is None:
            raise ValueError(f"Forward {forward_id} não encontrado.")
        return self._to_model(row)

    def set_process(
        self,
        forward_id: int,
        pid: int | None,
        status: ForwardStatus,
        *,
        health: str | None = None,
        error: str | None = None,
        exit_code: int | None = None,
        listener_state: str | None = None,
        destination_state: str | None = None,
        process_started_at: str | None = None,
        process_executable: str | None = None,
        process_fingerprint: str | None = None,
        engine_id: str | None = None,
        clear_engine_id: bool = False,
    ) -> ForwardRead:
        engine_sql = "NULL" if clear_engine_id else "COALESCE(?, engine_id)"
        engine_params: tuple[str | None, ...] = () if clear_engine_id else (engine_id,)
        with self.database.connection() as connection:
            connection.execute(
                "UPDATE forwards SET pid = ?, status = ?, health = ?, error = ?, "
                "exit_code = ?, listener_state = ?, destination_state = ?, "
                "process_started_at = ?, process_executable = ?, process_fingerprint = ?, "
                f"engine_id = {engine_sql}, last_checked_at = ?, updated_at = ? "
                "WHERE lab_id = ? AND id = ?",
                (
                    pid,
                    status.value,
                    health,
                    error,
                    exit_code,
                    listener_state,
                    destination_state,
                    process_started_at,
                    process_executable,
                    process_fingerprint,
                    *engine_params,
                    utc_now(),
                    utc_now(),
                    self.lab_id,
                    forward_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM forwards WHERE lab_id = ? AND id = ?", (self.lab_id, forward_id)
            ).fetchone()
        if row is None:
            raise ValueError(f"Forward {forward_id} não encontrado.")
        return self._to_model(row)

    def update_health(
        self,
        forward_id: int,
        *,
        status: ForwardStatus,
        health: str | None,
        error: str | None = None,
        exit_code: int | None = None,
        listener_state: str | None = None,
        destination_state: str | None = None,
    ) -> ForwardRead:
        """Update a runtime observation while keeping the process identity."""

        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE forwards SET status = ?, health = ?, error = ?, exit_code = ?,
                    listener_state = COALESCE(?, listener_state),
                    destination_state = COALESCE(?, destination_state),
                    last_checked_at = ?, updated_at = ?
                WHERE lab_id = ? AND id = ?
                """,
                (
                    status.value,
                    health,
                    error,
                    exit_code,
                    listener_state,
                    destination_state,
                    utc_now(),
                    utc_now(),
                    self.lab_id,
                    forward_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM forwards WHERE lab_id = ? AND id = ?", (self.lab_id, forward_id)
            ).fetchone()
        if row is None:
            raise ValueError(f"Forward {forward_id} não encontrado.")
        return self._to_model(row)

    def mark_connection_lost(self, connection_ids: set[int], error: str) -> builtin_list[int]:
        """Degrade active forwards whose reviewed transport disappeared."""

        if not connection_ids:
            return []
        placeholders = ",".join("?" for _ in connection_ids)
        values = sorted(connection_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id FROM forwards
                WHERE lab_id = ? AND connection_id IN ({placeholders})
                  AND status IN ('starting', 'active', 'degraded')
                """,
                (self.lab_id, *values),
            ).fetchall()
            connection.execute(
                f"""
                UPDATE forwards
                SET status = 'degraded', health = 'connection_lost', error = ?,
                    listener_state = 'unconfirmed', destination_state = 'unknown',
                    last_checked_at = ?, updated_at = ?
                WHERE lab_id = ? AND connection_id IN ({placeholders})
                  AND status IN ('starting', 'active', 'degraded')
                """,
                (error[:1000], utc_now(), utc_now(), self.lab_id, *values),
            )
        return [int(row["id"]) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> ForwardRead:
        raw = dict(row)
        raw["dependency_ids"] = tuple(json.loads(raw.pop("dependency_ids_json") or "[]"))
        raw["command_argv"] = tuple(json.loads(raw.pop("command_argv_json") or "[]"))
        return ForwardRead.model_validate(raw)
