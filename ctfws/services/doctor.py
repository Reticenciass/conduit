"""Workspace diagnostics and operator-facing health checks."""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ctfws.core.limits import max_file_bytes, max_workspace_bytes, retention_days
from ctfws.core.paths import WORKSPACE_DIRS, WorkspacePaths
from ctfws.core.process import process_identity
from ctfws.database.db import SCHEMA_VERSION, Database
from ctfws.pivot.manifest import ligolo_manifest_status
from ctfws.services.engine import EngineLock
from ctfws.services.maintenance import workspace_usage


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One diagnostic result."""

    name: str
    ok: bool
    detail: str
    optional: bool = False


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Aggregate diagnostic result."""

    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok or check.optional for check in self.checks)


class WorkspaceDoctor:
    """Inspect local workspace integrity without probing external systems."""

    def run(self, paths: WorkspacePaths) -> DoctorReport:
        checks: list[DoctorCheck] = []
        checks.append(
            DoctorCheck("workspace", paths.root.is_dir(), f"root={paths.root}", optional=False)
        )
        checks.append(
            DoctorCheck(
                "database-file",
                paths.database.is_file(),
                str(paths.database),
                optional=False,
            )
        )

        if paths.database.is_file():
            try:
                Database(paths.database).initialize()
                checks.append(
                    DoctorCheck(
                        "database-migration",
                        True,
                        f"migrações aplicadas até o schema {SCHEMA_VERSION}",
                    )
                )
            except Exception as error:
                checks.append(DoctorCheck("database-migration", False, str(error)))
            checks.extend(self._database_checks(paths.database))
        else:
            checks.append(DoctorCheck("database-integrity", False, "arquivo ausente"))

        for directory in WORKSPACE_DIRS:
            path = paths.root / directory
            checks.append(DoctorCheck(f"directory:{directory}", path.is_dir(), str(path)))

        for executable in (
            "ssh",
            "tmux",
            "chisel",
            "ligolo-agent",
            "ligolo-proxy",
            "dot",
        ):
            found = shutil.which(executable)
            checks.append(
                DoctorCheck(
                    f"tool:{executable}",
                    found is not None,
                    found or "não encontrado",
                    optional=True,
                )
            )

        ligolo = ligolo_manifest_status()
        checks.append(
            DoctorCheck(
                "tool:ligolo-contract",
                ligolo.valid,
                ligolo.reason,
                optional=True,
            )
        )

        for package in ("textual", "fastapi", "uvicorn", "websockets"):
            checks.append(
                DoctorCheck(
                    f"python:{package}",
                    importlib.util.find_spec(package) is not None,
                    "disponível" if importlib.util.find_spec(package) else "não instalado",
                    optional=True,
                )
            )
        for package in ("asyncssh", "psutil", "cryptography"):
            checks.append(
                DoctorCheck(
                    f"python:{package}",
                    importlib.util.find_spec(package) is not None,
                    "disponível" if importlib.util.find_spec(package) else "não instalado",
                    optional=True,
                )
            )
        try:
            limit = max_file_bytes()
            checks.append(
                DoctorCheck(
                    "configuration:file-limit",
                    True,
                    f"max_file_bytes={limit}",
                    optional=False,
                )
            )
        except ValueError as error:
            checks.append(DoctorCheck("configuration:file-limit", False, str(error)))
        try:
            quota = max_workspace_bytes()
            used = workspace_usage(paths.root)
            checks.append(
                DoctorCheck(
                    "configuration:workspace-quota",
                    quota is None or used <= quota,
                    f"used_bytes={used}; quota_bytes={quota if quota is not None else 'unlimited'}",
                )
            )
        except ValueError as error:
            checks.append(DoctorCheck("configuration:workspace-quota", False, str(error)))
        try:
            days = retention_days()
            checks.append(
                DoctorCheck(
                    "configuration:retention",
                    True,
                    f"retention_days={days if days is not None else 'disabled'}",
                )
            )
        except ValueError as error:
            checks.append(DoctorCheck("configuration:retention", False, str(error)))
        try:
            usage = shutil.disk_usage(paths.root)
            checks.append(
                DoctorCheck(
                    "disk-space",
                    usage.free >= 50 * 1024 * 1024,
                    f"free={usage.free} bytes",
                    optional=True,
                )
            )
        except OSError as error:
            checks.append(DoctorCheck("disk-space", False, str(error), optional=True))
        checks.append(
            DoctorCheck(
                "motor-socket",
                not (paths.root / "motor.sock").exists(),
                (
                    "nenhum socket abandonado"
                    if not (paths.root / "motor.sock").exists()
                    else "socket existe"
                ),
                optional=True,
            )
        )
        lock_path = paths.root / "motor.lock"
        owner = EngineLock.read_owner(lock_path) if lock_path.exists() else {}
        owner_pid = int(owner.get("pid", 0) or 0)
        motor_active = owner_pid > 0 and process_identity(owner_pid) is not None
        checks.append(
            DoctorCheck(
                "motor-lock",
                not motor_active,
                f"motor ativo (pid={owner_pid})" if motor_active else "nenhum motor ativo",
                optional=True,
            )
        )
        checks.extend(self._managed_process_checks(paths.database))
        return DoctorReport(tuple(checks))

    @staticmethod
    def _database_checks(path: Path) -> list[DoctorCheck]:
        database_path = str(path)
        checks: list[DoctorCheck] = []
        try:
            with sqlite3.connect(database_path) as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                version_row = connection.execute(
                    "SELECT MAX(version) FROM schema_version"
                ).fetchone()
                version = int(version_row[0] or 0)
                table_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
                    ).fetchone()[0]
                )
            checks.append(DoctorCheck("database-integrity", integrity == "ok", integrity))
            checks.append(
                DoctorCheck(
                    "schema-version",
                    version == SCHEMA_VERSION,
                    f"{version} (esperado {SCHEMA_VERSION})",
                )
            )
            checks.append(DoctorCheck("database-tables", table_count > 0, str(table_count)))
        except sqlite3.Error as error:
            checks.append(DoctorCheck("database-integrity", False, str(error)))
        return checks

    @staticmethod
    def _managed_process_checks(path: Path) -> list[DoctorCheck]:
        """Report stale managed PIDs without signalling or adopting them."""

        checks: list[DoctorCheck] = []
        try:
            with sqlite3.connect(path) as connection:
                rows = connection.execute(
                    "SELECT id, pid, process_executable, process_fingerprint "
                    "FROM forwards WHERE pid IS NOT NULL"
                ).fetchall()
        except sqlite3.Error:
            return checks
        stale = [int(row[0]) for row in rows if process_identity(int(row[1])) is None]
        checks.append(
            DoctorCheck(
                "managed-processes",
                not stale,
                "stale=" + ",".join(str(item) for item in stale) if stale else "nenhum PID stale",
                optional=True,
            )
        )
        return checks
