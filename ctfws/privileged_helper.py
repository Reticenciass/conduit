"""Entry point for the optional restricted Linux namespace helper."""

from __future__ import annotations

import argparse
import os
import signal
from pathlib import Path

from ctfws.core.paths import WorkspacePaths
from ctfws.services.routed_helper import RoutedNamespaceHelper
from ctfws.services.routed_socket import RoutedNamespaceSocketServer
from ctfws.services.workspace import WorkspaceService


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

    workspace = WorkspaceService(WorkspacePaths.from_value(args.workspace))
    # This instance executes locally as the helper; the motor-side instance is socket-backed.
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
