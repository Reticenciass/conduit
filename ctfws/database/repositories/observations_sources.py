"""Observation provenance repository."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.observation import (
    ObservationSourceCreate,
    ObservationSourceKind,
    ObservationSourceRead,
)


class ObservationSourceRepository:
    """Persist the origin and content identity of imported observations."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: ObservationSourceCreate) -> ObservationSourceRead:
        collected_at = data.collected_at or utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO observation_sources(
                    lab_id, host_id, snapshot_id, kind, command, origin,
                    content_hash, metadata_json, collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.lab_id,
                    data.host_id,
                    data.snapshot_id,
                    data.kind.value,
                    data.command,
                    data.origin,
                    data.content_hash,
                    json.dumps(data.metadata, ensure_ascii=False, sort_keys=True),
                    collected_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM observation_sources WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def create_from_text(
        self,
        *,
        host_id: int | None,
        text: str,
        command: str | None,
        origin: str | None = None,
        snapshot_id: int | None = None,
        kind: ObservationSourceKind = ObservationSourceKind.MANUAL_IMPORT,
        metadata: dict[str, object] | None = None,
    ) -> ObservationSourceRead:
        """Create a source using a stable SHA-256 content identity."""

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self.create(
            ObservationSourceCreate(
                host_id=host_id,
                snapshot_id=snapshot_id,
                kind=kind,
                command=command,
                origin=origin,
                content_hash=digest,
                metadata=metadata or {},
            )
        )

    def list(self, host_id: int | None = None) -> list[ObservationSourceRead]:
        query = "SELECT * FROM observation_sources WHERE lab_id = ?"
        params: tuple[object, ...] = (self.lab_id,)
        if host_id is not None:
            query += " AND host_id = ?"
            params += (host_id,)
        query += " ORDER BY collected_at DESC, id DESC"
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> ObservationSourceRead:
        raw = dict(row)
        raw["kind"] = raw.pop("kind")
        raw["metadata"] = json.loads(raw.pop("metadata_json") or "{}")
        return ObservationSourceRead.model_validate(raw)
