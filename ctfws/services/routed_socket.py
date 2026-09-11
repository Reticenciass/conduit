"""Restricted Unix-socket protocol for the optional privileged namespace helper."""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path
from typing import Any, cast

from ctfws.services.routed_helper import (
    NamespaceApplyResult,
    NamespaceOperation,
    RoutedCommandResult,
    RoutedCommandSpec,
    RoutedProxyResult,
    RoutedProxySpec,
    RoutedProxyStopSpec,
)

UNIX_FAMILY: int = getattr(socket, "AF_UNIX", 1)


class RoutedNamespaceSocketClient:
    """Send only typed namespace batches to a separately privileged helper."""

    VERSION = 1
    MAX_FRAME_BYTES = 64 * 1024

    def __init__(self, socket_path: Path, *, timeout: float = 5.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def apply(self, operations: tuple[NamespaceOperation, ...]) -> NamespaceApplyResult:
        """Apply one validated batch and return the helper-owned resources."""

        response = self._request("apply", operations)
        namespace = response.get("namespace")
        resources = response.get("resources")
        if not isinstance(namespace, str) or not isinstance(resources, list):
            raise RuntimeError("O helper retornou um manifesto de recursos inválido.")
        return NamespaceApplyResult(namespace, tuple(str(item) for item in resources))

    def remove(self, operations: tuple[NamespaceOperation, ...]) -> None:
        """Remove one validated batch in dependency-safe order."""

        self._request("remove", operations)

    def prepare_runtime(self, workspace_id: int, context_id: int) -> NamespaceApplyResult:
        """Create the isolated namespace and veth pair through the helper."""

        response = self._request_custom(
            {
                "version": self.VERSION,
                "mode": "runtime-prepare",
                "workspace_id": workspace_id,
                "context_id": context_id,
            }
        )
        namespace = response.get("namespace")
        resources = response.get("resources")
        if not isinstance(namespace, str) or not isinstance(resources, list):
            raise RuntimeError("O helper retornou um manifesto de runtime inválido.")
        return NamespaceApplyResult(namespace, tuple(str(item) for item in resources))

    def remove_runtime(
        self, workspace_id: int, context_id: int, resources: tuple[str, ...]
    ) -> None:
        """Remove one exact isolated runtime manifest."""

        self._request_custom(
            {
                "version": self.VERSION,
                "mode": "runtime-remove",
                "workspace_id": workspace_id,
                "context_id": context_id,
                "resources": list(resources),
            }
        )

    def start_proxy(self, spec: RoutedProxySpec) -> RoutedProxyResult:
        """Start the fixed Ligolo proxy through the root helper."""

        response = self._request_custom(
            {
                "version": self.VERSION,
                "mode": "proxy-start",
                "spec": {
                    "workspace_id": spec.workspace_id,
                    "context_id": spec.context_id,
                    "namespace": spec.namespace,
                    "namespace_address": spec.namespace_address,
                    "host_address": spec.host_address,
                    "proxy_port": spec.proxy_port,
                    "api_port": spec.api_port,
                    "proxy_path": spec.proxy_path,
                    "config_path": spec.config_path,
                    "cache_path": spec.cache_path,
                    "log_path": spec.log_path,
                    "api_password": spec.api_password,
                },
            }
        )
        try:
            raw_pid = response.get("pid")
            raw_started = response.get("process_started_at")
            raw_executable = response.get("executable")
            raw_fingerprint = response.get("command_fingerprint")
            raw_resources = response.get("resources")
            if (
                isinstance(raw_pid, bool)
                or not isinstance(raw_pid, int)
                or raw_started is not None
                and (isinstance(raw_started, bool) or not isinstance(raw_started, (int, float)))
                or raw_executable is not None
                and not isinstance(raw_executable, str)
                or raw_fingerprint is not None
                and not isinstance(raw_fingerprint, str)
                or not isinstance(raw_resources, list)
                or not all(isinstance(item, str) for item in raw_resources)
            ):
                raise ValueError
            return RoutedProxyResult(
                raw_pid,
                float(raw_started) if raw_started is not None else None,
                raw_executable,
                raw_fingerprint,
                tuple(raw_resources),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("O helper retornou uma identidade de proxy inválida.") from error

    def stop_proxy(self, spec: RoutedProxyStopSpec) -> None:
        """Stop a proxy through the root helper after identity validation."""

        self._request_custom(
            {
                "version": self.VERSION,
                "mode": "proxy-stop",
                "spec": {
                    "workspace_id": spec.workspace_id,
                    "context_id": spec.context_id,
                    "namespace": spec.namespace,
                    "pid": spec.pid,
                    "process_started_at": spec.process_started_at,
                    "executable": spec.executable,
                    "command_fingerprint": spec.command_fingerprint,
                },
            }
        )

    def execute_command(self, spec: RoutedCommandSpec) -> RoutedCommandResult:
        """Execute one allowlisted context tool through the helper worker."""

        response = self._request_custom(
            {
                "version": self.VERSION,
                "mode": "worker-run",
                "spec": {
                    "workspace_id": spec.workspace_id,
                    "context_id": spec.context_id,
                    "namespace": spec.namespace,
                    "program": spec.program,
                    "arguments": list(spec.arguments),
                    "timeout_seconds": spec.timeout_seconds,
                },
            }
        )
        output = response.get("output")
        returncode = response.get("returncode")
        timed_out = response.get("timed_out")
        truncated = response.get("output_truncated")
        if (
            not isinstance(output, str)
            or (
                returncode is not None
                and (isinstance(returncode, bool) or not isinstance(returncode, int))
            )
            or not isinstance(timed_out, bool)
            or not isinstance(truncated, bool)
        ):
            raise RuntimeError("O helper retornou um resultado de ferramenta inválido.")
        return RoutedCommandResult(returncode, timed_out, output, truncated)

    def _request(self, mode: str, operations: tuple[NamespaceOperation, ...]) -> dict[str, object]:
        if mode not in {"apply", "remove"}:
            raise ValueError("Modo de namespace inválido.")
        payload = {
            "version": self.VERSION,
            "mode": mode,
            "operations": [
                {
                    "workspace_id": operation.workspace_id,
                    "context_id": operation.context_id,
                    "action": operation.action,
                    "namespace": operation.namespace,
                    "cidr": operation.cidr,
                    "device": operation.device,
                }
                for operation in operations
            ],
        }
        return self._request_custom(payload)

    def _request_custom(self, payload: dict[str, object]) -> dict[str, object]:
        frame = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(frame) > self.MAX_FRAME_BYTES:
            raise ValueError("O lote de namespace excede o limite do protocolo local.")
        with socket.socket(UNIX_FAMILY, socket.SOCK_STREAM) as channel:
            channel.settimeout(self.timeout)
            try:
                channel.connect(str(self.socket_path))
                channel.sendall(frame)
                response_frame = self._receive_frame(channel)
            except OSError as error:
                raise RuntimeError(
                    f"Não foi possível contactar o helper de namespace: {error}"
                ) from error
        try:
            response = json.loads(response_frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("O helper retornou uma resposta inválida.") from error
        if not isinstance(response, dict) or response.get("version") != self.VERSION:
            raise RuntimeError("A versão do protocolo do helper não é compatível.")
        if response.get("ok") is not True:
            raise RuntimeError(str(response.get("message", "O helper recusou a operação.")))
        return response

    def _receive_frame(self, channel: socket.socket) -> bytes:
        received = bytearray()
        while len(received) <= self.MAX_FRAME_BYTES:
            chunk = channel.recv(4096)
            if not chunk:
                break
            received.extend(chunk)
            if b"\n" in chunk:
                frame, _separator, _remainder = bytes(received).partition(b"\n")
                return frame
        raise RuntimeError("O helper não encerrou a resposta dentro do limite do protocolo.")


class RoutedNamespaceSocketServer:
    """Serve the typed namespace protocol from a root-owned Unix socket."""

    VERSION = RoutedNamespaceSocketClient.VERSION
    MAX_FRAME_BYTES = RoutedNamespaceSocketClient.MAX_FRAME_BYTES
    MAX_OPERATIONS = 256

    def __init__(self, helper: Any, socket_path: Path | str) -> None:
        self.helper = helper
        self.socket_path = Path(socket_path).expanduser().resolve()
        self._server: socket.socket | None = None
        self._closed = False

    def serve_forever(self) -> None:
        """Accept requests until ``close`` is called or the process is interrupted."""

        self._prepare_socket()
        server = socket.socket(UNIX_FAMILY, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o660)
            server.listen(8)
            server.settimeout(1.0)
            self._server = server
            while not self._closed:
                try:
                    channel, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._closed:
                        break
                    raise
                with channel:
                    channel.settimeout(10.0)
                    self._serve_channel(channel)
        finally:
            self._server = None
            server.close()
            self._remove_bound_socket()

    def close(self) -> None:
        """Stop accepting requests and remove only this server's socket."""

        self._closed = True
        server = self._server
        if server is not None:
            server.close()

    def _serve_channel(self, channel: socket.socket) -> None:
        try:
            payload = self._decode_payload(self._receive_frame(channel))
            response = self._dispatch(payload)
        except Exception as error:  # The protocol must keep the helper alive after bad input.
            response = {
                "version": self.VERSION,
                "ok": False,
                "message": str(error)[:1000],
            }
        frame = (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(frame) > self.MAX_FRAME_BYTES:
            frame = (
                json.dumps(
                    {
                        "version": self.VERSION,
                        "ok": False,
                        "message": "Resposta do helper excedeu o limite do protocolo.",
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        channel.sendall(frame)

    def _dispatch(self, payload: dict[str, object]) -> dict[str, object]:
        if payload.get("version") != self.VERSION:
            raise ValueError("A versão do protocolo do helper não é compatível.")
        mode = payload.get("mode")
        if mode not in {
            "apply",
            "remove",
            "runtime-prepare",
            "runtime-remove",
            "proxy-start",
            "proxy-stop",
            "worker-run",
        }:
            raise ValueError("Modo de namespace inválido.")
        if mode == "runtime-prepare":
            _workspace_id, context_id = self._decode_context_identity(payload)
            runtime_result = self.helper.prepare_runtime(context_id)
            return {
                "version": self.VERSION,
                "ok": True,
                "namespace": runtime_result.namespace,
                "resources": list(runtime_result.resources),
            }
        if mode == "runtime-remove":
            workspace_id, context_id = self._decode_context_identity(payload)
            resources = payload.get("resources")
            if not isinstance(resources, list) or not all(
                isinstance(item, str) for item in resources
            ):
                raise ValueError("O manifesto de runtime é inválido.")
            # A fresh helper instance is workspace-bound, so the explicit ID
            # is checked before any network operation is attempted.
            if workspace_id != self.helper.workspace.lab.id:
                raise ValueError("A operação não pertence ao workspace ativo.")
            self.helper.remove_runtime(context_id, tuple(resources))
            return {"version": self.VERSION, "ok": True}
        if mode == "proxy-start":
            proxy_spec = self._decode_proxy_spec(payload.get("spec"))
            if proxy_spec.workspace_id != self.helper.workspace.lab.id:
                raise ValueError("O proxy não pertence ao workspace ativo.")
            proxy_result = self.helper.start_proxy(proxy_spec)
            return {
                "version": self.VERSION,
                "ok": True,
                "pid": proxy_result.pid,
                "process_started_at": proxy_result.process_started_at,
                "executable": proxy_result.executable,
                "command_fingerprint": proxy_result.command_fingerprint,
                "resources": list(proxy_result.resources),
            }
        if mode == "proxy-stop":
            stop_spec = self._decode_proxy_stop_spec(payload.get("spec"))
            self.helper.stop_proxy(stop_spec)
            return {"version": self.VERSION, "ok": True}
        if mode == "worker-run":
            spec = self._decode_command_spec(payload.get("spec"))
            result: RoutedCommandResult = self.helper.execute_command(spec)
            return {
                "version": self.VERSION,
                "ok": True,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "output": result.output,
                "output_truncated": result.output_truncated,
            }
        operations = self._decode_operations(payload.get("operations"))
        if mode == "apply":
            namespace_result: NamespaceApplyResult = self.helper.apply(operations)
            return {
                "version": self.VERSION,
                "ok": True,
                "namespace": namespace_result.namespace,
                "resources": list(namespace_result.resources),
            }
        self.helper.remove(operations)
        return {"version": self.VERSION, "ok": True}

    def _decode_context_identity(self, payload: dict[str, object]) -> tuple[int, int]:
        workspace_id = payload.get("workspace_id")
        context_id = payload.get("context_id")
        if (
            isinstance(workspace_id, bool)
            or not isinstance(workspace_id, int)
            or isinstance(context_id, bool)
            or not isinstance(context_id, int)
            or workspace_id < 1
            or context_id < 1
        ):
            raise ValueError("Workspace e contexto precisam ser inteiros positivos.")
        if workspace_id != self.helper.workspace.lab.id:
            raise ValueError("A operação não pertence ao workspace ativo.")
        return workspace_id, context_id

    @staticmethod
    def _decode_proxy_spec(value: object) -> RoutedProxySpec:
        if not isinstance(value, dict):
            raise ValueError("A especificação do proxy é inválida.")
        fields = (
            "workspace_id",
            "context_id",
            "namespace",
            "namespace_address",
            "host_address",
            "proxy_port",
            "api_port",
            "proxy_path",
            "config_path",
            "cache_path",
            "log_path",
            "api_password",
        )
        if set(value) != set(fields):
            raise ValueError("A especificação do proxy possui campos desconhecidos.")
        if any(not isinstance(value[field], str) for field in fields[2:5] + fields[7:]):
            raise ValueError("A especificação do proxy possui texto inválido.")
        if any(
            isinstance(value[field], bool) or not isinstance(value[field], int)
            for field in ("workspace_id", "context_id", "proxy_port", "api_port")
        ):
            raise ValueError("A especificação do proxy possui números inválidos.")
        return RoutedProxySpec(**cast(dict[str, Any], value))

    @staticmethod
    def _decode_proxy_stop_spec(value: object) -> RoutedProxyStopSpec:
        if not isinstance(value, dict):
            raise ValueError("A identidade de encerramento do proxy é inválida.")
        fields = (
            "workspace_id",
            "context_id",
            "namespace",
            "pid",
            "process_started_at",
            "executable",
            "command_fingerprint",
        )
        if set(value) != set(fields):
            raise ValueError("A identidade de encerramento possui campos desconhecidos.")
        if any(
            isinstance(value[field], bool) or not isinstance(value[field], int)
            for field in ("workspace_id", "context_id", "pid")
        ):
            raise ValueError("A identidade de encerramento possui IDs inválidos.")
        if not isinstance(value["namespace"], str):
            raise ValueError("O namespace de encerramento é inválido.")
        for field in ("executable", "command_fingerprint"):
            if value[field] is not None and not isinstance(value[field], str):
                raise ValueError("A identidade de encerramento possui texto inválido.")
        started_at = value["process_started_at"]
        if started_at is not None and (
            isinstance(started_at, bool) or not isinstance(started_at, (int, float))
        ):
            raise ValueError("A data de início do proxy é inválida.")
        return RoutedProxyStopSpec(**cast(dict[str, Any], value))

    @staticmethod
    def _decode_command_spec(value: object) -> RoutedCommandSpec:
        if not isinstance(value, dict):
            raise ValueError("A especificação da ferramenta é inválida.")
        fields = {
            "workspace_id",
            "context_id",
            "namespace",
            "program",
            "arguments",
            "timeout_seconds",
        }
        if set(value) != fields:
            raise ValueError("A especificação da ferramenta possui campos desconhecidos.")
        for field in ("workspace_id", "context_id", "timeout_seconds"):
            if isinstance(value[field], bool) or not isinstance(value[field], int):
                raise ValueError("A especificação da ferramenta possui números inválidos.")
        if not isinstance(value["namespace"], str) or not isinstance(value["program"], str):
            raise ValueError("A especificação da ferramenta possui texto inválido.")
        arguments = value["arguments"]
        if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
            raise ValueError("Os argumentos da ferramenta são inválidos.")
        return RoutedCommandSpec(
            workspace_id=int(value["workspace_id"]),
            context_id=int(value["context_id"]),
            namespace=value["namespace"],
            program=value["program"],
            arguments=tuple(arguments),
            timeout_seconds=int(value["timeout_seconds"]),
        )

    def _decode_payload(self, frame: bytes) -> dict[str, object]:
        try:
            payload = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("A requisição do helper não é JSON válido.") from error
        if not isinstance(payload, dict):
            raise ValueError("A requisição do helper precisa ser um objeto JSON.")
        return payload

    def _decode_operations(self, value: object) -> tuple[NamespaceOperation, ...]:
        if not isinstance(value, list) or not value:
            raise ValueError("A requisição precisa conter operações de namespace.")
        if len(value) > self.MAX_OPERATIONS:
            raise ValueError("A requisição excede o limite de operações de namespace.")
        operations: list[NamespaceOperation] = []
        allowed_keys = {
            "workspace_id",
            "context_id",
            "action",
            "namespace",
            "cidr",
            "device",
        }
        for item in value:
            if not isinstance(item, dict) or set(item) != allowed_keys:
                raise ValueError("Operação de namespace possui campos inválidos.")
            workspace_id = item["workspace_id"]
            context_id = item["context_id"]
            if (
                isinstance(workspace_id, bool)
                or not isinstance(workspace_id, int)
                or isinstance(context_id, bool)
                or not isinstance(context_id, int)
            ):
                raise ValueError("IDs da operação de namespace precisam ser inteiros.")
            text_fields = ("action", "namespace")
            if any(not isinstance(item[field], str) for field in text_fields):
                raise ValueError("Ação e namespace precisam ser texto.")
            if item["cidr"] is not None and not isinstance(item["cidr"], str):
                raise ValueError("CIDR inválido na operação de namespace.")
            if item["device"] is not None and not isinstance(item["device"], str):
                raise ValueError("Interface inválida na operação de namespace.")
            operations.append(
                NamespaceOperation(
                    workspace_id=workspace_id,
                    context_id=context_id,
                    action=item["action"],
                    namespace=item["namespace"],
                    cidr=item["cidr"],
                    device=item["device"],
                )
            )
        return tuple(operations)

    def _prepare_socket(self) -> None:
        if os.name == "nt":
            raise RuntimeError("O helper de namespace exige Linux e sockets Unix.")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.is_symlink():
            raise RuntimeError("O caminho do helper não pode ser um link simbólico.")
        if not self.socket_path.exists():
            return
        if not stat.S_ISSOCK(self.socket_path.stat().st_mode):
            raise RuntimeError("O caminho do helper já existe e não é um socket Unix.")
        probe = socket.socket(UNIX_FAMILY, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(self.socket_path))
        except OSError:
            self.socket_path.unlink()
        else:
            raise RuntimeError("Já existe um helper de namespace ativo neste socket.")
        finally:
            probe.close()

    def _remove_bound_socket(self) -> None:
        if self.socket_path.is_symlink() or not self.socket_path.exists():
            return
        try:
            if stat.S_ISSOCK(self.socket_path.stat().st_mode):
                self.socket_path.unlink()
        except OSError:
            return

    def _receive_frame(self, channel: socket.socket) -> bytes:
        received = bytearray()
        while len(received) <= self.MAX_FRAME_BYTES:
            chunk = channel.recv(4096)
            if not chunk:
                break
            received.extend(chunk)
            if b"\n" in chunk:
                frame, _separator, _remainder = bytes(received).partition(b"\n")
                return frame
        raise ValueError("A requisição do helper excedeu o limite do protocolo.")
