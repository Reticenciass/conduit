"""Bounded background task execution with durable state."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock

from ctfws.core.errors import EntityNotFoundError, IdempotencyConflictError
from ctfws.models.task import TaskCreate, TaskRead, TaskStatus
from ctfws.services.workspace import WorkspaceService


@dataclass(frozen=True, slots=True)
class TaskProgress:
    """Progress callback payload for one task step."""

    completed_steps: int
    total_steps: int
    current_step: str


TaskWork = Callable[[Callable[[TaskProgress], None]], dict[str, object]]
AsyncTaskWork = Callable[[Callable[[TaskProgress], None]], Awaitable[dict[str, object]]]


class TaskManager:
    """Run bounded operations while keeping their state in the workspace."""

    def __init__(self, workspace: WorkspaceService, max_workers: int = 4) -> None:
        self.workspace = workspace
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="ctfws-task"
        )
        # Sync and async work share one capacity gate. The executor size alone
        # cannot enforce the limit because AsyncSSH tasks run on the event loop.
        self._heavy_slots = threading.BoundedSemaphore(max_workers)
        self._futures: dict[int, Future[None]] = {}
        self._async_tasks: dict[int, asyncio.Task[None]] = {}
        self._cancelled: set[int] = set()
        self._lock = Lock()
        self.logger = logging.getLogger("ctfws.tasks")

    def recover(self) -> None:
        """Mark work from a previous motor as interrupted after lock acquisition."""

        for task in self.workspace.tasks.list():
            if task.status in {
                TaskStatus.QUEUED,
                TaskStatus.RUNNING,
                TaskStatus.WAITING_INPUT,
            }:
                self._update_with_event(
                    task.id,
                    status=TaskStatus.INTERRUPTED,
                    error_code="motor_restarted",
                    error_message="A tarefa foi interrompida quando o motor reiniciou.",
                )

    def close(self) -> None:
        """Stop accepting task work without killing running operations."""

        self._executor.shutdown(wait=False, cancel_futures=True)

    def list(self, limit: int = 100) -> list[TaskRead]:
        return self.workspace.tasks.list(limit)

    def get(self, task_id: int) -> TaskRead:
        task = self.workspace.tasks.get(task_id)
        if task is None:
            raise EntityNotFoundError(f"Tarefa {task_id} não encontrada.")
        return task

    def submit(self, data: TaskCreate, work: TaskWork) -> TaskRead:
        if data.idempotency_key:
            existing = self.workspace.tasks.find_idempotent(data.idempotency_key)
            if existing is not None:
                self._validate_idempotent_replay(existing, data)
                return existing
        try:
            task = self.workspace.tasks.create(data)
        except sqlite3.IntegrityError:
            if data.idempotency_key:
                existing = self.workspace.tasks.find_idempotent(data.idempotency_key)
                if existing is not None:
                    self._validate_idempotent_replay(existing, data)
                    return existing
            raise
        future = self._executor.submit(self._run, task.id, work)
        with self._lock:
            self._futures[task.id] = future
        return task

    def submit_async(self, data: TaskCreate, work: AsyncTaskWork) -> TaskRead:
        """Schedule work on the running web loop while preserving task state."""

        if data.idempotency_key:
            existing = self.workspace.tasks.find_idempotent(data.idempotency_key)
            if existing is not None:
                self._validate_idempotent_replay(existing, data)
                return existing
        try:
            task = self.workspace.tasks.create(data)
        except sqlite3.IntegrityError:
            if data.idempotency_key:
                existing = self.workspace.tasks.find_idempotent(data.idempotency_key)
                if existing is not None:
                    self._validate_idempotent_replay(existing, data)
                    return existing
            raise
        async_task = asyncio.create_task(self._run_async(task.id, work))
        with self._lock:
            self._async_tasks[task.id] = async_task
        return task

    @staticmethod
    def _validate_idempotent_replay(existing: TaskRead, requested: TaskCreate) -> None:
        """Reject reuse of one idempotency key for different request content."""

        if (
            existing.idempotency_hash is not None
            and requested.idempotency_hash is not None
            and existing.idempotency_hash != requested.idempotency_hash
        ):
            raise IdempotencyConflictError(
                "A chave de idempotência já foi usada por uma requisição diferente."
            )
        if existing.idempotency_hash is None and requested.idempotency_hash is not None:
            raise IdempotencyConflictError(
                "A chave de idempotência pertence a uma tarefa legada; use uma nova chave."
            )

    def retry(self, task_id: int, data: TaskCreate, work: TaskWork) -> TaskRead:
        """Create a new explicit attempt; the original task remains immutable in history."""

        original = self.get(task_id)
        if original.status in {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING_INPUT}:
            raise ValueError("A tarefa ainda está em execução e não pode ser repetida.")
        return self.submit(data, work)

    def cancel(self, task_id: int, requested_by: str | None = None) -> TaskRead:
        task = self.get(task_id)
        if task.status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.PARTIAL,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.INTERRUPTED,
        }:
            return task
        requested, event = self.workspace.tasks.request_cancel(task_id, requested_by)
        if event is not None:
            self.workspace.bus.publish(event)
        with self._lock:
            self._cancelled.add(task_id)
            future = self._futures.get(task_id)
            if future is not None:
                cancelled_before_start = future.cancel()
            else:
                cancelled_before_start = False
            async_task = self._async_tasks.get(task_id)
            if async_task is not None:
                async_task.cancel()
        if task.status == TaskStatus.QUEUED and cancelled_before_start:
            return self._mark_cancelled(task_id)
        if task.status == TaskStatus.QUEUED and async_task is None and future is None:
            return self._mark_cancelled(task_id)
        # A running synchronous worker cannot be force-killed safely.  Keep it
        # visible as running until its cooperative boundary completes, while
        # exposing cancel_requested in the durable task record.
        return requested

    def is_cancelled(self, task_id: int) -> bool:
        with self._lock:
            return task_id in self._cancelled

    def _run(self, task_id: int, work: TaskWork) -> None:
        if self.is_cancelled(task_id):
            self._mark_cancelled(task_id)
            return
        self._heavy_slots.acquire()
        try:
            if self.is_cancelled(task_id):
                self._mark_cancelled(task_id)
                return
            self._update_with_event(task_id, status=TaskStatus.RUNNING, current_step="iniciando")
            result = work(self._progress_callback(task_id))
            if self.is_cancelled(task_id):
                self._mark_cancelled(task_id)
                return
            final_status = (
                TaskStatus.PARTIAL
                if isinstance(result.get("failed_steps"), list) and bool(result.get("failed_steps"))
                else TaskStatus.SUCCEEDED
            )
            self._update_with_event(
                task_id,
                status=final_status,
                progress=100,
                current_step="concluído",
                result=result,
            )
        except Exception as error:
            if self.is_cancelled(task_id):
                self._mark_cancelled(task_id)
                return
            self.logger.exception("task=%s failed", task_id)
            self._update_with_event(
                task_id,
                status=TaskStatus.FAILED,
                error_code=type(error).__name__.lower(),
                error_message=str(error),
            )
        finally:
            self._heavy_slots.release()
            with self._lock:
                self._futures.pop(task_id, None)

    async def _run_async(self, task_id: int, work: AsyncTaskWork) -> None:
        acquire_task = asyncio.create_task(asyncio.to_thread(self._heavy_slots.acquire))
        acquired = False
        try:
            # Shield the blocking acquisition so cancellation cannot leave a
            # semaphore token held by the worker thread. If cancellation wins
            # while queued, the handler below waits for and releases it.
            await asyncio.shield(acquire_task)
            acquired = True
            if self.is_cancelled(task_id):
                self._mark_cancelled(task_id)
                return
            self._update_with_event(task_id, status=TaskStatus.RUNNING, current_step="iniciando")
            result = await work(self._progress_callback(task_id))
            if self.is_cancelled(task_id):
                self._mark_cancelled(task_id)
                return
            final_status = (
                TaskStatus.PARTIAL
                if isinstance(result.get("failed_steps"), list) and bool(result.get("failed_steps"))
                else TaskStatus.SUCCEEDED
            )
            self._update_with_event(
                task_id,
                status=final_status,
                progress=100,
                current_step="concluído",
                result=result,
            )
        except asyncio.CancelledError:
            if not acquire_task.done():
                await acquire_task
                acquired = True
            elif acquire_task.done() and not acquire_task.cancelled():
                acquired = bool(acquire_task.result())
            if self.is_cancelled(task_id):
                self._mark_cancelled(task_id)
            else:
                self._update_with_event(
                    task_id,
                    status=TaskStatus.INTERRUPTED,
                    error_code="async_task_cancelled",
                    error_message="A tarefa assíncrona foi interrompida.",
                )
            raise
        except Exception as error:
            if self.is_cancelled(task_id):
                self._mark_cancelled(task_id)
                return
            self.logger.exception("async task=%s failed", task_id)
            self._update_with_event(
                task_id,
                status=TaskStatus.FAILED,
                error_code=type(error).__name__.lower(),
                error_message=str(error),
            )
        finally:
            if acquired:
                self._heavy_slots.release()
            with self._lock:
                self._async_tasks.pop(task_id, None)

    def _progress_callback(self, task_id: int) -> Callable[[TaskProgress], None]:
        def update(progress: TaskProgress) -> None:
            if self.is_cancelled(task_id):
                return
            percentage = (
                round(progress.completed_steps * 100 / progress.total_steps)
                if progress.total_steps
                else 0
            )
            self._update_with_event(
                task_id,
                status=TaskStatus.RUNNING,
                event_type="TASK_PROGRESS",
                progress=percentage,
                current_step=progress.current_step,
                completed_steps=progress.completed_steps,
            )

        return update

    def _update_with_event(
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
    ) -> TaskRead:
        task, event = self.workspace.tasks.update_with_event(
            task_id,
            status=status,
            event_type=event_type,
            progress=progress,
            current_step=current_step,
            completed_steps=completed_steps,
            result=result,
            error_code=error_code,
            error_message=error_message,
        )
        # The repository commits both rows before the bus is notified.
        self.workspace.bus.publish(event)
        return task

    def _mark_cancelled(self, task_id: int) -> TaskRead:
        """Transition a task to cancelled once, even when worker and API race."""

        current = self.get(task_id)
        if current.status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.PARTIAL,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.INTERRUPTED,
        }:
            return current
        return self._update_with_event(
            task_id,
            status=TaskStatus.CANCELLED,
            error_code="cancelled_by_operator",
            error_message="Cancelada pelo operador.",
        )
