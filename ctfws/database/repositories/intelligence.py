"""Repositories for aliases and inferred host relationships."""

from __future__ import annotations

import json
from typing import Any

from ctfws.core.time import utc_now
from ctfws.database.db import Database


class IntelligenceRepository:
    """Persist explainable identity and relationship projections."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def add_alias(self, host_id: int, alias: str, source: str) -> None:
        with self.database.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO host_aliases(lab_id, host_id, alias, source, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.lab_id, host_id, alias, source, utc_now()),
            )

    def aliases(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM host_aliases WHERE lab_id = ? ORDER BY alias", (self.lab_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_relationship(
        self,
        source_host_id: int,
        target_host_id: int,
        relation_type: str,
        label: str,
        confidence: int,
        evidence: dict[str, Any],
    ) -> None:
        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO relationships(
                    lab_id, source_host_id, target_host_id, relation_type, label,
                    evidence_json, confidence, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.lab_id,
                    source_host_id,
                    target_host_id,
                    relation_type,
                    label,
                    json.dumps(evidence, sort_keys=True),
                    confidence,
                    utc_now(),
                ),
            )

    def relationships(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM relationships WHERE lab_id = ? ORDER BY id", (self.lab_id,)
            ).fetchall()
        return [dict(row) for row in rows]
