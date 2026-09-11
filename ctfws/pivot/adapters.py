"""Explicit command adapters for supported pivot transports.

Adapters produce reviewed command plans only.  Process execution remains in
the separately-confirmed forward process service.
"""

from __future__ import annotations

from dataclasses import dataclass
from shlex import join as shell_join
from shlex import split as shell_split
from typing import Protocol

from ctfws.models.connection import ConnectionProfileRead
from ctfws.models.forward import ExecutionLocation, ForwardCreate, ForwardKind, TransportRole


@dataclass(frozen=True, slots=True)
class TransportPlan:
    """A deterministic transport command and operator-facing warning."""

    tool: str
    command: str
    safety_note: str
    capabilities: tuple[str, ...] = ()
    role: str = "client"
    execution_location: str = ExecutionLocation.MOTOR.value
    argv: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TransportCapability:
    """Static contract exposed to the UI before an adapter is selected."""

    name: str
    display_name: str
    roles: tuple[str, ...]
    execution_locations: tuple[str, ...]
    modes: tuple[str, ...]
    capabilities: tuple[str, ...]
    executable_candidates: tuple[str, ...]
    limitations: tuple[str, ...]
    requires_explicit_execution: bool = False


class PivotAdapter(Protocol):
    """Interface implemented by one transport adapter."""

    name: str

    def capability(self) -> TransportCapability:
        """Describe supported roles and execution locations."""

    def build(
        self,
        spec: ForwardCreate,
        via_address: str,
        via_user: str | None,
        profile: ConnectionProfileRead | None = None,
    ) -> TransportPlan:
        """Build a command plan without executing it."""


def _role(
    spec: ForwardCreate, default: TransportRole, allowed: tuple[TransportRole, ...]
) -> TransportRole:
    selected = spec.role or default
    if selected not in allowed:
        values = ", ".join(item.value for item in allowed)
        raise ValueError(
            f"O transporte {spec.tool} não suporta o papel '{selected.value}'; "
            f"escolha um destes papéis: {values}."
        )
    return selected


def _execution(spec: ForwardCreate, allowed: tuple[ExecutionLocation, ...]) -> None:
    if spec.execution_location not in allowed:
        values = ", ".join(item.value for item in allowed)
        raise ValueError(
            f"O transporte {spec.tool} não pode executar em "
            f"'{spec.execution_location.value}'; locais aceitos: {values}."
        )


def _plan(
    adapter: str,
    spec: ForwardCreate,
    command: str,
    note: str,
    capabilities: tuple[str, ...],
    role: TransportRole,
) -> TransportPlan:
    return TransportPlan(
        adapter,
        command,
        note,
        capabilities,
        role.value,
        spec.execution_location.value,
        tuple(shell_split(command)),
    )


class SSHAdapter:
    """OpenSSH local, remote or dynamic forwarding adapter."""

    name = "ssh"

    def capability(self) -> TransportCapability:
        return TransportCapability(
            self.name,
            "SSH",
            (TransportRole.CLIENT.value,),
            (ExecutionLocation.MOTOR.value,),
            tuple(kind.value for kind in ForwardKind),
            ("local-forward", "remote-forward", "dynamic-socks"),
            ("ssh",),
            ("host key e autenticação continuam sujeitos à política SSH",),
        )

    def build(
        self,
        spec: ForwardCreate,
        via_address: str,
        via_user: str | None,
        profile: ConnectionProfileRead | None = None,
    ) -> TransportPlan:
        role = _role(spec, TransportRole.CLIENT, (TransportRole.CLIENT,))
        _execution(spec, (ExecutionLocation.MOTOR,))
        if profile is not None:
            via_address = profile.host
            via_user = profile.user
        target = f"{spec.user or via_user}@{via_address}" if spec.user or via_user else via_address
        reliability = [
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "BatchMode=yes",
        ]
        options = [*reliability]
        if profile is not None:
            options.extend(["-p", str(profile.port)])
            if profile.identity_file:
                options.extend(["-i", profile.identity_file])
            if profile.known_hosts_file:
                options.extend(["-o", f"UserKnownHostsFile={profile.known_hosts_file}"])
        if spec.kind == ForwardKind.DYNAMIC:
            mapping = f"{spec.local_address}:{spec.local_port}"
            command = shell_join(["ssh", "-N", *options, "-D", mapping, target])
        else:
            if not spec.target_address or spec.target_port is None:
                raise ValueError("Forward SSH exige target_address e target_port.")
            flag = {ForwardKind.LOCAL: "-L", ForwardKind.REMOTE: "-R"}[spec.kind]
            mapping = (
                f"{spec.local_address}:{spec.local_port}:"
                f"{spec.target_address}:{spec.target_port}"
            )
            command = shell_join(["ssh", "-N", *options, flag, mapping, target])
        return _plan(
            self.name,
            spec,
            command,
            "Confirme o host key e o destino antes de iniciar o processo.",
            ("local-forward", "remote-forward", "dynamic-socks"),
            role,
        )


class ChiselAdapter:
    """Chisel client adapter for reverse-capable local plans."""

    name = "chisel"

    def capability(self) -> TransportCapability:
        return TransportCapability(
            self.name,
            "Chisel",
            (TransportRole.CLIENT.value, TransportRole.SERVER.value),
            (ExecutionLocation.MOTOR.value,),
            (ForwardKind.LOCAL.value, ForwardKind.REMOTE.value, ForwardKind.DYNAMIC.value),
            ("tcp-forward", "dynamic-socks", "reverse-forward"),
            ("chisel",),
            ("cliente e servidor precisam usar endpoint e autenticação compatíveis",),
        )

    def build(
        self,
        spec: ForwardCreate,
        via_address: str,
        via_user: str | None,
        profile: ConnectionProfileRead | None = None,
    ) -> TransportPlan:
        del via_address, via_user, profile
        role = _role(spec, TransportRole.CLIENT, (TransportRole.CLIENT, TransportRole.SERVER))
        _execution(spec, (ExecutionLocation.MOTOR,))
        endpoint = spec.endpoint
        if not endpoint:
            raise ValueError("Forward Chisel exige endpoint explícito do servidor.")
        if role == TransportRole.SERVER:
            command_args = ["chisel", "server", "--port", endpoint, "--reverse"]
            if spec.kind == ForwardKind.DYNAMIC:
                command_args.append("--socks5")
            command = shell_join(command_args)
        elif spec.kind == ForwardKind.DYNAMIC:
            command = shell_join(["chisel", "client", endpoint, "socks"])
        else:
            if not spec.target_address or spec.target_port is None:
                raise ValueError("Forward Chisel exige target_address e target_port.")
            mapping = f"{spec.local_port}:{spec.target_address}:{spec.target_port}"
            if spec.kind == ForwardKind.REMOTE:
                mapping = f"R:{mapping}"
            command = shell_join(["chisel", "client", endpoint, mapping])
        return _plan(
            self.name,
            spec,
            command,
            "Use auth/fingerprint armazenados em referência segura; não cole segredo no workspace.",
            (
                "reverse-client",
                "dynamic-socks" if spec.kind == ForwardKind.DYNAMIC else "tcp-forward",
            ),
            role,
        )


class LigoloAdapter:
    """Ligolo-ng agent connection adapter."""

    name = "ligolo-ng"

    def capability(self) -> TransportCapability:
        return TransportCapability(
            self.name,
            "Ligolo-ng",
            (TransportRole.CLIENT.value, TransportRole.SERVER.value),
            (ExecutionLocation.REMOTE.value, ExecutionLocation.CONTEXT.value),
            (ForwardKind.LOCAL.value, ForwardKind.REMOTE.value, ForwardKind.DYNAMIC.value),
            ("control-channel", "routed-context"),
            ("ligolo-agent", "ligolo-proxy"),
            (
                "o agente deve ser colocado explicitamente no lado remoto;",
                "rotas só podem ser ativadas por um contexto roteado validado",
            ),
            True,
        )

    def build(
        self,
        spec: ForwardCreate,
        via_address: str,
        via_user: str | None,
        profile: ConnectionProfileRead | None = None,
    ) -> TransportPlan:
        del via_address, via_user, profile
        role = _role(spec, TransportRole.CLIENT, (TransportRole.CLIENT, TransportRole.SERVER))
        endpoint = spec.endpoint or "<proxy-host:11601>"
        if role == TransportRole.SERVER:
            command = ["ligolo-proxy", "-laddr", endpoint]
        else:
            command = ["ligolo-agent", "-connect", endpoint]
            if spec.fingerprint:
                command.extend(["-accept-fingerprint", spec.fingerprint])
        return _plan(
            self.name,
            spec,
            shell_join(command),
            "Depois da conexão, selecione o túnel e configure rotas no proxy Ligolo-ng.",
            ("control-channel", "routed-context"),
            role,
        )


class NetcatAdapter:
    """Single TCP-flow adapter; it deliberately cannot create a shell."""

    name = "nc"

    def capability(self) -> TransportCapability:
        return TransportCapability(
            self.name,
            "Netcat",
            (TransportRole.CLIENT.value, TransportRole.SERVER.value),
            (ExecutionLocation.MOTOR.value,),
            (ForwardKind.LOCAL.value, ForwardKind.DYNAMIC.value),
            ("single-tcp-flow",),
            ("nc", "ncat", "netcat"),
            ("um fluxo TCP não é um terminal nem um contexto roteado",),
        )

    def build(
        self,
        spec: ForwardCreate,
        via_address: str,
        via_user: str | None,
        profile: ConnectionProfileRead | None = None,
    ) -> TransportPlan:
        del via_address, via_user, profile
        default = TransportRole.SERVER if spec.kind == ForwardKind.DYNAMIC else TransportRole.CLIENT
        role = _role(spec, default, (TransportRole.CLIENT, TransportRole.SERVER))
        _execution(spec, (ExecutionLocation.MOTOR,))
        if role == TransportRole.SERVER:
            if spec.local_port < 1:
                raise ValueError("O servidor nc exige uma porta local explícita.")
            command = shell_join(["nc", "-l", "-v", "-p", str(spec.local_port)])
            return _plan(
                self.name,
                spec,
                command,
                "Recebe um único fluxo TCP; duplicar a visualização não cria outra shell.",
                ("single-tcp-flow",),
                role,
            )
        if not spec.target_address or spec.target_port is None:
            raise ValueError("Forward nc exige target_address e target_port.")
        command = shell_join(["nc", spec.target_address, str(spec.target_port)])
        return _plan(
            self.name,
            spec,
            command,
            "Fluxo TCP único sem execução remota ou persistência.",
            ("single-tcp-flow",),
            role,
        )


class GSocketAdapter:
    """Optional gsocket transport with explicit client/server role."""

    name = "gsocket"

    def capability(self) -> TransportCapability:
        return TransportCapability(
            self.name,
            "gsocket",
            (TransportRole.CLIENT.value, TransportRole.SERVER.value),
            (ExecutionLocation.MOTOR.value,),
            (ForwardKind.LOCAL.value, ForwardKind.DYNAMIC.value),
            ("single-tcp-flow",),
            ("gs-netcat",),
            ("ferramenta opcional; conexão estabelecida não equivale a uma shell",),
        )

    def build(
        self,
        spec: ForwardCreate,
        via_address: str,
        via_user: str | None,
        profile: ConnectionProfileRead | None = None,
    ) -> TransportPlan:
        del via_address, via_user, profile
        default = TransportRole.CLIENT if spec.endpoint else TransportRole.SERVER
        role = _role(spec, default, (TransportRole.CLIENT, TransportRole.SERVER))
        _execution(spec, (ExecutionLocation.MOTOR,))
        if role == TransportRole.CLIENT:
            if not spec.endpoint:
                raise ValueError("O cliente gsocket exige endpoint explícito.")
            command = shell_join(["gs-netcat", "-c", spec.endpoint])
        else:
            if spec.endpoint:
                raise ValueError("O servidor gsocket não aceita endpoint de cliente.")
            command = shell_join(["gs-netcat", "-s"])
        return _plan(
            self.name,
            spec,
            command,
            "gsocket é opcional e fornece um fluxo; não instala agente nem cria persistência.",
            ("single-tcp-flow",),
            role,
        )


def adapter_for(tool: str) -> PivotAdapter:
    """Resolve a supported adapter by its CLI tool name."""

    adapters: dict[str, PivotAdapter] = {
        "ssh": SSHAdapter(),
        "chisel": ChiselAdapter(),
        "ligolo": LigoloAdapter(),
        "ligolo-ng": LigoloAdapter(),
        "nc": NetcatAdapter(),
        "netcat": NetcatAdapter(),
        "gsocket": GSocketAdapter(),
        "gs-netcat": GSocketAdapter(),
    }
    try:
        return adapters[tool.lower()]
    except KeyError as error:
        raise ValueError(f"Ferramenta de pivot não suportada: {tool}") from error


def transport_capabilities() -> tuple[TransportCapability, ...]:
    """Return the stable adapter catalog used by API, CLI and UI."""

    return tuple(
        adapter_for(tool).capability() for tool in ("ssh", "chisel", "ligolo-ng", "nc", "gsocket")
    )
