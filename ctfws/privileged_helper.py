"""Entry point for the optional restricted Linux namespace helper."""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ctfws.core.paths import WorkspacePaths
from ctfws.services.routed_helper import RoutedNamespaceHelper
from ctfws.services.routed_socket import RoutedNamespaceSocketServer


@dataclass(slots=True)
class _LabIdentity:
    id: int


@dataclass(slots=True)
class _HelperWorkspace:
    lab: _LabIdentity
    paths: WorkspacePaths


def _read_workspace_identity(paths: WorkspacePaths) -> _HelperWorkspace:
    """Read only the identity needed by the privileged helper.

    The helper is deliberately not an application service and must not run
    SQLite migrations or change WAL state while the unprivileged motor owns
    the workspace.  Keeping this read-only also makes ``ProtectSystem=strict``
    meaningful for the systemd unit.
    """

    # ``immutable`` prevents SQLite from trying to create a WAL shared-memory
    # file while the systemd sandbox keeps the workspace read-only.
    database_uri = f"file:{paths.database}?mode=ro&immutable=1"
    with sqlite3.connect(database_uri, uri=True) as connection:
        row = connection.execute("SELECT id FROM labs LIMIT 1").fetchone()
    if row is None or not isinstance(row[0], int):
        raise RuntimeError("O workspace não possui uma identidade válida.")
    return _HelperWorkspace(lab=_LabIdentity(id=row[0]), paths=paths)


def main(argv: list[str] | None = None) -> int:
    """Run the root-owned helper; the motor connects through its Unix socket."""

    parser = argparse.ArgumentParser(
        description="CTF Workspace typed namespace helper (Linux, optional and privileged)."
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--socket", dest="socket_path", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.name == "nt":
        parser.error("O helper de namespace exige Linux.")

    paths = WorkspacePaths.from_value(args.workspace)
    workspace = _read_workspace_identity(paths)
    # This instance executes locally as the helper; the motor-side instance is
    # socket-backed. It intentionally has no write-capable application DB.
    helper = RoutedNamespaceHelper(workspace)
    server = RoutedNamespaceSocketServer(helper, args.socket_path)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: server.close())
    signal.signal(signal.SIGINT, lambda _signum, _frame: server.close())
    try:
        server.serve_forever()
    finally:
        server.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the service on Linux.
    raise SystemExit(main())
