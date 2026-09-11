"""Guided connection-profile operations."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

from ctfws.core.errors import DuplicateEntityError, EntityNotFoundError
from ctfws.models.connection import (
    ConnectionProfileCreate,
    ConnectionProfileRead,
    ConnectionProfileUpdate,
    ConnectionState,
)
from ctfws.models.forward import ForwardCreate, ForwardKind
from ctfws.services.workspace import WorkspaceService


def classify_ssh_error(value: object) -> str:
    """Return a stable operator-facing category for common SSH failures."""

    text = str(value)
    normalized = text.casefold()
    if (
        "remote host identification has changed" in normalized
        or "offending" in normalized
        and "key" in normalized
        or "host key" in normalized
        and "changed" in normalized
    ):
        return "host_key_changed"
    if (
        "host key" in normalized
        and any(
            term in normalized for term in ("not trusted", "verification failed", "authenticity")
        )
    ) or "hostkeynotverifiable" in normalized:
        return "host_key_unknown"
    if any(
        term in normalized
        for term in (
            "permission denied",
            "authentication failed",
            "authfailed",
            "permissiondenied",
            "invalid user",
        )
    ):
        return "credential_rejected"
    if any(
        term in normalized
        for term in (
            "connection refused",
            "connection timed out",
            "timed out",
            "name or service not known",
            "could not resolve hostname",
            "no route to host",
            "connectionreset",
            "connectionlost",
        )
    ):
        return "host_unavailable"
    return "ssh_error"


@dataclass(frozen=True, slots=True)
class ParsedJump:
    """One safe ProxyJump endpoint extracted from an SSH command."""

    host: str
    user: str | None = None
    port: int = 22


@dataclass(frozen=True, slots=True)
class ParsedSSH:
    """The safe subset extracted from a pasted OpenSSH command."""

    host: str
    user: str
    port: int = 22
    identity_file: str | None = None
    known_hosts_file: str | None = None
    jump_targets: tuple[ParsedJump, ...] = ()


class ConnectionService:
    """Create and resolve profiles without evaluating pasted shell text."""

    AUTH_METHODS = {"agent_or_key", "password", "key_passphrase"}
    AUTH_METHOD_ALIASES = {
        "agent": "agent_or_key",
        "ssh_agent": "agent_or_key",
        "key": "agent_or_key",
        "ssh_password": "password",
        "passphrase": "key_passphrase",
    }

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace

    def add(self, data: ConnectionProfileCreate) -> ConnectionProfileRead:
        data = data.model_copy(update={"auth_method": self.normalize_auth_method(data.auth_method)})
        self._validate_dependencies(data)
        try:
            return self.workspace.connections.create(data)
        except Exception as error:
            if "UNIQUE constraint failed" in str(error):
                raise DuplicateEntityError(f"A conexão {data.name} já existe.") from error
            raise

    def get(self, profile_id: int) -> ConnectionProfileRead:
        profile = self.workspace.connections.get(profile_id)
        if profile is None:
            raise EntityNotFoundError(f"Conexão {profile_id} não encontrada.")
        return profile

    def mark_state(
        self,
        profile_id: int,
        state: ConnectionState,
        error: str | None = None,
    ) -> ConnectionProfileRead:
        """Record connection health without changing the saved profile."""

        return self.workspace.connections.update_runtime(
            profile_id, state=state.value, last_error=error
        )

    def update(
        self,
        profile_id: int,
        data: ConnectionProfileUpdate,
        *,
        expected_revision: int,
    ) -> ConnectionProfileRead:
        """Apply an optimistic-concurrency guarded profile edit."""

        data = data.model_copy(update={"auth_method": self.normalize_auth_method(data.auth_method)})
        self._validate_dependencies(ConnectionProfileCreate.model_validate(data.model_dump()))
        return self.workspace.connections.update(
            profile_id, data, expected_revision=expected_revision
        )

    @classmethod
    def normalize_auth_method(cls, value: str) -> str:
        """Return one canonical auth method while accepting old spellings."""

        selected = value.casefold().replace("-", "_")
        if selected in cls.AUTH_METHODS:
            return selected
        normalized = cls.AUTH_METHOD_ALIASES.get(selected)
        if normalized is not None:
            return normalized
        choices = ", ".join(sorted(cls.AUTH_METHODS))
        raise ValueError(f"Método de autenticação SSH inválido; escolha: {choices}.")

    def resolve_jump_profile_ids(
        self, parsed: ParsedSSH
    ) -> tuple[tuple[int, ...], tuple[ParsedJump, ...]]:
        """Match ProxyJump endpoints to existing profiles without guessing."""

        profiles = self.workspace.connections.list()
        matched: list[int] = []
        unresolved: list[ParsedJump] = []
        for jump in parsed.jump_targets:
            candidates = [
                profile
                for profile in profiles
                if profile.host == jump.host
                and profile.port == jump.port
                and (jump.user is None or profile.user == jump.user)
            ]
            if len(candidates) == 1:
                matched.append(candidates[0].id)
            else:
                unresolved.append(jump)
        return tuple(matched), tuple(unresolved)

    def profile_chain(self, profile_id: int) -> list[ConnectionProfileRead]:
        """Return jump profiles followed by the destination profile."""

        result: list[ConnectionProfileRead] = []
        visited: set[int] = set()

        def visit(current: ConnectionProfileRead) -> None:
            if current.id in visited:
                raise ValueError("A cadeia de saltos SSH contém um ciclo.")
            visited.add(current.id)
            for jump_id in current.jump_profile_ids:
                visit(self.get(jump_id))
            result.append(current)

        visit(self.get(profile_id))
        if len(result) > 6:
            raise ValueError("A cadeia SSH não pode ter mais de cinco saltos.")
        return result

    def ssh_command(
        self,
        profile_id: int,
        *remote_args: str,
        batch_mode: bool = False,
        pty: bool = False,
    ) -> list[str]:
        """Build a structured OpenSSH command for one saved profile."""

        profile = self.get(profile_id)
        chain = self.profile_chain(profile_id)
        command = ["ssh"]
        if pty:
            command.append("-tt")
        if batch_mode:
            command.extend(["-o", "BatchMode=yes"])
        command.extend(["-o", "ConnectTimeout=8"])
        if pty:
            command.extend(["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"])
        command.extend(["-p", str(profile.port)])
        if profile.identity_file:
            command.extend(["-i", profile.identity_file])
        if profile.known_hosts_file:
            command.extend(["-o", f"UserKnownHostsFile={profile.known_hosts_file}"])
        jumps = chain[:-1]
        if jumps:
            jump_targets = [f"{jump.user}@{jump.host}:{jump.port}" for jump in jumps]
            command.extend(["-J", ",".join(jump_targets)])
        command.append(f"{profile.user}@{profile.host}")
        command.extend(remote_args)
        return command

    def ssh_forward_command(self, spec: ForwardCreate, profile_id: int) -> str:
        """Build one reviewed forward command, including every configured jump."""

        profile = self.get(profile_id)
        chain = self.profile_chain(profile_id)
        options = [
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "BatchMode=yes",
            "-p",
            str(profile.port),
        ]
        if profile.identity_file:
            options.extend(["-i", profile.identity_file])
        if profile.known_hosts_file:
            options.extend(["-o", f"UserKnownHostsFile={profile.known_hosts_file}"])
        if len(chain) > 1:
            options.extend(
                [
                    "-J",
                    ",".join(f"{jump.user}@{jump.host}:{jump.port}" for jump in chain[:-1]),
                ]
            )
        target = f"{profile.user}@{profile.host}"
        if spec.kind == ForwardKind.DYNAMIC:
            mapping = f"{spec.local_address}:{spec.local_port}"
            return shlex.join(["ssh", "-N", *options, "-D", mapping, target])
        if not spec.target_address or spec.target_port is None:
            raise ValueError("Forward SSH exige target_address e target_port.")
        flag = {ForwardKind.LOCAL: "-L", ForwardKind.REMOTE: "-R"}[spec.kind]
        mapping = (
            f"{spec.local_address}:{spec.local_port}:" f"{spec.target_address}:{spec.target_port}"
        )
        return shlex.join(["ssh", "-N", *options, flag, mapping, target])

    def test(self, profile_id: int) -> ConnectionProfileRead:
        """Test authentication and host-key validation without a shell."""

        self.mark_state(profile_id, ConnectionState.CONNECTING)
        command = self.ssh_command(profile_id, "true", batch_mode=True)
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=15, check=False
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            return self.mark_state(
                profile_id,
                ConnectionState.ERROR,
                f"[{classify_ssh_error(error)}] {error}",
            )
        if completed.returncode == 0:
            return self.mark_state(profile_id, ConnectionState.READY)
        detail = (completed.stderr or completed.stdout or "sem saída").strip()
        return self.mark_state(
            profile_id,
            ConnectionState.ERROR,
            f"[{classify_ssh_error(detail)}] {detail}",
        )

    def _validate_dependencies(self, data: ConnectionProfileCreate) -> None:
        self.normalize_auth_method(data.auth_method)
        if data.host_id is not None and self.workspace.hosts.get(str(data.host_id)) is None:
            raise EntityNotFoundError(f"Host {data.host_id} não encontrado neste laboratório.")
        visited: set[int] = set()
        for jump_id in data.jump_profile_ids:
            if jump_id in visited:
                raise ValueError("A cadeia SSH contém um salto duplicado.")
            visited.add(jump_id)
            if self.workspace.connections.get(jump_id) is None:
                raise EntityNotFoundError(f"Perfil de salto {jump_id} não encontrado.")
        if len(data.jump_profile_ids) > 5:
            raise ValueError("A cadeia SSH não pode ter mais de cinco saltos.")

    @staticmethod
    def parse_ssh(value: str) -> ParsedSSH:
        """Parse an SSH command while rejecting executable/config injection flags."""

        tokens = shlex.split(value, posix=True)
        if tokens and tokens[0].split("/")[-1] == "ssh":
            tokens = tokens[1:]
        if not tokens:
            raise ValueError("Informe um comando SSH ou um destino SSH.")

        user: str | None = None
        port = 22
        identity_file: str | None = None
        known_hosts_file: str | None = None
        destination: str | None = None
        jump_targets: list[ParsedJump] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in {"-p", "--port"}:
                index += 1
                if index >= len(tokens):
                    raise ValueError("A opção -p exige uma porta.")
                port = int(tokens[index])
            elif token.startswith("-p") and token != "-p":
                port = int(token[2:])
            elif token in {"-i", "--identity-file"}:
                index += 1
                if index >= len(tokens):
                    raise ValueError("A opção -i exige um arquivo.")
                identity_file = tokens[index]
            elif token in {"-J", "--proxy-jump"} or (token.startswith("-J") and token != "-J"):
                if token in {"-J", "--proxy-jump"}:
                    index += 1
                    if index >= len(tokens):
                        raise ValueError("A opção -J exige uma cadeia de saltos.")
                    jump_value = tokens[index]
                else:
                    jump_value = token[2:]
                jump_targets.extend(ConnectionService._parse_jump_list(jump_value))
            elif token == "-o":
                index += 1
                if index >= len(tokens):
                    raise ValueError("A opção -o exige uma configuração.")
                option = tokens[index]
                key, separator, option_value = option.partition("=")
                if not separator:
                    raise ValueError("Use opções SSH no formato Chave=Valor.")
                if key.lower() in {"proxycommand", "localcommand", "remotecommand"}:
                    raise ValueError(f"A opção SSH {key} não é aceita pelo importador.")
                if key.lower() == "userknownhostsfile":
                    known_hosts_file = option_value
                elif key.lower() == "proxyjump":
                    jump_targets.extend(ConnectionService._parse_jump_list(option_value))
                else:
                    raise ValueError(f"Opção SSH não suportada pelo assistente: {key}")
            elif token.startswith("-o") and token != "-o":
                option = token[2:]
                key, separator, option_value = option.partition("=")
                if not separator:
                    raise ValueError("Use opções SSH no formato Chave=Valor.")
                if key.lower() in {"proxycommand", "localcommand", "remotecommand"}:
                    raise ValueError(f"A opção SSH {key} não é aceita pelo importador.")
                if key.lower() == "userknownhostsfile":
                    known_hosts_file = option_value
                elif key.lower() == "proxyjump":
                    jump_targets.extend(ConnectionService._parse_jump_list(option_value))
                else:
                    raise ValueError(f"Opção SSH não suportada pelo assistente: {key}")
            elif token in {"-l", "--login-name"}:
                index += 1
                if index >= len(tokens):
                    raise ValueError("A opção -l exige um usuário.")
                user = tokens[index]
            elif token.startswith("-"):
                # Accept harmless connection-display flags but never pass them through.
                if token not in {"-4", "-6", "-A", "-a", "-q", "-v", "-vv", "-vvv"}:
                    raise ValueError(f"Opção SSH não suportada pelo assistente: {token}")
            elif destination is None:
                destination = token
            else:
                raise ValueError("Informe somente um destino SSH.")
            index += 1

        if destination is None:
            raise ValueError("Nenhum destino SSH foi informado.")
        if "@" in destination:
            user, host = destination.rsplit("@", 1)
        else:
            host = destination
        if not user or not host:
            raise ValueError("O destino SSH precisa estar no formato usuario@host.")
        if not 1 <= port <= 65535:
            raise ValueError("A porta SSH precisa estar entre 1 e 65535.")
        if len(jump_targets) > 5:
            raise ValueError("A cadeia SSH não pode ter mais de cinco saltos.")
        return ParsedSSH(host, user, port, identity_file, known_hosts_file, tuple(jump_targets))

    @staticmethod
    def _parse_jump_list(value: str) -> list[ParsedJump]:
        if value == "none":
            return []
        values = value.split(",")
        if not values or any(not item for item in values):
            raise ValueError("A cadeia ProxyJump contém um salto vazio.")
        if len(values) > 5:
            raise ValueError("A cadeia SSH não pode ter mais de cinco saltos.")
        return [ConnectionService._parse_jump_target(item) for item in values]

    @staticmethod
    def _parse_jump_target(value: str) -> ParsedJump:
        """Parse one ProxyJump endpoint without invoking shell or SSH config."""

        if not value or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError("O salto ProxyJump contém caracteres inválidos.")
        user: str | None = None
        endpoint = value
        if "@" in endpoint:
            user, endpoint = endpoint.rsplit("@", 1)
            if not user:
                raise ValueError("O salto ProxyJump precisa de um usuário válido.")
        port = 22
        if endpoint.startswith("["):
            closing = endpoint.find("]")
            if closing < 0:
                raise ValueError("O salto ProxyJump IPv6 não está entre colchetes.")
            host = endpoint[1:closing]
            suffix = endpoint[closing + 1 :]
            if suffix:
                if not suffix.startswith(":") or not suffix[1:].isdigit():
                    raise ValueError("A porta do salto ProxyJump é inválida.")
                port = int(suffix[1:])
        elif endpoint.count(":") == 1 and endpoint.rsplit(":", 1)[1].isdigit():
            host, raw_port = endpoint.rsplit(":", 1)
            port = int(raw_port)
        else:
            host = endpoint
        if not host or len(host) > 255 or any(char in host for char in "/\\;|&$`\n\r"):
            raise ValueError("O host do salto ProxyJump é inválido.")
        if user is not None and (len(user) > 120 or any(char in user for char in "/\\;|&$`\n\r")):
            raise ValueError("O usuário do salto ProxyJump é inválido.")
        if not 1 <= port <= 65535:
            raise ValueError("A porta do salto ProxyJump precisa estar entre 1 e 65535.")
        return ParsedJump(host, user, port)
