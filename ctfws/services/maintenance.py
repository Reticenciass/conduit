"""Explicit workspace quota and retention operations."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from ctfws.core.limits import max_workspace_bytes, retention_days
from ctfws.services.workspace import WorkspaceService


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    """Storage state and the files eligible for an explicit cleanup."""

    used_bytes: int
    quota_bytes: int | None
    retention_days: int | None
    removable: tuple[str, ...]
    removable_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "used_bytes": self.used_bytes,
            "quota_bytes": self.quota_bytes,
            "retention_days": self.retention_days,
            "removable": list(self.removable),
            "removable_bytes": self.removable_bytes,
        }


class WorkspaceMaintenanceService:
    """Never delete evidence automatically; clean only typed safe candidates."""

    _TEMPORARY = re.compile(r"^\..+\.ctfws-[0-9a-f]{16,}\.part$")

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace

    def inspect(self) -> MaintenanceReport:
        quota = max_workspace_bytes()
        days = retention_days()
        candidates: list[tuple[str, int]] = []
        cutoff = time.time() - days * 86400 if days is not None else None
        used = 0
        root = self.workspace.paths.root.resolve()
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                relative = path.resolve().relative_to(root)
                size = path.stat().st_size
                used += size
                if cutoff is not None and self._is_removable(relative, path):
                    if path.stat().st_mtime <= cutoff:
                        candidates.append((relative.as_posix(), size))
            except (OSError, ValueError):
                continue
        return MaintenanceReport(
            used,
            quota,
            days,
            tuple(item[0] for item in candidates),
            sum(item[1] for item in candidates),
        )

    def prune(self) -> MaintenanceReport:
        """Remove only expired logs and generated ``.part`` files."""

        report = self.inspect()
        root = self.workspace.paths.root.resolve()
        for relative in report.removable:
            target = (root / relative).resolve()
            if root not in target.parents or target.is_symlink() or not target.is_file():
                continue
            if not self._is_removable(Path(relative), target):
                continue
            try:
                target.unlink()
            except OSError:
                continue
        return self.inspect()

    @staticmethod
    def _is_removable(relative: Path, path: Path) -> bool:
        return (
            relative.parts[:1] == ("logs",)
            or WorkspaceMaintenanceService._TEMPORARY.fullmatch(path.name) is not None
        )


def workspace_usage(root: Path) -> int:
    """Count regular workspace files without following symbolic links."""

    total = 0
    resolved_root = root.resolve()
    for path in resolved_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if resolved_root in path.resolve().parents:
                total += path.stat().st_size
        except OSError:
            continue
    return total
