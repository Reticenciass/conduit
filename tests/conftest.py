from collections.abc import Iterator
from pathlib import Path

import pytest

from ctfws.core.paths import WorkspacePaths
from ctfws.database.db import Database
from ctfws.database.repositories import LabRepository
from ctfws.models.lab import LabCreate
from ctfws.services.workspace import WorkspaceService


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[WorkspaceService]:
    paths = WorkspacePaths.create(tmp_path / "pivot-lab")
    database = Database(paths.database)
    database.initialize()
    LabRepository(database).create(LabCreate(name="Pivot-Lab", platform="test"), paths.root)
    workspace = WorkspaceService(paths)
    yield workspace
    workspace.port_leases.close()
