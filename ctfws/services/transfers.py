"""Explicit SFTP transfers for saved SSH profiles."""

from __future__ import annotations

import asyncio
import hashlib
import os
import posixpath
import shlex
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctfws.core.errors import EntityNotFoundError
from ctfws.core.limits import max_file_bytes, max_workspace_bytes
from ctfws.models.connection import ConnectionProfileRead
from ctfws.models.transfer import ConflictPolicy, TransferDirection, TransferStatus
from ctfws.services.connections import ConnectionService
from ctfws.services.maintenance import workspace_usage
from ctfws.services.workspace import WorkspaceService


@dataclass(frozen=True, slots=True)
class TransferResult:
    """Result of one completed SFTP transfer."""

    direction: str
    local_path: str
    remote_path: str
    size: int
    sha256: str
    output: str
    transfer_id: int | None = None
    integrity_verified: bool = False


def _effective_conflict(overwrite: bool, policy: ConflictPolicy) -> ConflictPolicy:
    """Keep the legacy boolean while honoring an explicit conflict policy."""

    return ConflictPolicy.REPLACE if overwrite and policy == ConflictPolicy.CANCEL else policy


def _keep_both_path(target: Path) -> Path:
    """Choose a free local sibling without replacing the requested destination."""

    for index in range(1, 10_000):
        candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise ValueError("Não foi possível encontrar um nome livre para manter ambos os arquivos.")


class SFTPTransferService:
    """Transfer files through OpenSSH SFTP without invoking a shell."""

    def __init__(self, workspace: WorkspaceService, ssh_manager: Any | None = None) -> None:
        self.workspace = workspace
        self.ssh_manager = ssh_manager
        self.max_file_size = max_file_bytes()

    @property
    def async_available(self) -> bool:
        """Whether this service can use the motor's shared AsyncSSH session."""

        return self.ssh_manager is not None and bool(self.ssh_manager.available)

    async def upload_async(
        self,
        connection_id: int,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool = False,
        conflict: ConflictPolicy = ConflictPolicy.CANCEL,
    ) -> TransferResult:
        """Upload through shared AsyncSSH/SFTP with remote atomic finalization."""

        if not self.async_available:
            return await asyncio.to_thread(
                self.upload,
                connection_id,
                local_path,
                remote_path,
                overwrite=overwrite,
                conflict=conflict,
            )
        manager = self._async_manager()
        source = self._local_file(local_path)
        return await self._upload_async_source(
            manager,
            connection_id,
            source,
            self._remote_path(remote_path),
            source.relative_to(self.workspace.paths.root).as_posix(),
            overwrite,
            conflict,
        )

    async def upload_tool_async(
        self,
        connection_id: int,
        tool_id: int,
        remote_path: str,
        *,
        overwrite: bool = False,
        conflict: ConflictPolicy = ConflictPolicy.CANCEL,
    ) -> TransferResult:
        """Transfer a cataloged tool only after rechecking its recorded hash."""

        source, size, digest = self._catalog_tool_source(tool_id)
        if not self.async_available:
            return await asyncio.to_thread(
                self._upload_tool_sync,
                connection_id,
                tool_id,
                source,
                self._remote_path(remote_path),
                overwrite,
                conflict,
                size,
                digest,
            )
        return await self._upload_async_source(
            self._async_manager(),
            connection_id,
            source,
            self._remote_path(remote_path),
            f"tool:{tool_id}:{source.name}",
            overwrite,
            conflict,
            size=size,
            digest=digest,
        )

    def upload_tool(
        self,
        connection_id: int,
        tool_id: int,
        remote_path: str,
        *,
        overwrite: bool = False,
        conflict: ConflictPolicy = ConflictPolicy.CANCEL,
    ) -> TransferResult:
        """Upload a cataloged tool through the compatibility SFTP process path."""

        source, size, digest = self._catalog_tool_source(tool_id)
        return self._upload_tool_sync(
            connection_id,
            tool_id,
            source,
            self._remote_path(remote_path),
            overwrite,
            conflict,
            size,
            digest,
        )

    async def _upload_async_source(
        self,
        manager: Any,
        connection_id: int,
        source: Path,
        remote: str,
        display_path: str,
        overwrite: bool,
        conflict: ConflictPolicy,
        *,
        size: int | None = None,
        digest: str | None = None,
    ) -> TransferResult:
        profile = self._profile(connection_id)
        size, digest = (
            (size, digest) if size is not None and digest is not None else self._digest(source)
        )
        transfer: Any | None = None
        sftp: Any | None = None
        temporary: str | None = None
        try:
            sftp = await manager.start_sftp(profile.id)
            policy = _effective_conflict(overwrite, conflict)
            resolved_remote = remote
            if await self._remote_exists(sftp, remote):
                if policy == ConflictPolicy.CANCEL:
                    raise ValueError(
                        "destination_conflict: o arquivo remoto já existe; "
                        "escolha conflict=keep_both ou conflict=replace."
                    )
                if policy == ConflictPolicy.KEEP_BOTH:
                    resolved_remote = await self._keep_both_remote(sftp, remote)
            transfer = self.workspace.transfers.create(
                connection_id, TransferDirection.UPLOAD, display_path, resolved_remote, size
            )
            temporary = f"{resolved_remote}.ctfws-{uuid.uuid4().hex}.part"
            await self._write_remote_file(sftp, source, temporary)
            await self._finalize_remote_file(sftp, temporary, resolved_remote, policy)
            remote_digest = await self._remote_digest(sftp, resolved_remote)
            verified = remote_digest == digest
            if not verified:
                raise RuntimeError("A verificação SFTP detectou divergência no hash remoto.")
        except asyncio.CancelledError:
            if sftp is not None and temporary is not None:
                await self._remove_remote_file(sftp, temporary)
            if transfer is not None:
                self.workspace.transfers.finish(
                    transfer.id,
                    status=TransferStatus.CANCELLED,
                    bytes_transferred=0,
                    size=size,
                    local_sha256=digest,
                    error="Transferência cancelada pelo operador.",
                )
            raise
        except Exception as error:
            if sftp is not None and temporary is not None:
                await self._remove_remote_file(sftp, temporary)
            if transfer is not None:
                self.workspace.transfers.finish(
                    transfer.id,
                    status=TransferStatus.FAILED,
                    bytes_transferred=0,
                    size=size,
                    local_sha256=digest,
                    error=str(error)[:1000],
                )
            raise
        assert transfer is not None
        self.workspace.transfers.finish(
            transfer.id,
            status=TransferStatus.SUCCEEDED,
            bytes_transferred=size,
            size=size,
            local_sha256=digest,
            remote_sha256=remote_digest,
            integrity_verified=verified,
        )
        return TransferResult(
            "upload", display_path, resolved_remote, size, digest, "", transfer.id, verified
        )

    async def download_async(
        self,
        connection_id: int,
        remote_path: str,
        local_path: str,
        *,
        overwrite: bool = False,
        conflict: ConflictPolicy = ConflictPolicy.CANCEL,
    ) -> TransferResult:
        """Download by streaming SFTP into a workspace temporary file."""

        if not self.async_available:
            return await asyncio.to_thread(
                self.download,
                connection_id,
                remote_path,
                local_path,
                overwrite=overwrite,
                conflict=conflict,
            )
        manager = self._async_manager()
        profile = self._profile(connection_id)
        remote = self._remote_path(remote_path)
        target = self._local_target(local_path)
        policy = _effective_conflict(overwrite, conflict)
        if target.exists():
            if policy == ConflictPolicy.CANCEL:
                raise ValueError(
                    "destination_conflict: o arquivo local já existe; "
                    "escolha conflict=keep_both ou conflict=replace."
                )
            if policy == ConflictPolicy.KEEP_BOTH:
                target = _keep_both_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        existing_size = target.stat().st_size if target.exists() else 0
        quota_base = workspace_usage(self.workspace.paths.root) - existing_size
        temporary = target.with_name(f".{target.name}.ctfws-{uuid.uuid4().hex}.part")
        transfer = self.workspace.transfers.create(
            connection_id,
            TransferDirection.DOWNLOAD,
            target.relative_to(self.workspace.paths.root).as_posix(),
            remote,
            0,
        )
        sftp: Any | None = None
        try:
            sftp = await manager.start_sftp(profile.id)
            size, digest = await self._read_remote_file(
                sftp, remote, temporary, quota_base=quota_base
            )
            remote_digest = await self._remote_digest(sftp, remote)
            if remote_digest != digest:
                raise RuntimeError("A verificação SFTP detectou divergência no hash remoto.")
            os.replace(temporary, target)
            self.workspace.transfers.finish(
                transfer.id,
                status=TransferStatus.SUCCEEDED,
                bytes_transferred=size,
                size=size,
                local_sha256=digest,
                remote_sha256=remote_digest,
                integrity_verified=remote_digest == digest,
            )
        except asyncio.CancelledError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self.workspace.transfers.finish(
                transfer.id,
                status=TransferStatus.CANCELLED,
                bytes_transferred=0,
                size=0,
                local_sha256=None,
                error="Transferência cancelada pelo operador.",
            )
            raise
        except Exception as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self.workspace.transfers.finish(
                transfer.id,
                status=TransferStatus.FAILED,
                bytes_transferred=0,
                size=0,
                local_sha256=None,
                error=str(error)[:1000],
            )
            raise
        return TransferResult(
            "download",
            target.relative_to(self.workspace.paths.root).as_posix(),
            remote,
            size,
            digest,
            "",
            transfer.id,
            True,
        )

    async def list_remote_async(
        self, connection_id: int, remote_path: str = "."
    ) -> list[dict[str, str]]:
        """List SFTP entries using the shared connection, without a shell."""

        if not self.async_available:
            return await asyncio.to_thread(self.list_remote, connection_id, remote_path)
        manager = self._async_manager()
        profile = self._profile(connection_id)
        remote = self._remote_path(remote_path)
        sftp = await manager.start_sftp(profile.id)
        entries = await sftp.readdir(remote)
        result: list[dict[str, str]] = []
        for entry in entries:
            name = str(getattr(entry, "filename", entry))
            attrs = getattr(entry, "attrs", None)
            permissions = getattr(attrs, "permissions", None)
            kind = (
                "directory"
                if isinstance(permissions, int) and stat.S_ISDIR(permissions)
                else "file" if isinstance(permissions, int) else "entry"
            )
            result.append({"name": name, "path": posixpath.join(remote, name), "kind": kind})
        return result

    def upload(
        self,
        connection_id: int,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool = False,
        conflict: ConflictPolicy = ConflictPolicy.CANCEL,
    ) -> TransferResult:
        policy = _effective_conflict(overwrite, conflict)
        profile = self._profile(connection_id)
        source = self._local_file(local_path)
        remote = self._remote_path(remote_path)
        if policy == ConflictPolicy.KEEP_BOTH:
            remote = self._keep_both_remote_sync(profile, remote)
        elif policy == ConflictPolicy.CANCEL and self._remote_exists_sync(profile, remote):
            raise ValueError(
                "destination_conflict: o arquivo remoto já existe; "
                "escolha conflict=keep_both ou conflict=replace."
            )
        size, digest = self._digest(source)
        transfer = self.workspace.transfers.create(
            connection_id,
            TransferDirection.UPLOAD,
            source.relative_to(self.workspace.paths.root).as_posix(),
            remote,
            size,
        )
        batch = f"put {shlex.quote(str(source))} {shlex.quote(remote)}\n"
        try:
            output = self._run(profile, batch)
        except Exception as error:
            self.workspace.transfers.finish(
                transfer.id,
                status=TransferStatus.FAILED,
                bytes_transferred=0,
                size=size,
                local_sha256=digest,
                error=str(error)[:1000],
            )
            raise
        self.workspace.transfers.finish(
            transfer.id,
            status=TransferStatus.SUCCEEDED,
            bytes_transferred=size,
            size=size,
            local_sha256=digest,
        )
        return TransferResult(
            "upload",
            source.relative_to(self.workspace.paths.root).as_posix(),
            remote,
            size,
            digest,
            output,
            transfer.id,
        )

    def download(
        self,
        connection_id: int,
        remote_path: str,
        local_path: str,
        *,
        overwrite: bool = False,
        conflict: ConflictPolicy = ConflictPolicy.CANCEL,
    ) -> TransferResult:
        profile = self._profile(connection_id)
        remote = self._remote_path(remote_path)
        target = self._local_target(local_path)
        policy = _effective_conflict(overwrite, conflict)
        if target.exists():
            if policy == ConflictPolicy.CANCEL:
                raise ValueError(
                    "destination_conflict: o arquivo local já existe; "
                    "escolha conflict=keep_both ou conflict=replace."
                )
            if policy == ConflictPolicy.KEEP_BOTH:
                target = _keep_both_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        existing_size = target.stat().st_size if target.exists() else 0
        quota_base = workspace_usage(self.workspace.paths.root) - existing_size
        temporary = target.with_name(f".{target.name}.ctfws-{uuid.uuid4().hex}.part")
        transfer = self.workspace.transfers.create(
            connection_id,
            TransferDirection.DOWNLOAD,
            target.relative_to(self.workspace.paths.root).as_posix(),
            remote,
            0,
        )
        batch = f"get {shlex.quote(remote)} {shlex.quote(str(temporary))}\n"
        try:
            output = self._run(profile, batch)
            size, digest = self._digest(temporary)
            self._check_quota(quota_base, size)
            os.replace(temporary, target)
        except Exception as error:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self.workspace.transfers.finish(
                transfer.id,
                status=TransferStatus.FAILED,
                bytes_transferred=0,
                size=0,
                local_sha256=None,
                error=str(error)[:1000],
            )
            raise
        self.workspace.transfers.finish(
            transfer.id,
            status=TransferStatus.SUCCEEDED,
            bytes_transferred=size,
            size=size,
            local_sha256=digest,
        )
        return TransferResult(
            "download",
            target.relative_to(self.workspace.paths.root).as_posix(),
            remote,
            size,
            digest,
            output,
            transfer.id,
        )

    def _upload_tool_sync(
        self,
        connection_id: int,
        tool_id: int,
        source: Path,
        remote: str,
        overwrite: bool,
        conflict: ConflictPolicy,
        size: int,
        digest: str,
    ) -> TransferResult:
        policy = _effective_conflict(overwrite, conflict)
        profile = self._profile(connection_id)
        display_path = f"tool:{tool_id}:{source.name}"
        if policy == ConflictPolicy.KEEP_BOTH:
            remote = self._keep_both_remote_sync(profile, remote)
        elif policy == ConflictPolicy.CANCEL and self._remote_exists_sync(profile, remote):
            raise ValueError(
                "destination_conflict: o arquivo remoto já existe; "
                "escolha conflict=keep_both ou conflict=replace."
            )
        transfer = self.workspace.transfers.create(
            connection_id, TransferDirection.UPLOAD, display_path, remote, size
        )
        batch = f"put {shlex.quote(str(source))} {shlex.quote(remote)}\n"
        try:
            output = self._run(profile, batch)
        except Exception as error:
            self.workspace.transfers.finish(
                transfer.id,
                status=TransferStatus.FAILED,
                bytes_transferred=0,
                size=size,
                local_sha256=digest,
                error=str(error)[:1000],
            )
            raise
        self.workspace.transfers.finish(
            transfer.id,
            status=TransferStatus.SUCCEEDED,
            bytes_transferred=size,
            size=size,
            local_sha256=digest,
        )
        return TransferResult("upload", display_path, remote, size, digest, output, transfer.id)

    def list_remote(self, connection_id: int, remote_path: str = ".") -> list[dict[str, str]]:
        """List remote entries without invoking a remote shell."""

        profile = self._profile(connection_id)
        remote = self._remote_path(remote_path)
        output = self._run(profile, f"ls -1 {shlex.quote(remote)}\n")
        entries: list[dict[str, str]] = []
        for line in output.splitlines():
            name = line.rstrip("\r")
            if not name or name.startswith("sftp>"):
                continue
            entries.append(
                {
                    "name": name,
                    "path": posixpath.join(remote, name),
                    "kind": "entry",
                }
            )
        return entries

    def _profile(self, connection_id: int) -> ConnectionProfileRead:
        profile = ConnectionService(self.workspace).get(connection_id)
        if profile.transport.value != "ssh":
            raise ValueError("Transferência SFTP exige um perfil SSH.")
        return profile

    def _catalog_tool_source(self, tool_id: int) -> tuple[Path, int, str]:
        """Resolve and hash a catalog entry immediately before a transfer."""

        tool = self.workspace.tools.get(tool_id)
        if tool is None:
            raise EntityNotFoundError(f"Ferramenta {tool_id} não encontrada neste workspace.")
        source = Path(tool.path).expanduser().resolve()
        if not source.is_file():
            raise EntityNotFoundError(f"Ferramenta local não encontrada: {source}")
        size, digest = self._digest(source)
        if digest != tool.sha256:
            raise ValueError(
                "A ferramenta mudou desde o cadastro; atualize o catálogo antes de enviar."
            )
        return source, size, digest

    def _async_manager(self) -> Any:
        if self.ssh_manager is None or not self.ssh_manager.available:
            raise RuntimeError("O transporte AsyncSSH não está disponível no motor.")
        return self.ssh_manager

    def _local_file(self, relative: str) -> Path:
        target = self._local_target(relative)
        if not target.is_file():
            raise EntityNotFoundError(f"Arquivo local não encontrado: {relative}")
        if target.stat().st_size > self.max_file_size:
            raise ValueError("O arquivo excede o limite configurado do workspace.")
        return target

    def _digest(self, path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                if size > self.max_file_size:
                    raise ValueError("O arquivo excede o limite configurado do workspace.")
                digest.update(chunk)
        return size, digest.hexdigest()

    async def _write_remote_file(self, sftp: Any, source: Path, remote: str) -> None:
        remote_file = await sftp.open(remote, "wb")
        try:
            with source.open("rb") as local_file:
                while chunk := local_file.read(1024 * 1024):
                    await remote_file.write(chunk)
        finally:
            close = getattr(remote_file, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    @staticmethod
    async def _remote_exists(sftp: Any, remote: str) -> bool:
        stat = getattr(sftp, "stat", None)
        if stat is None:
            return False
        try:
            await stat(remote)
        except Exception as error:
            if getattr(error, "code", None) == 2 or "no such file" in str(error).lower():
                return False
            raise
        return True

    async def _keep_both_remote(self, sftp: Any, remote: str) -> str:
        """Choose a free remote sibling without overwriting an existing file."""

        directory, filename = posixpath.split(remote)
        stem, suffix = posixpath.splitext(filename)
        for index in range(1, 10_000):
            candidate_name = f"{stem} ({index}){suffix}"
            candidate = posixpath.join(directory, candidate_name) if directory else candidate_name
            if not await self._remote_exists(sftp, candidate):
                return candidate
        raise ValueError(
            "Não foi possível encontrar um nome remoto livre para manter ambos os arquivos."
        )

    async def _finalize_remote_file(
        self, sftp: Any, temporary: str, target: str, policy: ConflictPolicy
    ) -> None:
        """Publish a completed upload, replacing only after explicit review."""

        if policy == ConflictPolicy.REPLACE:
            posix_rename = getattr(sftp, "posix_rename", None)
            if callable(posix_rename):
                try:
                    await posix_rename(temporary, target)
                    return
                except Exception as error:
                    detail = str(error).lower()
                    unsupported = getattr(error, "code", None) in {8, "SSH_FX_OP_UNSUPPORTED"}
                    unsupported = unsupported or "unsupported" in detail
                    if not unsupported:
                        raise
            remove = getattr(sftp, "remove", None)
            if not callable(remove):
                raise RuntimeError(
                    "O servidor SFTP não oferece substituição atômica nem remoção de destino."
                )
            try:
                await remove(target)
            except Exception as error:
                missing = getattr(error, "code", None) == 2 or "no such file" in str(error).lower()
                if not missing:
                    raise
        await sftp.rename(temporary, target)

    @staticmethod
    async def _remove_remote_file(sftp: Any, remote: str) -> None:
        """Best-effort cleanup that never hides the original transfer error."""

        remove = getattr(sftp, "remove", None)
        if not callable(remove):
            return
        try:
            await remove(remote)
        except Exception as error:
            missing = getattr(error, "code", None) == 2 or "no such file" in str(error).lower()
            if not missing:
                return

    def _remote_exists_sync(self, profile: ConnectionProfileRead, remote: str) -> bool:
        """Check a remote path through SFTP without treating real errors as absence."""

        try:
            self._run(profile, f"ls -d {shlex.quote(remote)}\n")
        except RuntimeError as error:
            detail = str(error).lower()
            missing_markers = (
                "no such file",
                "not found",
                "cannot stat",
                "can't stat",
                "does not exist",
            )
            if any(marker in detail for marker in missing_markers):
                return False
            raise
        return True

    def _keep_both_remote_sync(self, profile: ConnectionProfileRead, remote: str) -> str:
        """Choose a free remote sibling using the compatibility SFTP process."""

        directory, filename = posixpath.split(remote)
        stem, suffix = posixpath.splitext(filename)
        for index in range(1, 10_000):
            candidate_name = f"{stem} ({index}){suffix}"
            candidate = posixpath.join(directory, candidate_name) if directory else candidate_name
            if not self._remote_exists_sync(profile, candidate):
                return candidate
        raise ValueError(
            "Não foi possível encontrar um nome remoto livre para manter ambos os arquivos."
        )

    async def _remote_digest(self, sftp: Any, remote: str) -> str:
        remote_file = await sftp.open(remote, "rb")
        digest = hashlib.sha256()
        try:
            while chunk := await remote_file.read(1024 * 1024):
                digest.update(chunk)
        finally:
            close = getattr(remote_file, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
        return digest.hexdigest()

    async def _read_remote_file(
        self,
        sftp: Any,
        remote: str,
        target: Path,
        *,
        quota_base: int | None = None,
    ) -> tuple[int, str]:
        remote_file = await sftp.open(remote, "rb")
        digest = hashlib.sha256()
        size = 0
        try:
            with target.open("wb") as local_file:
                while chunk := await remote_file.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_file_size:
                        raise ValueError(
                            "O arquivo remoto excede o limite configurado do workspace."
                        )
                    if quota_base is not None:
                        self._check_quota(quota_base, size)
                    local_file.write(chunk)
                    digest.update(chunk)
        finally:
            close = getattr(remote_file, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
        return size, digest.hexdigest()

    @staticmethod
    def _check_quota(base_usage: int, incoming_size: int) -> None:
        quota = max_workspace_bytes()
        if quota is not None and base_usage + incoming_size > quota:
            raise ValueError("A quota de armazenamento do workspace seria excedida.")

    def _local_target(self, relative: str) -> Path:
        root = self.workspace.paths.root.resolve()
        requested = root / relative
        if requested.is_symlink():
            raise ValueError("Links simbólicos não são destinos válidos no workspace.")
        target = requested.resolve()
        if target != root and root not in target.parents:
            raise ValueError("O arquivo precisa estar dentro do workspace.")
        if target.name == "workspace.db" or target.name.startswith("workspace.db-"):
            raise ValueError("Arquivos internos do workspace não são destinos de transferência.")
        return target

    @staticmethod
    def _remote_path(value: str) -> str:
        if not value or "\n" in value or "\r" in value:
            raise ValueError("O caminho remoto é obrigatório e não pode conter quebras de linha.")
        if value.startswith("-"):
            raise ValueError("O caminho remoto não pode começar com '-'.")
        return value

    def _run(self, profile: ConnectionProfileRead, batch: str) -> str:
        command = [
            "sftp",
            "-b",
            "-",
            "-P",
            str(profile.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
        ]
        chain = ConnectionService(self.workspace).profile_chain(profile.id)
        if len(chain) > 1:
            command.extend(
                [
                    "-J",
                    ",".join(f"{jump.user}@{jump.host}:{jump.port}" for jump in chain[:-1]),
                ]
            )
        if profile.identity_file:
            command.extend(["-i", profile.identity_file])
        if profile.known_hosts_file:
            command.extend(["-o", f"UserKnownHostsFile={profile.known_hosts_file}"])
        command.append(f"{profile.user}@{profile.host}")
        try:
            result = subprocess.run(
                command,
                input=batch,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except FileNotFoundError as error:
            raise RuntimeError("sftp não está instalado na máquina do motor.") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "sem saída").strip()
            raise RuntimeError(f"Transferência SFTP falhou: {detail}")
        return (result.stdout or result.stderr or "").strip()
