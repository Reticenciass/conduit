"""Filesystem layout and workspace discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ctfws.core.errors import InvalidWorkspaceError, WorkspaceNotFoundError

WORKSPACE_DIRS = ("notes", "evidence", "reports", "loot", "logs")


def slugify(value: str) -> str:
    """Return a filesystem-friendly slug while preserving a readable name."""

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("O nome do laboratório precisa conter letras ou números.")
    return slug


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    """Paths belonging to one isolated lab workspace."""

    root: Path

    @property
    def database(self) -> Path:
        return self.root / "workspace.db"

    @property
    def log_file(self) -> Path:
        return self.root / "logs" / "ctfws.log"

    def ensure_layout(self) -> None:
        """Create the non-database workspace directories."""

        self.root.mkdir(parents=True, exist_ok=True)
        for directory in WORKSPACE_DIRS:
            (self.root / directory).mkdir(exist_ok=True)

    @classmethod
    def from_value(cls, value: Path | None) -> WorkspacePaths:
        """Resolve an explicit path or discover `workspace.db` in the current directory."""

        candidate = value.expanduser() if value is not None else Path.cwd()
        candidate = candidate.resolve()
        if candidate.is_file():
            if candidate.name != "workspace.db":
                raise InvalidWorkspaceError("O arquivo do workspace deve se chamar workspace.db.")
            root = candidate.parent
        else:
            root = candidate
        paths = cls(root)
        if not paths.database.is_file():
            raise WorkspaceNotFoundError(
                f"Nenhum workspace encontrado em {paths.database}. " "Use 'ctfws lab create'."
            )
        return paths

    @classmethod
    def create(cls, root: Path) -> WorkspacePaths:
        """Create the layout for a new lab and return its paths."""

        paths = cls(root.expanduser().resolve())
        paths.ensure_layout()
        return paths


def discover_labs(base_dir: Path) -> list[WorkspacePaths]:
    """Find direct child directories containing a workspace database."""

    base = base_dir.expanduser().resolve()
    if not base.exists():
        return []
    return [
        WorkspacePaths(child)
        for child in sorted(base.iterdir(), key=lambda item: item.name.lower())
        if child.is_dir() and (child / "workspace.db").is_file()
    ]
