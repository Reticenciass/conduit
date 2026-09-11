"""Typed, workspace-scoped operations for optional Linux network namespaces."""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        return {
            "available": self.available,
            "privileged": self.privileged,
            "socket_configured": self.socket_configured,
            "socket_available": self.socket_available,
            "socket_path": str(self.socket_path) if self.socket_path else None,
            "operations": sorted(self._ACTIONS),
            "transport": "unix-socket-or-local-runner",
        }

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
