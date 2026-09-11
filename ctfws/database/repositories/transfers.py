"""Repository for file transfer audit and integrity state."""

from __future__ import annotations

import sqlite3

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.models.transfer import TransferDirection, TransferRead, TransferStatus


class TransferRepository:
    """Keep a transfer record even when an operation ends partially."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(
        self,
        connection_id: int,
        direction: TransferDirection,
        local_path: str,
        remote_path: str,
        size: int,
        requested_by: str | None = None,
    ) -> TransferRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO transfers(
                    lab_id, connection_id, direction, local_path, remote_path,
                    size, bytes_transferred, status, requested_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 'running', ?, ?, ?)
                """,
                (
                    self.lab_id,
                    connection_id,
                    direction.value,
                    local_path,
                    remote_path,
                    size,
                    requested_by,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM transfers WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def finish(
        self,
        transfer_id: int,
        *,
        status: TransferStatus,
        bytes_transferred: int,
        size: int | None = None,
        local_sha256: str | None,
        remote_sha256: str | None = None,
        integrity_verified: bool = False,
        error: str | None = None,
    ) -> TransferRead:
        with self.database.connection() as connection:
            connection.execute(
                """
                UPDATE transfers
                SET status = ?, bytes_transferred = ?, size = COALESCE(?, size),
                    local_sha256 = ?,
                    remote_sha256 = ?, integrity_verified = ?, error = ?, updated_at = ?
                WHERE lab_id = ? AND id = ?
                """,
                (
                    status.value,
                    bytes_transferred,
                    size,
                    local_sha256,
                    remote_sha256,
                    int(integrity_verified),
                    error,
                    utc_now(),
                    self.lab_id,
                    transfer_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM transfers WHERE lab_id = ? AND id = ?",
                (self.lab_id, transfer_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Transferência {transfer_id} não encontrada.")
        return self._to_model(row)

    def list(self, limit: int = 100) -> list[TransferRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM transfers WHERE lab_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (self.lab_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._to_model(row) for row in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> TransferRead:
        raw = dict(row)
        raw["integrity_verified"] = bool(raw["integrity_verified"])
        return TransferRead.model_validate(raw)
