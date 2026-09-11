"""Persistence for non-secret connection profiles."""

from __future__ import annotations

import json
import sqlite3
from builtins import list as builtin_list

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.connection import (
    ConnectionProfileCreate,
    ConnectionProfileRead,
    ConnectionProfileUpdate,
)


class ConnectionRepository:
    """Store connection metadata without passwords or private key contents."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: ConnectionProfileCreate) -> ConnectionProfileRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO connection_profiles(
                    lab_id, name, transport, host, port, user, identity_file,
                    known_hosts_file, auth_ref, host_id, jump_profile_ids_json, auth_method,
                    state, last_checked_at, last_error, revision, tags_json, metadata_json,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'disconnected', NULL, NULL, 1, ?, ?, ?, ?
                )
                """,
                (
                    self.lab_id,
                    data.name,
                    data.transport.value,
                    data.host,
                    data.port,
                    data.user,
                    data.identity_file,
                    data.known_hosts_file,
                    data.auth_ref,
                    data.host_id,
                    json.dumps(list(data.jump_profile_ids)),
                    data.auth_method,
                    json.dumps(list(data.tags), ensure_ascii=False),
                    json.dumps(data.metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM connection_profiles WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def list(self) -> list[ConnectionProfileRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM connection_profiles WHERE lab_id = ? ORDER BY name, id",
                (self.lab_id,),
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def get(self, profile_id: int) -> ConnectionProfileRead | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM connection_profiles WHERE lab_id = ? AND id = ?",
                (self.lab_id, profile_id),
            ).fetchone()
        return self._to_model(row) if row else None

    def update_runtime(
        self,
        profile_id: int,
        *,
        state: str,
        last_error: str | None = None,
        generation: int | None = None,
        capabilities: tuple[str, ...] | None = None,
    ) -> ConnectionProfileRead:
        """Store the last known transport state without changing profile data."""

        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE connection_profiles
                SET state = ?, last_checked_at = ?, last_error = ?,
                    generation = COALESCE(?, generation),
                    capabilities_json = COALESCE(?, capabilities_json),
                    updated_at = ?
                WHERE lab_id = ? AND id = ?
                """,
                (
                    state,
                    utc_now(),
                    last_error,
                    generation,
                    (
                        json.dumps(list(capabilities), ensure_ascii=False)
                        if capabilities is not None
                        else None
                    ),
                    utc_now(),
                    self.lab_id,
                    profile_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM connection_profiles WHERE lab_id = ? AND id = ?",
                (self.lab_id, profile_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Conexão {profile_id} não encontrada.")
        return self._to_model(row)

    def mark_degraded(self, profile_ids: set[int], error: str) -> builtin_list[int]:
        """Mark currently usable profiles degraded after a transport dependency loss."""

        if not profile_ids:
            return []
        placeholders = ",".join("?" for _ in profile_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id FROM connection_profiles
                WHERE lab_id = ? AND id IN ({placeholders})
                  AND state IN ('ready', 'connecting')
                """,
                (self.lab_id, *sorted(profile_ids)),
            ).fetchall()
            connection.execute(
                f"""
                UPDATE connection_profiles
                SET state = ?, last_error = ?, capabilities_json = '[]',
                    last_checked_at = ?, updated_at = ?
                WHERE lab_id = ? AND id IN ({placeholders})
                  AND state IN ('ready', 'connecting')
                """,
                (
                    "degraded",
                    error[:1000],
                    utc_now(),
                    utc_now(),
                    self.lab_id,
                    *sorted(profile_ids),
                ),
            )
        return [int(row["id"]) for row in rows]

    def bind_host(self, profile_id: int, host_id: int) -> ConnectionProfileRead:
        """Associate a profile with a host only inside this workspace."""

        with self.database.connection() as connection:
            connection.execute(
                "UPDATE connection_profiles SET host_id = ?, revision = revision + 1, "
                "updated_at = ? WHERE lab_id = ? AND id = ?",
                (host_id, utc_now(), self.lab_id, profile_id),
            )
            row = connection.execute(
                "SELECT * FROM connection_profiles WHERE lab_id = ? AND id = ?",
                (self.lab_id, profile_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Conexão {profile_id} não encontrada.")
        return self._to_model(row)

    def update(
        self,
        profile_id: int,
        data: ConnectionProfileUpdate,
        *,
        expected_revision: int,
    ) -> ConnectionProfileRead:
        """Update a profile only when the caller still owns its revision."""

        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE connection_profiles SET
                    name = ?, transport = 'ssh', host = ?, port = ?, user = ?,
                    identity_file = ?, known_hosts_file = ?, auth_ref = ?, host_id = ?,
                    jump_profile_ids_json = ?, auth_method = ?, tags_json = ?,
                    metadata_json = ?, revision = revision + 1, updated_at = ?
                WHERE lab_id = ? AND id = ? AND revision = ?
                """,
                (
                    data.name,
                    data.host,
                    data.port,
                    data.user,
                    data.identity_file,
                    data.known_hosts_file,
                    data.auth_ref,
                    data.host_id,
                    json.dumps(list(data.jump_profile_ids)),
                    data.auth_method,
                    json.dumps(list(data.tags), ensure_ascii=False),
                    json.dumps(data.metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    self.lab_id,
                    profile_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("revision_conflict")
            row = connection.execute(
                "SELECT * FROM connection_profiles WHERE lab_id = ? AND id = ?",
                (self.lab_id, profile_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Conexão {profile_id} não encontrada.")
        return self._to_model(row)

    @staticmethod
    def _to_model(row: sqlite3.Row) -> ConnectionProfileRead:
        raw = dict(row)
        raw["tags"] = tuple(json.loads(raw.pop("tags_json") or "[]"))
        raw["jump_profile_ids"] = tuple(json.loads(raw.pop("jump_profile_ids_json") or "[]"))
        raw["metadata"] = json.loads(raw.pop("metadata_json") or "{}")
        raw["capabilities"] = tuple(json.loads(raw.pop("capabilities_json") or "[]"))
        return ConnectionProfileRead.model_validate(raw)
