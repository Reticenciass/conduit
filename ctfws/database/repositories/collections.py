"""Repository for inspection collection runs."""

from __future__ import annotations

import json
import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.collection import CollectionRunRead, CollectionStatus


class CollectionRepository:
    """Persist partial collection progress without replacing old evidence."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(
        self, host_id: int | None, total_steps: int, snapshot_id: int | None = None
    ) -> CollectionRunRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO collection_runs(
                    lab_id, host_id, snapshot_id, status, parser_version, total_steps,
                    completed_steps, result_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', 'v1', ?, 0, '{}', ?, ?)
                """,
                (self.lab_id, host_id, snapshot_id, total_steps, now, now),
            )
            row = connection.execute(
                "SELECT * FROM collection_runs WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def update(
        self,
        run_id: int,
        *,
        status: CollectionStatus,
        completed_steps: int,
        result: dict[str, object] | None = None,
    ) -> CollectionRunRead:
        with self.database.connection() as connection:
            current = connection.execute(
                "SELECT * FROM collection_runs WHERE lab_id = ? AND id = ?",
                (self.lab_id, run_id),
            ).fetchone()
            if current is None:
                raise ValueError(f"Coleta {run_id} não encontrada.")
            raw = dict(current)
            connection.execute(
                """
                UPDATE collection_runs
                SET status = ?, completed_steps = ?, result_json = ?, updated_at = ?
                WHERE lab_id = ? AND id = ?
                """,
                (
                    status.value,
                    completed_steps,
                    (
                        raw["result_json"]
                        if result is None
                        else json.dumps(result, ensure_ascii=False, sort_keys=True)
                    ),
                    utc_now(),
                    self.lab_id,
                    run_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM collection_runs WHERE lab_id = ? AND id = ?",
                (self.lab_id, run_id),
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def list(self, host_id: int | None = None, limit: int = 50) -> list[CollectionRunRead]:
        query = "SELECT * FROM collection_runs WHERE lab_id = ?"
        params: list[object] = [self.lab_id]
        if host_id is not None:
            query += " AND host_id = ?"
            params.append(host_id)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(limit, 200)))
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> CollectionRunRead:
        raw = dict(row)
        raw["result"] = json.loads(raw.pop("result_json") or "{}")
        return CollectionRunRead.model_validate(raw)
