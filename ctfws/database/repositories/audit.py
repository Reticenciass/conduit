"""Repository for immutable audit records."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.audit import AuditCreate, AuditRead


class AuditRepository:
    """Persist actor/resource outcomes scoped to one workspace."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: AuditCreate) -> AuditRead:
        now = utc_now()
        safe_details = self._sanitize(data.details)
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO audit_log(
                    lab_id, actor, action, resource_type, resource_id, result,
                    correlation_id, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.lab_id,
                    data.actor,
                    data.action,
                    data.resource_type,
                    data.resource_id,
                    data.result,
                    data.correlation_id,
                    json.dumps(safe_details, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM audit_log WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def list(self, limit: int = 100) -> list[AuditRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_log WHERE lab_id = ? ORDER BY id DESC LIMIT ?",
                (self.lab_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> AuditRead:
        raw = dict(row)
        raw["details"] = json.loads(raw.pop("details_json") or "{}")
        return AuditRead.model_validate(raw)

    @classmethod
    def _sanitize(cls, value: Any) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                normalized = str(key)
                if any(
                    marker in normalized.lower()
                    for marker in ("password", "secret", "token", "private_key")
                ):
                    result[normalized] = "[redacted]"
                else:
                    result[normalized] = cls._sanitize(item)
            return result
        if isinstance(value, (list, tuple)):
            return [cls._sanitize(item) for item in value]
        return value
