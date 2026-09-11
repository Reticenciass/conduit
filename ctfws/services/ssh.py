"""Reusable AsyncSSH transports owned by one workspace motor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from ctfws.models.connection import ConnectionProfileRead, ConnectionState
from ctfws.services.connections import ConnectionService, classify_ssh_error
from ctfws.services.workspace import WorkspaceService

_asyncssh: Any
try:
    import asyncssh as _asyncssh
except ImportError:  # pragma: no cover - optional runtime dependency
    _asyncssh = None
asyncssh: Any = _asyncssh


class AsyncSSHConnectionManager:
    """Share one authenticated SSH connection among operations in a workspace.

    Passwords are accepted only for the duration of ``connect`` and are never
    put in the profile, command line, logs or returned objects.
    """

    def __init__(
        self,
        workspace: WorkspaceService,
        secret_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self.workspace = workspace
        self.secret_resolver = secret_resolver
        self.connections: dict[int, Any] = {}
        self._chain_connections: dict[int, list[Any]] = {}
        self._connecting: dict[int, Any] = {}

    CAPABILITIES = (
        "exec",
        "pty",
        "sftp",
        "local-forward",
        "remote-forward",
        "dynamic-socks",
    )

    @property
    def available(self) -> bool:
        return asyncssh is not None

    async def connect(
        self,
        profile_id: int,
        password: str | None = None,
        *,
        credential_kind: str | None = None,
    ) -> Any:
        """Connect a profile and its jump chain, reusing an existing session."""

        if asyncssh is None:
            raise RuntimeError("AsyncSSH não está instalado no motor.")
        existing = self.connections.get(profile_id)
        if existing is not None and not existing.is_closed():
            self.workspace.connections.update_runtime(
                profile_id, state=ConnectionState.READY.value, last_error=None
            )
            return existing
        if profile_id in self._connecting:
            connection, _chain_connections = await self._connecting[profile_id]
            return connection
        self.workspace.connections.update_runtime(
            profile_id, state=ConnectionState.CONNECTING.value, last_error=None
        )
        task = asyncio.create_task(self._connect_chain(profile_id, password, credential_kind))
        self._connecting[profile_id] = task
        try:
            connection, chain_connections = await task
            self.connections[profile_id] = connection
            self._chain_connections[profile_id] = chain_connections
            profile = self.workspace.connections.get(profile_id)
            self.workspace.connections.update_runtime(
                profile_id,
                state=ConnectionState.READY.value,
                last_error=None,
                generation=(profile.generation + 1) if profile is not None else None,
                capabilities=self.CAPABILITIES,
            )
            return connection
        except Exception as error:
            self.workspace.connections.update_runtime(
                profile_id,
                state=ConnectionState.ERROR.value,
                last_error=self._safe_error(error, secret=password),
            )
            self.workspace.mark_connection_dependents(
                self._dependent_profile_ids(profile_id),
                "A conexão SSH não pôde ser estabelecida.",
            )
            raise
        finally:
            self._connecting.pop(profile_id, None)

    async def run(
        self,
        profile_id: int,
        command: str,
        *,
        password: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Run one command on a shared SSH connection."""

        connection = await self.connect(profile_id, password)
        return await connection.run(command, check=False, timeout=timeout)

    async def start_sftp(self, profile_id: int, *, password: str | None = None) -> Any:
        """Open an SFTP client over the profile's shared SSH connection."""

        connection = await self.connect(profile_id, password)
        return await connection.start_sftp_client()

    async def close(self, profile_id: int) -> None:
        """Close one profile connection and update its durable state."""

        affected = self._dependent_profile_ids(profile_id)
        self.connections.pop(profile_id, None)
        chain_connections = self._chain_connections.pop(profile_id, [])
        for connection in reversed(chain_connections):
            connection.close()
            with suppress(Exception):
                await connection.wait_closed()
        self.workspace.connections.update_runtime(
            profile_id, state=ConnectionState.DISCONNECTED.value, last_error=None
        )
        self.workspace.mark_connection_dependents(
            affected,
            "A conexão SSH foi encerrada; recursos dependentes precisam ser retomados.",
        )

    async def close_all(self) -> None:
        """Stop every transport owned by this motor."""

        for profile_id in list(self.connections):
            await self.close(profile_id)

    async def _connect_chain(
        self,
        profile_id: int,
        password: str | None,
        credential_kind: str | None,
    ) -> tuple[Any, list[Any]]:
        service = ConnectionService(self.workspace)
        chain = service.profile_chain(profile_id)
        tunnel: Any = None
        opened: list[Any] = []
        try:
            for profile in chain:
                # A temporary password belongs to the explicitly requested
                # endpoint.  Each jump resolves its own opt-in vault
                # reference instead of receiving the target's credential.
                profile_secret = (
                    password
                    if profile.id == profile_id and password is not None
                    else self._resolve_password(profile)
                )
                selected_kind = (
                    credential_kind if profile.id == profile_id and password is not None else None
                )
                kwargs = self._connect_kwargs(
                    profile,
                    profile_secret,
                    credential_kind=self._credential_kind(profile, selected_kind),
                )
                if tunnel is not None:
                    kwargs["tunnel"] = tunnel
                connection = await asyncssh.connect(**kwargs)
                opened.append(connection)
                tunnel = connection
            return opened[-1], opened
        except Exception:
            for connection in reversed(opened):
                connection.close()
                with suppress(Exception):
                    await connection.wait_closed()
            raise

    def _resolve_password(self, profile: ConnectionProfileRead) -> str | None:
        if not profile.auth_ref or self.secret_resolver is None:
            return None
        if not profile.auth_ref.startswith("vault:"):
            return None
        try:
            return self.secret_resolver(profile.auth_ref)
        except Exception as error:
            raise RuntimeError(
                "A credencial referenciada não pôde ser resolvida pelo cofre."
            ) from error

    @staticmethod
    def _connect_kwargs(
        profile: ConnectionProfileRead,
        password: str | None,
        *,
        credential_kind: str = "password",
    ) -> dict[str, Any]:
        if credential_kind not in {"password", "passphrase"}:
            raise ValueError("Tipo de credencial SSH não suportado.")
        kwargs: dict[str, Any] = {
            "host": profile.host,
            "port": profile.port,
            "username": profile.user,
        }
        if password is not None:
            kwargs[credential_kind] = password
        if profile.identity_file:
            kwargs["client_keys"] = [profile.identity_file]
        if profile.known_hosts_file:
            kwargs["known_hosts"] = profile.known_hosts_file
        return kwargs

    @staticmethod
    def _credential_kind(profile: ConnectionProfileRead, requested: str | None) -> str:
        """Map a profile/one-time choice to AsyncSSH's secret keyword."""

        selected = (requested or profile.auth_method or "").casefold().replace("-", "_")
        if selected in {"key_passphrase", "passphrase"}:
            return "passphrase"
        if selected in {"password", "ssh_password"}:
            return "password"
        # Preserve the legacy meaning of auth_ref and the existing temporary
        # password API for profiles which use agent_or_key by default.
        return "password"

    def _dependent_profile_ids(self, profile_id: int) -> set[int]:
        """Include profiles whose explicit jump chain contains this profile."""

        affected = {profile_id}
        profiles = self.workspace.connections.list()
        changed = True
        while changed:
            changed = False
            for profile in profiles:
                if profile.id not in affected and any(
                    jump_id in affected for jump_id in profile.jump_profile_ids
                ):
                    affected.add(profile.id)
                    changed = True
        return affected

    @staticmethod
    def _safe_error(error: Exception, *, secret: str | None = None) -> str:
        text = str(error)
        if secret:
            text = text.replace(secret, "<redacted>")
        text = text.replace("password", "credential").replace("Password", "credential")
        return f"[{classify_ssh_error(text)}] {text[:980]}"
