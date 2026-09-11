"""Access path repository."""

from __future__ import annotations

import json
import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.access import AccessPathCreate, AccessPathRead


class AccessPathRepository:
    """Persist explainable paths to hosts and services."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def upsert(self, data: AccessPathCreate) -> AccessPathRead:
        now = utc_now()
        host_hops_json = json.dumps(list(data.hop_host_ids))
        hops_json = json.dumps(list(data.hop_session_ids))
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM access_paths
                WHERE lab_id = ? AND target_host_id IS ? AND target_address = ?
                    AND network_scope = ? AND target_port IS ?
                    AND hop_host_ids_json = ? AND hop_session_ids_json = ?
                """,
                (
                    self.lab_id,
                    data.target_host_id,
                    data.target_address,
                    data.network_scope,
                    data.target_port,
                    host_hops_json,
                    hops_json,
                ),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO access_paths(
                        lab_id, target_host_id, target_address, network_scope,
                        target_port, service_id,
                        state, confidence, hop_session_ids_json, reason, source,
                        hop_host_ids_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.lab_id,
                        data.target_host_id,
                        data.target_address,
                        data.network_scope,
                        data.target_port,
                        data.service_id,
                        data.state.value,
                        data.confidence,
                        hops_json,
                        data.reason,
                        data.source,
                        host_hops_json,
                        now,
                        now,
                    ),
                )
                path_id = cursor.lastrowid
            else:
                path_id = int(row["id"])
                connection.execute(
                    """
                    UPDATE access_paths
                    SET service_id = ?, network_scope = ?, state = ?, confidence = ?,
                        reason = ?, source = ?, hop_host_ids_json = ?, hop_session_ids_json = ?,
                        updated_at = ?
                    WHERE lab_id = ? AND id = ?
                    """,
                    (
                        data.service_id,
                        data.network_scope,
                        data.state.value,
                        data.confidence,
                        data.reason,
                        data.source,
                        host_hops_json,
                        hops_json,
                        now,
                        self.lab_id,
                        path_id,
                    ),
                )
            persisted = connection.execute(
                "SELECT * FROM access_paths WHERE lab_id = ? AND id = ?",
                (self.lab_id, path_id),
            ).fetchone()
            assert persisted is not None
        return self._to_model(persisted)

    def get(self, path_id: int) -> AccessPathRead | None:
        """Get a path only when it belongs to this workspace."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM access_paths WHERE lab_id = ? AND id = ?",
                (self.lab_id, path_id),
            ).fetchone()
        return self._to_model(row) if row else None

    def mark_verified(self, path_id: int, verification_id: int, verified_at: str) -> AccessPathRead:
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE access_paths
                SET state = 'verified', verification_id = ?, verified_at = ?,
                    updated_at = ?
                WHERE lab_id = ? AND id = ?
                """,
                (verification_id, verified_at, utc_now(), self.lab_id, path_id),
            )
            row = connection.execute(
                "SELECT * FROM access_paths WHERE lab_id = ? AND id = ?",
                (self.lab_id, path_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Caminho {path_id} não encontrado.")
        return self._to_model(row)

    def mark_failed(self, path_id: int, verification_id: int) -> AccessPathRead:
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE access_paths
                SET state = 'failed', verification_id = ?, verified_at = NULL,
                    updated_at = ?
                WHERE lab_id = ? AND id = ?
                """,
                (verification_id, utc_now(), self.lab_id, path_id),
            )
            row = connection.execute(
                "SELECT * FROM access_paths WHERE lab_id = ? AND id = ?",
                (self.lab_id, path_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Caminho {path_id} não encontrado.")
        return self._to_model(row)

    def list(self, target_host_id: int | None = None) -> list[AccessPathRead]:
        query = "SELECT * FROM access_paths WHERE lab_id = ?"
        params: list[object] = [self.lab_id]
        if target_host_id is not None:
            query += " AND target_host_id = ?"
            params.append(target_host_id)
        query += " ORDER BY confidence DESC, target_address, target_port"
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> AccessPathRead:
        raw = dict(row)
        raw["hop_host_ids"] = tuple(json.loads(raw.pop("hop_host_ids_json") or "[]"))
        raw["hop_session_ids"] = tuple(json.loads(raw.pop("hop_session_ids_json") or "[]"))
        return AccessPathRead.model_validate(raw)
