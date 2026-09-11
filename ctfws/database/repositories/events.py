"""Event timeline repository."""

from __future__ import annotations

import json
import sqlite3
from builtins import list as builtin_list
from typing import Any

from ctfws.database.db import Database
from ctfws.events.bus import Event


class EventRepository:
    """Persist and query domain events."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def append(self, event: Event) -> int:
        with self.database.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO events(lab_id, event_type, entity_type, entity_id, message, "
                "payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.lab_id,
                    event.event_type,
                    event.entity_type,
                    event.entity_id,
                    event.message,
                    json.dumps(dict(event.payload), sort_keys=True),
                    event.created_at,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite não retornou o ID do evento inserido.")
            return cursor.lastrowid

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE lab_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (self.lab_id, limit),
            ).fetchall()
        return [self._to_dict(row) for row in rows]

    def list_after(self, event_id: int, limit: int = 100) -> builtin_list[dict[str, Any]]:
        """Read events after a cursor for SSE reconnection."""

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE lab_id = ? AND id > ?
                ORDER BY id LIMIT ?
                """,
                (self.lab_id, event_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._to_dict(row) for row in rows]

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result
