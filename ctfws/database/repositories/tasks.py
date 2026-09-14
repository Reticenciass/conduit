"""Persistence for durable background tasks."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.events import Event
from ctfws.models.task import TaskCreate, TaskRead, TaskStatus


class TaskRepository:
    """Store task state so a motor restart is visible and recoverable."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def create(self, data: TaskCreate) -> TaskRead:
        now = utc_now()
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO workspace_tasks(
                    lab_id, kind, resource_type, resource_id, status, progress,
                    current_step, total_steps, completed_steps, result_json,
                    idempotency_key, idempotency_hash, requested_by,
                    cancel_requested, cancel_requested_at, cancel_requested_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', 0, NULL, ?, 0, '{}', ?, ?, ?, 0, NULL, NULL, ?, ?)
                """,
                (
                    self.lab_id,
                    data.kind,
                    data.resource_type,
                    data.resource_id,
                    data.total_steps,
                    data.idempotency_key,
                    data.idempotency_hash,
                    data.requested_by,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM workspace_tasks WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        assert row is not None
        return self._to_model(row)

    def request_cancel(
        self, task_id: int, requested_by: str | None = None
    ) -> tuple[TaskRead, Event | None]:
        """Record a cancellation request before attempting cooperative cleanup."""

        terminal_statuses = {
            TaskStatus.SUCCEEDED.value,
            TaskStatus.PARTIAL.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
            TaskStatus.INTERRUPTED.value,
        }
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_tasks WHERE lab_id = ? AND id = ?",
                (self.lab_id, task_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"Tarefa {task_id} não encontrada.")
            if row["status"] in terminal_statuses or row["cancel_requested"]:
                return self._to_model(row), None
            now = utc_now()
            connection.execute(
                "UPDATE workspace_tasks SET cancel_requested = 1, "
                "cancel_requested_at = ?, cancel_requested_by = ?, updated_at = ? "
                "WHERE lab_id = ? AND id = ?",
                (now, requested_by, now, self.lab_id, task_id),
            )
            updated = connection.execute(
                "SELECT * FROM workspace_tasks WHERE lab_id = ? AND id = ?",
                (self.lab_id, task_id),
            ).fetchone()
            assert updated is not None
            task = self._to_model(updated)
            event = Event(
                event_type="TASK_CANCEL_REQUESTED",
                message=f"Task {task.id} cancellation requested",
                entity_type="task",
                entity_id=task.id,
                payload={"kind": task.kind, "status": task.status.value},
            )
            self._insert_event(connection, event)
        return task, event

    def get(self, task_id: int) -> TaskRead | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_tasks WHERE lab_id = ? AND id = ?",
                (self.lab_id, task_id),
            ).fetchone()
        return self._to_model(row) if row else None

    def find_idempotent(self, key: str) -> TaskRead | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_tasks WHERE lab_id = ? AND idempotency_key = ?",
                (self.lab_id, key),
            ).fetchone()
        return self._to_model(row) if row else None

    def list(self, limit: int = 100) -> list[TaskRead]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM workspace_tasks WHERE lab_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (self.lab_id, limit),
            ).fetchall()
        return [self._to_model(row) for row in rows]

    def update(
        self,
        task_id: int,
        *,
        status: TaskStatus,
        progress: int | None = None,
        current_step: str | None = None,
        completed_steps: int | None = None,
        result: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> TaskRead:
        with self.database.connection() as connection:
            return self._update_connection(
                connection,
                task_id,
                status=status,
                progress=progress,
                current_step=current_step,
                completed_steps=completed_steps,
                result=result,
                error_code=error_code,
                error_message=error_message,
            )

    def update_with_event(
        self,
        task_id: int,
        *,
        status: TaskStatus,
        event_type: str | None = None,
        progress: int | None = None,
        current_step: str | None = None,
        completed_steps: int | None = None,
        result: dict[str, object] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> tuple[TaskRead, Event]:
        """Commit a task transition and its timeline event in one transaction."""

        with self.database.connection() as connection:
            task = self._update_connection(
                connection,
                task_id,
                status=status,
                progress=progress,
                current_step=current_step,
                completed_steps=completed_steps,
                result=result,
                error_code=error_code,
                error_message=error_message,
            )
            event = Event(
                event_type=event_type or f"TASK_{task.status.value.upper()}",
                message=f"Task {task.id} {task.status.value}",
                entity_type="task",
                entity_id=task.id,
                payload={
                    "kind": task.kind,
                    "status": task.status.value,
                    "progress": task.progress,
                },
            )
            self._insert_event(connection, event)
        return task, event

    def _update_connection(
        self,
        connection: Any,
        task_id: int,
        *,
        status: TaskStatus,
        progress: int | None,
        current_step: str | None,
        completed_steps: int | None,
        result: dict[str, object] | None,
        error_code: str | None,
        error_message: str | None,
    ) -> TaskRead:
        current = connection.execute(
            "SELECT * FROM workspace_tasks WHERE lab_id = ? AND id = ?",
            (self.lab_id, task_id),
        ).fetchone()
        if current is None:
            raise ValueError(f"Tarefa {task_id} não encontrada.")
        raw = dict(current)
        connection.execute(
            """
            UPDATE workspace_tasks SET status = ?, progress = ?, current_step = ?,
                completed_steps = ?, result_json = ?, error_code = ?, error_message = ?,
                updated_at = ?
            WHERE lab_id = ? AND id = ?
            """,
            (
                status.value,
                raw["progress"] if progress is None else max(0, min(100, progress)),
                current_step if current_step is not None else raw["current_step"],
                raw["completed_steps"] if completed_steps is None else completed_steps,
                (
                    json.dumps(
                        raw["result_json"] if result is None else result,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    if result is not None
                    else raw["result_json"]
                ),
                error_code,
                error_message,
                utc_now(),
                self.lab_id,
                task_id,
            ),
        )
        row = connection.execute(
            "SELECT * FROM workspace_tasks WHERE lab_id = ? AND id = ?",
            (self.lab_id, task_id),
        ).fetchone()
        assert row is not None
        return self._to_model(row)

    def _insert_event(self, connection: Any, event: Event) -> None:
        connection.execute(
            "INSERT INTO events(lab_id, event_type, entity_type, entity_id, message, "
            "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
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

    @staticmethod
    def _to_model(row: sqlite3.Row) -> TaskRead:
        raw = dict(row)
        raw["result"] = json.loads(raw.pop("result_json") or "{}")
        raw["cancel_requested"] = bool(raw.get("cancel_requested", 0))
        return TaskRead.model_validate(raw)
