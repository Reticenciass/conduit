"""Catalog local tools by immutable hash without executing them."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ctfws.core.errors import EntityNotFoundError
from ctfws.core.limits import max_file_bytes
from ctfws.models.tool import ToolCreate, ToolRead
from ctfws.services.workspace import WorkspaceService


class ToolCatalogService:
    """Register binaries/scripts for explicit later transfer."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace

    def register(self, data: ToolCreate) -> ToolRead:
        path = Path(data.path).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Ferramenta não encontrada: {path}")
        digest = hashlib.sha256()
        size = 0
        limit = max_file_bytes()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ValueError("A ferramenta excede o limite configurado de arquivo.")
                digest.update(chunk)
        return self.workspace.tools.create(
            data.model_copy(update={"path": str(path)}), size, digest.hexdigest()
        )

    def get(self, tool_id: int) -> ToolRead:
        """Resolve a catalog entry within the active workspace."""

        tool = self.workspace.tools.get(tool_id)
        if tool is None:
            raise EntityNotFoundError(f"Ferramenta {tool_id} não encontrada neste workspace.")
        return tool
