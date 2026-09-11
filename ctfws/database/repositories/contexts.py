"""Repository for explicit SOCKS/routed context plans."""

from __future__ import annotations

import json
import sqlite3
from builtins import list as builtin_list

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.context import ContextStatus, NetworkContextCreate, NetworkContextRead


class ContextRepository:
    """Store typed context configuration and runtime state."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: NetworkContextCreate, command: tuple[str, ...]) -> NetworkContextRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO network_contexts(
                    lab_id, name, transport, connection_id, network_cidrs_json,
                    local_address, local_port, dns_mode, status, command_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
                """,
                (
                    self.lab_id,
                    data.name,
                    data.transport.value,
                    data.connection_id,
                    json.dumps(list(data.network_cidrs)),
                    data.local_address,
                    data.local_port,
                    data.dns_mode,
                    json.dumps(list(command)),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM network_contexts WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def get(self, context_id: int) -> NetworkContextRead | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM network_contexts WHERE lab_id = ? AND id = ?",
                (self.lab_id, context_id),
            ).fetchone()
        return self._to_model(row) if row else None

    def list(self) -> list[NetworkContextRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM network_contexts WHERE lab_id = ? ORDER BY id", (self.lab_id,)
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def update_runtime(
        self,
        context_id: int,
        *,
        status: ContextStatus,
        pid: int | None = None,
        error: str | None = None,
        process_started_at: str | None = None,
        process_executable: str | None = None,
        process_fingerprint: str | None = None,
        clear_process_identity: bool = False,
        engine_id: str | None = None,
        clear_engine_id: bool = False,
        namespace_name: str | None = None,
        clear_namespace: bool = False,
        resource_manifest: tuple[str, ...] | None = None,
        health: str | None = None,
        capabilities: tuple[str, ...] | None = None,
    ) -> NetworkContextRead:
        manifest_json = (
            json.dumps(list(resource_manifest)) if resource_manifest is not None else None
        )
        capabilities_json = (
            json.dumps(list(capabilities), ensure_ascii=False) if capabilities is not None else None
        )
        namespace_sql = "NULL" if clear_namespace else "COALESCE(?, namespace_name)"
        engine_sql = "NULL" if clear_engine_id else "COALESCE(?, engine_id)"
        process_started_sql = (
            "NULL" if clear_process_identity else "COALESCE(?, process_started_at)"
        )
        process_executable_sql = (
            "NULL" if clear_process_identity else "COALESCE(?, process_executable)"
        )
        process_fingerprint_sql = (
            "NULL" if clear_process_identity else "COALESCE(?, process_fingerprint)"
        )
        params: list[object] = [
            status.value,
            pid,
            error,
            health,
            capabilities_json,
        ]
        if not clear_process_identity:
            params.extend([process_started_at, process_executable, process_fingerprint])
        if not clear_engine_id:
            params.append(engine_id)
        if not clear_namespace:
            params.append(namespace_name)
        params.extend([manifest_json, utc_now(), self.lab_id, context_id])
        with self.database.connection() as connection:
            connection.execute(
                f"""
                UPDATE network_contexts
                SET status = ?, pid = ?, error = ?,
                    health = ?,
                    capabilities_json = COALESCE(?, capabilities_json),
                    process_started_at = {process_started_sql},
                    process_executable = {process_executable_sql},
                    process_fingerprint = {process_fingerprint_sql},
                    engine_id = {engine_sql},
                    namespace_name = {namespace_sql},
                    resource_manifest_json = COALESCE(?, resource_manifest_json),
                    updated_at = ?
                WHERE lab_id = ? AND id = ?
                """,
                tuple(params),
            )
            row = connection.execute(
                "SELECT * FROM network_contexts WHERE lab_id = ? AND id = ?",
                (self.lab_id, context_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Contexto {context_id} não encontrado.")
        return self._to_model(row)

    def mark_connection_lost(self, connection_ids: set[int], error: str) -> builtin_list[int]:
        """Degrade active contexts after their SSH transport disappears."""

        if not connection_ids:
            return []
        placeholders = ",".join("?" for _ in connection_ids)
        values = sorted(connection_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id FROM network_contexts
                WHERE lab_id = ? AND connection_id IN ({placeholders})
                  AND status IN ('starting', 'active', 'degraded')
                """,
                (self.lab_id, *values),
            ).fetchall()
            connection.execute(
                f"""
                UPDATE network_contexts
                SET status = 'degraded', health = 'connection_lost',
                    capabilities_json = '[]', error = ?, updated_at = ?
                WHERE lab_id = ? AND connection_id IN ({placeholders})
                  AND status IN ('starting', 'active', 'degraded')
                """,
                (error[:1000], utc_now(), self.lab_id, *values),
            )
        return [int(row["id"]) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> NetworkContextRead:
        raw = dict(row)
        raw["network_cidrs"] = tuple(json.loads(raw.pop("network_cidrs_json") or "[]"))
        raw["command"] = tuple(json.loads(raw.pop("command_json") or "[]"))
        raw["resource_manifest"] = tuple(json.loads(raw.pop("resource_manifest_json") or "[]"))
        raw["capabilities"] = tuple(json.loads(raw.pop("capabilities_json") or "[]"))
        return NetworkContextRead.model_validate(raw)
