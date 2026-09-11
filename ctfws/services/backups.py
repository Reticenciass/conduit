"""Complete workspace backup and integrity-checked restore."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from ctfws.database.db import SCHEMA_VERSION, Database
from ctfws.services.workspace import WorkspaceService


class WorkspaceBackupService:
    """Archive database, evidence and operational files without vault secrets."""

    INCLUDED_DIRS = ("notes", "evidence", "reports", "loot", "logs")

    def create(self, workspace: WorkspaceService, destination: Path) -> Path:
        destination = destination.expanduser().resolve()
        if destination.suffix.lower() != ".zip":
            destination = destination.with_suffix(".zip")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ctfws-backup-") as temporary:
            temporary_path = Path(temporary)
            database_copy = temporary_path / "workspace.db"
            workspace.database.backup_to(database_copy)
            files: list[Path] = [database_copy]
            for directory in self.INCLUDED_DIRS:
                source = workspace.paths.root / directory
                if not source.is_dir():
                    continue
                for item in source.rglob("*"):
                    if item.is_file():
                        relative = Path(directory) / item.relative_to(source)
                        target = temporary_path / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, target)
                        files.append(target)
            manifest = {
                "format": "ctfws-backup-v1",
                "schema_version": SCHEMA_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "files": {
                    item.relative_to(temporary_path).as_posix(): {
                        "size": item.stat().st_size,
                        "sha256": self._sha256(item),
                    }
                    for item in files
                },
            }
            manifest_path = temporary_path / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for item in [*files, manifest_path]:
                    archive.write(item, item.relative_to(temporary_path).as_posix())
        return destination

    def restore(self, archive: Path, destination: Path, *, replace: bool = False) -> Path:
        archive = archive.expanduser().resolve()
        requested_destination = destination.expanduser()
        if not archive.is_file():
            raise FileNotFoundError(f"Backup não encontrado: {archive}")
        if requested_destination.exists() and requested_destination.is_symlink():
            raise ValueError("O destino de restauração não pode ser um link simbólico.")
        destination = requested_destination.resolve()
        if destination.exists() and any(destination.iterdir()) and not replace:
            raise ValueError(
                "O destino já contém dados; escolha uma pasta nova ou confirme replace."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ctfws-restore-") as temporary:
            temporary_path = Path(temporary)
            with zipfile.ZipFile(archive) as source:
                self._extract_safe(source, temporary_path)
            manifest_path = temporary_path / "manifest.json"
            if not manifest_path.is_file():
                raise ValueError("Backup sem manifesto de integridade.")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") != "ctfws-backup-v1":
                raise ValueError("Formato de backup não suportado.")
            for relative, metadata in dict(manifest.get("files", {})).items():
                item = (temporary_path / relative).resolve()
                if temporary_path not in item.parents:
                    raise ValueError("Manifesto aponta para fora do backup.")
                if not item.is_file() or item.stat().st_size != int(metadata["size"]):
                    raise ValueError(f"Arquivo inválido no backup: {relative}")
                if self._sha256(item) != metadata["sha256"]:
                    raise ValueError(f"Hash inválido no backup: {relative}")
            if destination.exists() and replace:
                for item in destination.iterdir():
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
            shutil.copytree(temporary_path, destination, dirs_exist_ok=True)
            (destination / "manifest.json").unlink(missing_ok=True)
        Database(destination / "workspace.db").initialize()
        return destination

    @staticmethod
    def _extract_safe(archive: zipfile.ZipFile, destination: Path) -> None:
        for member in archive.infolist():
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("Backup contém link simbólico; restauração bloqueada.")
            target = (destination / member.filename).resolve()
            if destination not in target.parents and target != destination:
                raise ValueError("Backup contém caminho fora do destino.")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
