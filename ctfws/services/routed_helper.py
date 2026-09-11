"""Typed, workspace-scoped operations for optional Linux network namespaces."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pragma: no cover - pwd only exists on POSIX.
    import pwd
except ImportError:  # pragma: no cover - exercised by Windows development.
    pwd = None  # type: ignore[assignment]

from ctfws.core.process import process_identity, process_matches
from ctfws.pivot.manifest import ligolo_manifest_status
from ctfws.services.workspace import WorkspaceService


@dataclass(frozen=True, slots=True)
class NamespaceOperation:
    """One allowlisted operation accepted by the privileged helper."""

    workspace_id: int
    context_id: int
    action: str
    namespace: str
    cidr: str | None = None
    device: str | None = None


@dataclass(frozen=True, slots=True)
class NamespaceApplyResult:
    """Resources that were created and can be removed precisely later."""

    namespace: str
    resources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutedProxySpec:
    """Typed request to prepare one isolated Ligolo proxy runtime."""

    workspace_id: int
    context_id: int
    namespace: str
    namespace_address: str
    host_address: str
    proxy_port: int
    api_port: int
    proxy_path: str
    config_path: str
    cache_path: str
    log_path: str
    api_password: str


@dataclass(frozen=True, slots=True)
class RoutedProxyResult:
    """Identity returned after an isolated proxy was started."""

    pid: int
    process_started_at: float | None
    executable: str | None
    command_fingerprint: str | None
    resources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutedProxyStopSpec:
    """Identity required before a proxy process can receive a signal."""

    workspace_id: int
    context_id: int
    namespace: str
    pid: int
    process_started_at: float | None
    executable: str | None
    command_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class RoutedCommandSpec:
    """One non-interactive tool invocation inside a routed context."""

    workspace_id: int
    context_id: int
    namespace: str
    program: str
    arguments: tuple[str, ...] = ()
    timeout_seconds: int = 60


@dataclass(frozen=True, slots=True)
class RoutedCommandResult:
    """Bounded result of one contextual tool invocation."""

    returncode: int | None
    timed_out: bool
    output: str
    output_truncated: bool


class RoutedNamespaceHelper:
    """Build and optionally execute only typed ``ip netns`` operations.

    The normal motor never runs this helper.  A future root-owned helper can
    use the same operation contract through a Unix socket; this local runner
    is useful for an explicitly configured single-user installation.
    """

    _NAMESPACE = re.compile(r"^ctfws-[0-9]+-[0-9]+$")
    _DEVICE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
    _ACTIONS = {"create", "delete", "route-add", "route-delete"}
    _APPLY_ACTIONS = {"create", "route-add"}
    _REMOVE_ACTIONS = {"delete", "route-delete"}
    _RUNTIME_ADDRESS_NETWORK = ipaddress.ip_network("169.254.0.0/16")
    _RUNTIME_NAMESPACE_INTERFACE = re.compile(r"^ctn[0-9a-f]{10}$")
    _RUNTIME_HOST_INTERFACE = re.compile(r"^cth[0-9a-f]{10}$")
    _RUNTIME_PATH_PARTS = {"runtime", "contexts"}
    _CONTEXT_PROGRAMS = {
        "curl",
        "dig",
        "ncat",
        "nc",
        "netcat",
        "nmap",
        "nslookup",
        "openssl",
        "wget",
    }

    def __init__(
        self,
        workspace: WorkspaceService,
        runner: Any = None,
        socket_path: Path | str | None = None,
    ) -> None:
        self.workspace = workspace
        self.runner = runner or subprocess
        configured_socket = socket_path or os.getenv("CTFWS_NAMESPACE_SOCKET")
        self.socket_path = (
            Path(configured_socket).expanduser().resolve() if configured_socket else None
        )

    @property
    def socket_configured(self) -> bool:
        return self.socket_path is not None

    @property
    def socket_available(self) -> bool:
        return self.socket_path is not None and os.name != "nt" and self.socket_path.exists()

    @property
    def available(self) -> bool:
        if self.socket_configured:
            return self.socket_available
        return os.name != "nt" and shutil.which("ip") is not None

    @property
    def privileged(self) -> bool:
        if self.socket_configured:
            # Filesystem ownership/mode of the root-owned socket is the trust boundary.
            return self.socket_available
        return os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0

    def capabilities(self) -> dict[str, object]:
        manifest = ligolo_manifest_status()
        proxy_path = manifest.proxy_path
        proxy_net_admin = False
        if proxy_path and os.name != "nt" and shutil.which("getcap"):
            try:
                result = subprocess.run(
                    ["getcap", proxy_path], capture_output=True, text=True, check=False
                )
                proxy_net_admin = "cap_net_admin" in result.stdout.casefold()
            except OSError:
                proxy_net_admin = False
        return {
            "available": self.available,
            "privileged": self.privileged,
            "socket_configured": self.socket_configured,
            "socket_available": self.socket_available,
            "socket_path": str(self.socket_path) if self.socket_path else None,
            "operations": sorted(self._ACTIONS),
            "transport": "unix-socket-or-local-runner",
            "runtime": {
                "available": self.runtime_available,
                "proxy_net_admin": proxy_net_admin,
                "requires": ["CAP_SYS_ADMIN", "CAP_NET_ADMIN", "setcap"],
                "network": str(self._RUNTIME_ADDRESS_NETWORK),
            },
        }

    @property
    def runtime_available(self) -> bool:
        """Whether this process can prepare the isolated proxy runtime.

        A socket-backed helper is only considered available when the helper
        service is reachable. The service itself validates the privileged
        operations and is the only component allowed to create the veth pair.
        """

        if self.socket_configured:
            return self.socket_available
        return self.available and self.privileged

    def runtime_interface_names(self, context_id: int) -> tuple[str, str]:
        """Return deterministic host and namespace veth names for a context."""

        if context_id < 1:
            raise ValueError("O contexto precisa ter um ID positivo.")
        # Lab ids and context ids are bounded by the database; the hexadecimal
        # suffix keeps names within Linux's 15-byte interface limit and avoids
        # collisions between decimal spellings.
        suffix = f"{self.workspace.lab.id:05x}{context_id:05x}"[-10:]
        return f"cth{suffix}", f"ctn{suffix}"

    def runtime_resources(self, context_id: int) -> tuple[str, ...]:
        """List the exact resources owned by the isolated runtime."""

        host_interface, namespace_interface = self.runtime_interface_names(context_id)
        namespace = self.namespace_for(context_id)
        host_address, namespace_address = self.runtime_addresses(context_id)
        return (
            f"namespace:{namespace}",
            f"veth-host:{host_interface}",
            f"veth-namespace:{namespace_interface}",
            f"address-host:{host_interface}:{host_address}/30",
            f"address-namespace:{namespace_interface}:{namespace_address}/30",
        )

    def runtime_addresses(self, context_id: int) -> tuple[str, str]:
        """Allocate one deterministic, non-overlapping /30 for a context."""

        if context_id < 1 or context_id > 16384:
            raise ValueError("O contexto excede o limite de enlaces isolados disponíveis.")
        index = context_id - 1
        third_octet, block = divmod(index, 64)
        fourth_octet = block * 4
        return (
            f"169.254.{third_octet}.{fourth_octet + 1}",
            f"169.254.{third_octet}.{fourth_octet + 2}",
        )

    def prepare_runtime(self, context_id: int) -> NamespaceApplyResult:
        """Create only the context-owned namespace and its host/namespace link."""

        if self.socket_configured:
            if not self.socket_available:
                raise RuntimeError("O socket do helper de namespace não está disponível.")
            from ctfws.services.routed_socket import RoutedNamespaceSocketClient

            assert self.socket_path is not None
            return RoutedNamespaceSocketClient(self.socket_path).prepare_runtime(
                self.workspace.lab.id, context_id
            )
        if not self.runtime_available:
            raise RuntimeError(
                "O helper roteado exige Linux, ip e privilégio CAP_SYS_ADMIN/CAP_NET_ADMIN."
            )
        namespace = self.namespace_for(context_id)
        host_interface, namespace_interface = self.runtime_interface_names(context_id)
        host_address, namespace_address = self.runtime_addresses(context_id)
        operations = (
            ["ip", "netns", "add", namespace],
            [
                "ip",
                "link",
                "add",
                host_interface,
                "type",
                "veth",
                "peer",
                "name",
                namespace_interface,
            ],
            ["ip", "link", "set", namespace_interface, "netns", namespace],
            ["ip", "addr", "add", f"{host_address}/30", "dev", host_interface],
            ["ip", "link", "set", host_interface, "up"],
            ["ip", "-n", namespace, "link", "set", "lo", "up"],
            [
                "ip",
                "-n",
                namespace,
                "addr",
                "add",
                f"{namespace_address}/30",
                "dev",
                namespace_interface,
            ],
            ["ip", "-n", namespace, "link", "set", namespace_interface, "up"],
        )
        created = False
        try:
            for command in operations:
                self.runner.run(command, check=True, capture_output=True)
                if command[:4] == ["ip", "netns", "add", namespace]:
                    created = True
        except Exception:
            if created:
                self.runner.run(
                    ["ip", "link", "delete", host_interface], check=False, capture_output=True
                )
                self.runner.run(
                    ["ip", "netns", "delete", namespace], check=False, capture_output=True
                )
            raise
        return NamespaceApplyResult(namespace, self.runtime_resources(context_id))

    def remove_runtime(self, context_id: int, resources: tuple[str, ...]) -> None:
        """Remove only resources previously returned by :meth:`prepare_runtime`."""

        expected = set(self.runtime_resources(context_id))
        if set(resources) != expected:
            raise ValueError("O manifesto do runtime roteado não corresponde ao contexto.")
        if self.socket_configured:
            if not self.socket_available:
                raise RuntimeError("O socket do helper de namespace não está disponível.")
            from ctfws.services.routed_socket import RoutedNamespaceSocketClient

            assert self.socket_path is not None
            RoutedNamespaceSocketClient(self.socket_path).remove_runtime(
                self.workspace.lab.id, context_id, resources
            )
            return
        if not self.runtime_available:
            raise RuntimeError("O helper roteado não está disponível para limpeza.")
        host_interface, _namespace_interface = self.runtime_interface_names(context_id)
        # Deleting the host side removes the veth peer; deleting the namespace
        # also removes any TUN left behind by a crashed proxy.
        self.runner.run(["ip", "link", "delete", host_interface], check=False, capture_output=True)
        self.runner.run(
            ["ip", "netns", "delete", self.namespace_for(context_id)],
            check=False,
            capture_output=True,
        )

    def start_proxy(self, spec: RoutedProxySpec) -> RoutedProxyResult:
        """Start a fixed Ligolo proxy inside a context-owned namespace."""

        self._validate_proxy_spec(spec)
        if self.socket_configured:
            if not self.socket_available:
                raise RuntimeError("O socket do helper de namespace não está disponível.")
            from ctfws.services.routed_socket import RoutedNamespaceSocketClient

            assert self.socket_path is not None
            return RoutedNamespaceSocketClient(self.socket_path).start_proxy(spec)
        return self._start_proxy_local(spec)

    def stop_proxy(self, spec: RoutedProxyStopSpec) -> None:
        """Stop a proxy only after its recorded identity has been verified."""

        self._validate_stop_spec(spec)
        if self.socket_configured:
            if not self.socket_available:
                raise RuntimeError("O socket do helper de namespace não está disponível.")
            from ctfws.services.routed_socket import RoutedNamespaceSocketClient

            assert self.socket_path is not None
            RoutedNamespaceSocketClient(self.socket_path).stop_proxy(spec)
            return
        self._stop_proxy_local(spec)

    def execute_command(self, spec: RoutedCommandSpec) -> RoutedCommandResult:
        """Run one allowlisted binary as the unprivileged context worker."""

        self._validate_command_spec(spec)
        if self.socket_configured:
            if not self.socket_available:
                raise RuntimeError("O socket do helper de namespace não está disponível.")
            from ctfws.services.routed_socket import RoutedNamespaceSocketClient

            assert self.socket_path is not None
            return RoutedNamespaceSocketClient(self.socket_path).execute_command(spec)
        if not self.runtime_available:
            raise RuntimeError("O helper roteado não está disponível para executar a ferramenta.")
        command = [
            sys.executable,
            "-m",
            "ctfws.routed_worker",
            "--namespace",
            spec.namespace,
            "--run-program",
            spec.program,
            "--run-arguments",
            *spec.arguments,
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            data, _ = process.communicate(timeout=spec.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)  # type: ignore[attr-defined]
            try:
                data, _ = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, getattr(signal, "SIGKILL", 9))  # type: ignore[attr-defined]
                data, _ = process.communicate(timeout=2)
        data = data or b""
        return RoutedCommandResult(
            process.returncode,
            timed_out,
            data[: 4 * 1024 * 1024].decode("utf-8", errors="replace"),
            len(data) > 4 * 1024 * 1024,
        )

    def _start_proxy_local(self, spec: RoutedProxySpec) -> RoutedProxyResult:
        runtime_dir = Path(spec.config_path).parent
        runtime_dir.mkdir(parents=True, exist_ok=True)
        Path(spec.cache_path).mkdir(parents=True, exist_ok=True)
        self._write_proxy_config(spec)
        log_handle = Path(spec.log_path).open("ab")
        try:
            command = [
                sys.executable,
                "-m",
                "ctfws.routed_worker",
                "--namespace",
                spec.namespace,
                "--proxy",
                spec.proxy_path,
                "--config",
                spec.config_path,
                "--selfcert-cache",
                spec.cache_path,
                "--selfcert-domain",
                f"conduit-{spec.context_id}",
                "--listen",
                f"{spec.namespace_address}:{spec.proxy_port}",
                "--api-listen",
                f"{spec.namespace_address}:{spec.api_port}",
            ]
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        time.sleep(0.15)
        identity = process_identity(process.pid)
        if process.poll() is not None or identity is None:
            detail = Path(spec.log_path).read_text(encoding="utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"O proxy Ligolo encerrou ao iniciar: {detail}")
        return RoutedProxyResult(
            process.pid,
            identity.create_time,
            identity.executable,
            identity.command_fingerprint,
            (f"proxy:{process.pid}",),
        )

    def _stop_proxy_local(self, spec: RoutedProxyStopSpec) -> None:
        identity = process_identity(spec.pid)
        if not process_matches(
            identity,
            create_time=spec.process_started_at,
            executable=spec.executable,
            command_fingerprint=spec.command_fingerprint,
        ):
            raise RuntimeError("A identidade do processo Ligolo não corresponde ao registro salvo.")
        try:
            kill_group = os.killpg  # type: ignore[attr-defined]
            kill_group(spec.pid, getattr(signal, "SIGTERM", 15))
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and process_identity(spec.pid) is not None:
            time.sleep(0.05)
        if process_identity(spec.pid) is not None:
            os.killpg(spec.pid, getattr(signal, "SIGKILL", 9))  # type: ignore[attr-defined]

    def _validate_proxy_spec(self, spec: RoutedProxySpec) -> None:
        if spec.workspace_id != self.workspace.lab.id:
            raise ValueError("O runtime não pertence ao workspace ativo.")
        if spec.namespace != self.namespace_for(spec.context_id):
            raise ValueError("O namespace do proxy não pertence ao contexto informado.")
        _host_address, expected_namespace_address = self.runtime_addresses(spec.context_id)
        expected_host_address, _namespace_address = self.runtime_addresses(spec.context_id)
        if spec.namespace_address != expected_namespace_address:
            raise ValueError("O endereço do namespace não pertence ao runtime Conduit.")
        if spec.host_address != expected_host_address:
            raise ValueError("O endereço do host não pertence ao runtime Conduit.")
        for port in (spec.proxy_port, spec.api_port):
            if not 1024 <= port <= 65535:
                raise ValueError("As portas internas do proxy precisam estar entre 1024 e 65535.")
        if spec.proxy_port == spec.api_port:
            raise ValueError("O proxy e a API precisam usar portas internas diferentes.")
        if not 24 <= len(spec.api_password) <= 256 or any(
            character.isspace() for character in spec.api_password
        ):
            raise ValueError("A credencial temporária do proxy é inválida.")
        manifest = ligolo_manifest_status()
        proxy_path = str(Path(spec.proxy_path).expanduser().resolve())
        if not manifest.valid or not manifest.binary_verified or manifest.proxy_path != proxy_path:
            raise RuntimeError(
                "O proxy Ligolo não corresponde ao manifesto fixado e verificado do Conduit."
            )
        root = self.workspace.paths.root.resolve()
        for value, label, base in (
            (spec.config_path, "configuração", root / "runtime"),
            (spec.cache_path, "cache", root / "runtime"),
            (spec.log_path, "log", root / "logs"),
        ):
            path = Path(value).expanduser().resolve()
            if not path.is_relative_to(base):
                raise ValueError(
                    f"O caminho de {label} precisa estar dentro do runtime do workspace."
                )

    def _validate_stop_spec(self, spec: RoutedProxyStopSpec) -> None:
        if spec.workspace_id != self.workspace.lab.id:
            raise ValueError("O processo não pertence ao workspace ativo.")
        if spec.namespace != self.namespace_for(spec.context_id) or spec.pid < 1:
            raise ValueError("A identidade do proxy não é válida para este contexto.")
        manifest = ligolo_manifest_status()
        if not manifest.valid or manifest.proxy_path != spec.executable:
            raise RuntimeError("O proxy registrado não corresponde ao binário fixado do Conduit.")

    def _validate_command_spec(self, spec: RoutedCommandSpec) -> None:
        if spec.workspace_id != self.workspace.lab.id:
            raise ValueError("A ferramenta não pertence ao workspace ativo.")
        if spec.namespace != self.namespace_for(spec.context_id):
            raise ValueError("O namespace da ferramenta não pertence ao contexto informado.")
        if spec.program.rsplit("/", 1)[-1] not in self._CONTEXT_PROGRAMS:
            raise ValueError(
                "Ferramenta não permitida neste contexto; escolha uma ação de rede suportada."
            )
        if not 1 <= spec.timeout_seconds <= 3600:
            raise ValueError("O tempo limite da ferramenta precisa estar entre 1 e 3600 segundos.")
        if len(spec.arguments) > 256:
            raise ValueError("A ferramenta recebeu argumentos demais.")
        for argument in spec.arguments:
            if not isinstance(argument, str) or len(argument) > 4096 or "\x00" in argument:
                raise ValueError("Um argumento da ferramenta é inválido.")

    def _write_proxy_config(self, spec: RoutedProxySpec) -> None:
        config = (
            "web:\n"
            "  enabled: true\n"
            "  enableui: false\n"
            f"  listen: {spec.namespace_address}:{spec.api_port}\n"
            "  users:\n"
            f"    conduit: {json.dumps(spec.api_password)}\n"
            "proxy:\n"
            f"  selfcertcache: {json.dumps(spec.cache_path)}\n"
            f"  historyfile: {json.dumps(str(Path(spec.config_path).with_suffix('.history')))}\n"
        )
        path = Path(spec.config_path)
        path.write_text(config, encoding="utf-8")
        try:
            path.chmod(0o600)
            if pwd is not None and hasattr(os, "chown"):
                getpwnam = pwd.getpwnam  # type: ignore[attr-defined]
                uid = getpwnam("ctfws").pw_uid
                gid = getpwnam("ctfws").pw_gid
                os.chown(path, uid, gid)
                for directory in (path.parent, Path(spec.cache_path)):
                    os.chown(directory, uid, gid)
        except (KeyError, OSError):
            # The development runner may not have a ctfws account. The
            # production helper service is required to have one.
            pass

    def namespace_for(self, context_id: int) -> str:
        if context_id < 1:
            raise ValueError("O contexto precisa ter um ID positivo.")
        return f"ctfws-{self.workspace.lab.id}-{context_id}"

    def plan(
        self,
        context_id: int,
        network_cidrs: tuple[str, ...],
        *,
        device: str | None = None,
    ) -> tuple[NamespaceOperation, ...]:
        namespace = self.namespace_for(context_id)
        if device is not None and not self._DEVICE.fullmatch(device):
            raise ValueError("Nome de interface inválido para o contexto roteado.")
        operations = [NamespaceOperation(self.workspace.lab.id, context_id, "create", namespace)]
        for value in network_cidrs:
            network = ipaddress.ip_network(value, strict=False)
            if network.prefixlen == 0:
                raise ValueError("Um contexto roteado não pode instalar a rota padrão.")
            operations.append(
                NamespaceOperation(
                    self.workspace.lab.id,
                    context_id,
                    "route-add",
                    namespace,
                    str(network),
                    device,
                )
            )
        return tuple(operations)

    def cleanup_plan(
        self,
        context_id: int,
        network_cidrs: tuple[str, ...],
        *,
        device: str | None = None,
    ) -> tuple[NamespaceOperation, ...]:
        """Create the exact reverse operation set for a context-owned namespace."""

        planned = self.plan(context_id, network_cidrs, device=device)
        namespace = planned[0].namespace
        routes = [
            NamespaceOperation(
                operation.workspace_id,
                operation.context_id,
                "route-delete",
                namespace,
                operation.cidr,
                operation.device,
            )
            for operation in reversed(planned[1:])
        ]
        return (*routes, NamespaceOperation(self.workspace.lab.id, context_id, "delete", namespace))

    def cleanup_from_manifest(
        self, context_id: int, resources: tuple[str, ...]
    ) -> tuple[NamespaceOperation, ...]:
        """Rebuild an exact cleanup batch from resources returned by the helper."""

        namespace = self.namespace_for(context_id)
        routes: list[NamespaceOperation] = []
        namespace_seen = False
        for resource in resources:
            if resource == f"namespace:{namespace}":
                namespace_seen = True
                continue
            prefix = f"route:{namespace}:"
            if not resource.startswith(prefix):
                raise ValueError("O manifesto do namespace contém um recurso desconhecido.")
            encoded_route = resource[len(prefix) :]
            cidr, separator, device = encoded_route.rpartition(":")
            if not separator or not cidr:
                raise ValueError("O manifesto do namespace contém uma rota inválida.")
            routes.append(
                NamespaceOperation(
                    self.workspace.lab.id,
                    context_id,
                    "route-delete",
                    namespace,
                    cidr,
                    None if device == "-" else device,
                )
            )
        if not namespace_seen:
            raise ValueError("O manifesto do namespace não contém o namespace principal.")
        routes.reverse()
        routes.append(NamespaceOperation(self.workspace.lab.id, context_id, "delete", namespace))
        return self.validate(tuple(routes), mode="remove")

    def validate(
        self,
        operations: tuple[NamespaceOperation, ...],
        *,
        mode: str = "apply",
    ) -> tuple[NamespaceOperation, ...]:
        """Validate a complete batch before any privileged command is run."""

        if mode not in {"apply", "remove"}:
            raise ValueError("Modo de validação de namespace inválido.")
        if not operations:
            raise ValueError("Nenhuma operação de namespace foi solicitada.")
        validated = tuple(self._validate(operation) for operation in operations)
        allowed = self._APPLY_ACTIONS if mode == "apply" else self._REMOVE_ACTIONS
        if any(operation.action not in allowed for operation in validated):
            raise ValueError(f"Lote de namespace inválido para o modo '{mode}'.")
        identity = (validated[0].workspace_id, validated[0].context_id, validated[0].namespace)
        if any(
            (operation.workspace_id, operation.context_id, operation.namespace) != identity
            for operation in validated
        ):
            raise ValueError("Todas as operações precisam pertencer ao mesmo contexto.")
        if len({self._resource_key(operation) for operation in validated}) != len(validated):
            raise ValueError("O lote contém recursos de namespace duplicados.")
        if mode == "apply":
            if (
                validated[0].action != "create"
                or sum(operation.action == "create" for operation in validated) != 1
            ):
                raise ValueError("O lote de aplicação precisa começar com um único create.")
        elif validated[-1].action != "delete":
            raise ValueError("O lote de remoção precisa terminar com delete.")
        return validated

    def apply(self, operations: tuple[NamespaceOperation, ...]) -> NamespaceApplyResult:
        validated = self.validate(operations, mode="apply")
        if self.socket_configured:
            if not self.socket_available:
                raise RuntimeError("O socket do helper de namespace não está disponível.")
            from ctfws.services.routed_socket import RoutedNamespaceSocketClient

            assert self.socket_path is not None
            return RoutedNamespaceSocketClient(self.socket_path).apply(validated)
        if not self.available:
            raise RuntimeError("O helper roteado exige Linux com o binário ip instalado.")
        if not self.privileged:
            raise PermissionError("O helper roteado precisa ser executado com privilégio restrito.")
        created: list[str] = []
        try:
            for operation in validated:
                self.runner.run(self._command(operation), check=True, capture_output=True)
                if operation.action == "create":
                    created.append(operation.namespace)
        except Exception:
            for namespace in reversed(created):
                self.runner.run(
                    ["ip", "netns", "delete", namespace], check=False, capture_output=True
                )
            raise
        namespace = validated[0].namespace
        resources = tuple(
            self._resource_key(operation)
            for operation in validated
            if operation.action in {"create", "route-add"}
        )
        return NamespaceApplyResult(namespace, resources)

    def remove(self, operations: tuple[NamespaceOperation, ...]) -> None:
        """Remove resources in reverse dependency order after validation."""

        validated = self.validate(operations, mode="remove")
        if self.socket_configured:
            if not self.socket_available:
                raise RuntimeError("O socket do helper de namespace não está disponível.")
            from ctfws.services.routed_socket import RoutedNamespaceSocketClient

            assert self.socket_path is not None
            RoutedNamespaceSocketClient(self.socket_path).remove(validated)
            return
        if not self.available:
            raise RuntimeError("O helper roteado exige Linux com o binário ip instalado.")
        if not self.privileged:
            raise PermissionError("O helper roteado precisa ser executado com privilégio restrito.")
        for operation in validated:
            self.runner.run(self._command(operation), check=False, capture_output=True)

    def _validate(self, operation: NamespaceOperation) -> NamespaceOperation:
        if operation.workspace_id != self.workspace.lab.id:
            raise ValueError("A operação não pertence ao workspace ativo.")
        if operation.context_id < 1 or operation.action not in self._ACTIONS:
            raise ValueError("Operação de namespace não permitida.")
        expected_namespace = self.namespace_for(operation.context_id)
        if operation.namespace != expected_namespace or not self._NAMESPACE.fullmatch(
            operation.namespace
        ):
            raise ValueError("Namespace não pertence ao contexto informado.")
        if operation.action.startswith("route-"):
            if operation.cidr is None:
                raise ValueError("Operação de rota exige CIDR.")
            network = ipaddress.ip_network(operation.cidr, strict=False)
            if network.prefixlen == 0:
                raise ValueError("A rota padrão não pode ser alterada pelo helper.")
            if operation.device is not None and not self._DEVICE.fullmatch(operation.device):
                raise ValueError("Nome de interface inválido para o helper.")
        elif operation.cidr is not None or operation.device is not None:
            raise ValueError("Create/delete de namespace não aceita parâmetros de rota.")
        return operation

    @staticmethod
    def _command(operation: NamespaceOperation) -> list[str]:
        if operation.action == "create":
            return ["ip", "netns", "add", operation.namespace]
        if operation.action == "delete":
            return ["ip", "netns", "delete", operation.namespace]
        verb = "replace" if operation.action == "route-add" else "del"
        command = ["ip", "-n", operation.namespace, "route", verb, str(operation.cidr)]
        if operation.device is not None:
            command.extend(["dev", operation.device])
        return command

    @staticmethod
    def _resource_key(operation: NamespaceOperation) -> str:
        if operation.action == "create":
            return f"namespace:{operation.namespace}"
        return f"route:{operation.namespace}:{operation.cidr}:{operation.device or '-'}"
