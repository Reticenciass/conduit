"""Button-driven Ligolo-ng runtime orchestration.

The Conduit motor owns the workflow while a restricted helper owns the few
privileged namespace operations. The API of Ligolo is experimental, so every
call is isolated behind this adapter and the pinned release is checked before
the first process is started.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import secrets
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctfws.pivot.manifest import ligolo_agent_for_architecture, ligolo_manifest_status
from ctfws.services.routed_helper import (
    RoutedNamespaceHelper,
    RoutedProxyResult,
    RoutedProxySpec,
    RoutedProxyStopSpec,
)


class LigoloAPIError(RuntimeError):
    """A stable operator-facing error from the pinned Ligolo API."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class LigoloAPIClient:
    """Small, dependency-free client for the pinned local Ligolo API."""

    AUTH_READY_TIMEOUT_SECONDS = 8.0

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.token: str | None = None

    async def login(self) -> None:
        deadline = time.monotonic() + self.AUTH_READY_TIMEOUT_SECONDS
        while True:
            try:
                result = await self._request(
                    "POST",
                    "/api/auth",
                    {"Username": self.username, "Password": self.password},
                    auth=False,
                )
                token = result.get("token")
                if not isinstance(token, str) or not token:
                    raise LigoloAPIError(502, "A API Ligolo não retornou uma sessão válida.")
                self.token = token
                return
            except (OSError, LigoloAPIError) as error:
                # The proxy opens its API listener before the web middleware
                # has necessarily completed. Retry only startup-like failures;
                # malformed responses and other HTTP errors remain fail-fast.
                if isinstance(error, LigoloAPIError) and error.status not in {401, 403, 500}:
                    raise
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.2)

    async def get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def post(self, path: str, payload: dict[str, object]) -> Any:
        return await self._request("POST", path, payload)

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        auth: bool = True,
    ) -> Any:
        if not path.startswith("/api/"):
            raise ValueError("O caminho da API Ligolo é inválido.")
        token = self.token

        def request() -> Any:
            body = (
                json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None
            )
            headers = {"Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if auth and token:
                headers["Authorization"] = f"Bearer {token}"
            connection = http.client.HTTPConnection(self.host, self.port, timeout=3)
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                raw = response.read(4 * 1024 * 1024)
                try:
                    data = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise LigoloAPIError(
                        response.status, "A API Ligolo retornou uma resposta inválida."
                    ) from error
                if response.status < 200 or response.status >= 300:
                    detail = data.get("error") if isinstance(data, dict) else None
                    raise LigoloAPIError(response.status, str(detail or f"HTTP {response.status}"))
                return data
            finally:
                connection.close()

        return await asyncio.to_thread(request)


@dataclass(slots=True)
class LigoloRuntimeSession:
    """Ephemeral handles which are intentionally not restored as active."""

    context_id: int
    connection: Any
    spec: RoutedProxySpec
    proxy: RoutedProxyResult
    proxy_fingerprint: str
    api: LigoloAPIClient
    forward_listener: Any
    agent_process: Any
    remote_agent_path: str
    agent_id: int
    interface_name: str
    api_password: str = field(repr=False)


class LigoloRuntime:
    """Prepare, start, verify and clean one isolated Ligolo context."""

    PROXY_PORT = 22401
    API_PORT = 22402
    AGENT_WAIT_SECONDS = 20
    API_WAIT_SECONDS = 8

    def __init__(self, workspace: Any, ssh_manager: Any, helper: RoutedNamespaceHelper) -> None:
        self.workspace = workspace
        self.ssh_manager = ssh_manager
        self.helper = helper

    async def start(self, context: Any) -> LigoloRuntimeSession:
        if context.connection_id is None:
            raise ValueError("Um contexto roteado exige uma conexão SSH.")
        if not context.network_cidrs:
            raise ValueError("Selecione ao menos uma rede para acessar.")
        manifest = ligolo_manifest_status()
        if not manifest.valid or not manifest.binary_verified or not manifest.proxy_path:
            raise RuntimeError(f"Ligolo indisponível: {manifest.reason}")
        if not self.helper.runtime_available:
            raise RuntimeError("O helper privilegiado do contexto roteado não está disponível.")
        connection = await self.ssh_manager.connect(context.connection_id)
        architecture = await self._remote_architecture(connection)
        agent_path, agent_reason = ligolo_agent_for_architecture(architecture)
        if agent_path is None:
            raise RuntimeError(f"Agente Ligolo indisponível: {agent_reason}")

        runtime_root = self.workspace.paths.root / "runtime" / "contexts" / str(context.id)
        config_path = runtime_root / "ligolo-proxy.yaml"
        cache_path = runtime_root / "ligolo-selfcerts"
        log_path = self.workspace.paths.root / "logs" / f"context-{context.id}-ligolo.log"
        namespace = self.helper.namespace_for(context.id)
        host_address, namespace_address = self.helper.runtime_addresses(context.id)
        password = secrets.token_urlsafe(32).replace("-", "A").replace("_", "B")
        spec = RoutedProxySpec(
            workspace_id=self.workspace.lab.id,
            context_id=context.id,
            namespace=namespace,
            namespace_address=namespace_address,
            host_address=host_address,
            proxy_port=self.PROXY_PORT,
            api_port=self.API_PORT,
            proxy_path=manifest.proxy_path,
            config_path=str(config_path),
            cache_path=str(cache_path),
            log_path=str(log_path),
            api_password=password,
        )
        prepared = False
        proxy: RoutedProxyResult | None = None
        listener: Any | None = None
        agent_process: Any | None = None
        remote_agent_path = f"/tmp/conduit-ligolo-{self.workspace.lab.id}-{context.id}.agent"
        try:
            resources = await asyncio.to_thread(self.helper.prepare_runtime, context.id)
            prepared = True
            if tuple(resources.resources) != self.helper.runtime_resources(context.id):
                raise RuntimeError("O helper retornou recursos diferentes do plano revisado.")
            proxy = await asyncio.to_thread(self.helper.start_proxy, spec)
            await self._wait_tcp(spec.namespace_address, spec.api_port, self.API_WAIT_SECONDS)
            fingerprint = await self._wait_fingerprint(cache_path, context.id)
            api = LigoloAPIClient(spec.namespace_address, spec.api_port, "conduit", password)
            await api.login()
            await self._upload_agent(connection, agent_path, remote_agent_path)
            listener = await connection.forward_remote_port(
                "127.0.0.1", 0, spec.namespace_address, spec.proxy_port
            )
            remote_port = int(listener.get_port())
            agent_command = shlex.join(
                [
                    remote_agent_path,
                    "-connect",
                    f"127.0.0.1:{remote_port}",
                    "-accept-fingerprint",
                    fingerprint,
                    "-retry",
                    "-reconnect",
                ]
            )
            process_kwargs: dict[str, object] = {}
            from ctfws.services.ssh import asyncssh

            if asyncssh is not None:
                process_kwargs = {
                    "stdin": asyncssh.DEVNULL,
                    "stdout": asyncssh.DEVNULL,
                    "stderr": asyncssh.DEVNULL,
                }
            agent_process = await connection.create_process(
                f"exec {agent_command}", **process_kwargs
            )
            agent_id = await self._wait_for_agent(api)
            interface_name = f"ctw{context.id:x}"[:15]
            await api.post("/api/v1/interfaces", {"Interface": interface_name})
            await api.post(
                "/api/v1/routes",
                {"Interface": interface_name, "Route": list(context.network_cidrs)},
            )
            await api.post(f"/api/v1/tunnel/{agent_id}", {"Interface": interface_name})
            await self._wait_for_tunnel(api, agent_id, interface_name)
            return LigoloRuntimeSession(
                context.id,
                connection,
                spec,
                proxy,
                fingerprint,
                api,
                listener,
                agent_process,
                remote_agent_path,
                agent_id,
                interface_name,
                password,
            )
        except Exception as error:
            try:
                await self.cleanup(
                    context.id,
                    connection,
                    spec,
                    proxy,
                    listener,
                    agent_process,
                    remote_agent_path,
                    prepared,
                )
            except Exception as cleanup_error:
                raise RuntimeError(f"{error}; limpeza pendente: {cleanup_error}") from error
            raise

    async def stop(self, session: LigoloRuntimeSession) -> None:
        await self.cleanup(
            session.context_id,
            session.connection,
            session.spec,
            session.proxy,
            session.forward_listener,
            session.agent_process,
            session.remote_agent_path,
            True,
        )

    async def cleanup(
        self,
        context_id: int,
        connection: Any | None,
        spec: RoutedProxySpec,
        proxy: RoutedProxyResult | None,
        listener: Any | None,
        agent_process: Any | None,
        remote_agent_path: str,
        prepared: bool,
    ) -> None:
        cleanup_errors: list[str] = []
        if agent_process is not None:
            try:
                agent_process.terminate()
                wait_closed = getattr(agent_process, "wait_closed", None)
                if wait_closed is not None:
                    result = wait_closed()
                    if hasattr(result, "__await__"):
                        await result
            except Exception as error:
                cleanup_errors.append(f"agente remoto: {error}")
        if connection is not None:
            try:
                await connection.run(
                    f"rm -f -- {shlex.quote(remote_agent_path)}", check=False, timeout=5
                )
            except Exception as error:
                cleanup_errors.append(f"agente temporário: {error}")
        if listener is not None:
            try:
                listener.close()
                wait_closed = getattr(listener, "wait_closed", None)
                if wait_closed is not None:
                    result = wait_closed()
                    if hasattr(result, "__await__"):
                        await result
            except Exception as error:
                cleanup_errors.append(f"encaminhamento SSH: {error}")
        if proxy is not None:
            try:
                await asyncio.to_thread(
                    self.helper.stop_proxy,
                    RoutedProxyStopSpec(
                        spec.workspace_id,
                        context_id,
                        spec.namespace,
                        proxy.pid,
                        proxy.process_started_at,
                        proxy.executable,
                        proxy.command_fingerprint,
                    ),
                )
            except Exception as error:
                cleanup_errors.append(f"proxy Ligolo: {error}")
        if prepared:
            try:
                await asyncio.to_thread(
                    self.helper.remove_runtime,
                    context_id,
                    self.helper.runtime_resources(context_id),
                )
            except Exception as error:
                cleanup_errors.append(f"namespace: {error}")
        Path(spec.config_path).unlink(missing_ok=True)
        try:
            Path(spec.cache_path).rmdir()
        except OSError:
            pass
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))

    async def _upload_agent(self, connection: Any, local_path: str, remote_path: str) -> None:
        sftp = await connection.start_sftp_client()
        try:
            await sftp.put(local_path, remote_path, preserve=False, follow_symlinks=False)
            await sftp.chmod(remote_path, 0o700)
        finally:
            close = getattr(sftp, "exit", None) or getattr(sftp, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    async def _remote_architecture(self, connection: Any) -> str:
        result = await connection.run("uname -m", check=False, timeout=8)
        if int(result.exit_status or 0) != 0:
            raise RuntimeError("Não foi possível identificar a arquitetura da máquina remota.")
        architecture = str(result.stdout).strip()
        if (
            not architecture
            or len(architecture) > 32
            or any(char.isspace() for char in architecture)
        ):
            raise RuntimeError("A máquina remota retornou uma arquitetura inválida.")
        return architecture

    async def _wait_for_agent(self, api: LigoloAPIClient) -> int:
        deadline = time.monotonic() + self.AGENT_WAIT_SECONDS
        while time.monotonic() < deadline:
            value = await api.get("/api/v1/agents")
            if isinstance(value, dict):
                for key, agent in value.items():
                    if isinstance(agent, dict):
                        try:
                            return int(key)
                        except (TypeError, ValueError):
                            continue
            await asyncio.sleep(0.25)
        raise RuntimeError("O agente Ligolo não apareceu no proxy dentro do tempo esperado.")

    async def _wait_for_tunnel(self, api: LigoloAPIClient, agent_id: int, interface: str) -> None:
        deadline = time.monotonic() + self.AGENT_WAIT_SECONDS
        while time.monotonic() < deadline:
            value = await api.get("/api/v1/agents")
            agent = value.get(str(agent_id)) if isinstance(value, dict) else None
            if (
                isinstance(agent, dict)
                and agent.get("Running") is True
                and agent.get("Interface") == interface
            ):
                return
            await asyncio.sleep(0.25)
        raise RuntimeError("O proxy não confirmou o túnel Ligolo nem a interface esperada.")

    async def _wait_tcp(self, host: str, port: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                reader, writer = await asyncio.open_connection(host, port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError as error:
                last_error = error
                await asyncio.sleep(0.1)
        raise RuntimeError(f"A API do proxy não abriu: {last_error}")

    async def _wait_fingerprint(self, cache_path: Path, context_id: int) -> str:
        cert_path = cache_path / f"conduit-{context_id}_cert"
        deadline = time.monotonic() + self.API_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                data = cert_path.read_bytes()
                from cryptography import x509
                from cryptography.hazmat.primitives import serialization

                certificate = x509.load_pem_x509_certificate(data)
                der = certificate.public_bytes(serialization.Encoding.DER)
                return hashlib.sha256(der).hexdigest()
            except (OSError, ValueError):
                await asyncio.sleep(0.1)
        raise RuntimeError("O proxy não publicou o certificado TLS do canal de controle.")
