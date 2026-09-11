"""Host tag repository."""

from __future__ import annotations

from ctfws.core.time import utc_now
from ctfws.database.db import Database


class TagRepository:
    """Manage normalized tags for hosts."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def add(self, host_id: int, tag: str) -> None:
        with self.database.connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO host_tags(lab_id, host_id, tag, created_at) "
                "VALUES (?, ?, ?, ?)",
                (self.lab_id, host_id, tag.strip().lower(), utc_now()),
            )

    def remove(self, host_id: int, tag: str) -> None:
        with self.database.connection() as connection:
            connection.execute(
                "DELETE FROM host_tags WHERE lab_id = ? AND host_id = ? AND tag = ?",
                (self.lab_id, host_id, tag.strip().lower()),
            )

    def list_for_host(self, host_id: int) -> list[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT tag FROM host_tags WHERE lab_id = ? AND host_id = ? ORDER BY tag",
                (self.lab_id, host_id),
            ).fetchall()
        return [str(row["tag"]) for row in rows]

    def hosts_with_tag(self, tag: str) -> set[int]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT host_id FROM host_tags WHERE lab_id = ? AND tag = ?",
                (self.lab_id, tag.strip().lower()),
            ).fetchall()
        return {int(row["host_id"]) for row in rows}
