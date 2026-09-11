"""Restricted Unix-socket protocol for the optional privileged namespace helper."""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path
from typing import Any

from ctfws.services.routed_helper import NamespaceApplyResult, NamespaceOperation

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
        if mode not in {"apply", "remove"}:
            raise ValueError("Modo de namespace inválido.")
        operations = self._decode_operations(payload.get("operations"))
        if mode == "apply":
            result: NamespaceApplyResult = self.helper.apply(operations)
            return {
                "version": self.VERSION,
                "ok": True,
                "namespace": result.namespace,
                "resources": list(result.resources),
            }
        self.helper.remove(operations)
        return {"version": self.VERSION, "ok": True}

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
