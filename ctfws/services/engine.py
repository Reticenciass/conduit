"""Single-owner lock for the resources managed by one workspace motor."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ctfws.core.process import is_process_alive


class EngineLock:
    """Claim a workspace with a recoverable, stale-PID-aware lock file."""

    def __init__(self, path: Path, owner_id: str | None = None) -> None:
        self.path = path
        self.owner_id = owner_id
        self._owner: dict[str, Any] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        owner: dict[str, Any] = {"pid": os.getpid()}
        if self.owner_id:
            owner["engine_id"] = self.owner_id
        payload = json.dumps(owner, sort_keys=True)
        for _attempt in range(2):
            try:
                with self.path.open("x", encoding="utf-8") as handle:
                    handle.write(payload)
                self._owner = owner
                return
            except FileExistsError:
                current = self.read_owner(self.path)
                pid = int(current.get("pid", 0)) if current else 0
                if pid > 0 and is_process_alive(pid):
                    raise RuntimeError(
                        f"Este workspace já possui um motor ativo (PID {pid})."
                    ) from None
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    continue
        raise RuntimeError("Não foi possível obter o lock do motor.")

    def release(self) -> None:
        if self._owner is None:
            return
        current = self.read_owner(self.path)
        if current.get("pid") == self._owner["pid"]:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._owner = None

    @staticmethod
    def read_owner(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
