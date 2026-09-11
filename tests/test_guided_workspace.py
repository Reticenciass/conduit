"""Tests for the guided connection and managed-terminal workflow."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from pathlib import Path

import pytest

from ctfws.models.connection import ConnectionProfileCreate
from ctfws.models.context import ContextStatus, ContextTransport, NetworkContextCreate
from ctfws.models.forward import ForwardCreate, ForwardKind, ForwardStatus
from ctfws.models.host import HostCreate
from ctfws.models.terminal import TerminalCreate, TerminalKind, TerminalSharing, TerminalStatus
from ctfws.models.tool import ToolCreate
from ctfws.models.transfer import ConflictPolicy
from ctfws.services.connections import ConnectionService, classify_ssh_error
from ctfws.services.contexts import NetworkContextService
from ctfws.services.inspection import RemoteInspectionService
from ctfws.services.pivot import PivotService
from ctfws.services.processes import ForwardProcessService
from ctfws.services.ssh import AsyncSSHConnectionManager
from ctfws.services.terminals import TerminalManager
from ctfws.services.tools import ToolCatalogService
from ctfws.services.transfers import SFTPTransferService


def test_parse_ssh_accepts_safe_connection_options() -> None:
    parsed = ConnectionService.parse_ssh("ssh -p 2222 -i ~/.ssh/id_ed25519 kali@192.0.2.139")
    assert parsed.host == "192.0.2.139"
    assert parsed.user == "kali"
    assert parsed.port == 2222
    assert parsed.identity_file == "~/.ssh/id_ed25519"
    assert ConnectionService.parse_ssh("ssh -l analyst host").user == "analyst"


def test_parse_ssh_preserves_proxyjump_as_typed_data(workspace) -> None:
    jump = workspace.connections.create(
        ConnectionProfileCreate(name="bastion", host="192.0.2.10", user="kali", port=2200)
    )
    parsed = ConnectionService.parse_ssh("ssh -J kali@192.0.2.10:2200 analyst@10.0.0.20")
    assert parsed.jump_targets[0].host == "192.0.2.10"
    assert parsed.jump_targets[0].user == "kali"
    assert parsed.jump_targets[0].port == 2200
    assert ConnectionService(workspace).resolve_jump_profile_ids(parsed) == ((jump.id,), ())


def test_parse_ssh_rejects_unknown_o_option_instead_of_ignoring_it() -> None:
    with pytest.raises(ValueError, match="não suportada"):
        ConnectionService.parse_ssh("ssh -o StrictHostKeyChecking=no analyst@host")


def test_parse_ssh_rejects_command_execution_options() -> None:
    try:
        ConnectionService.parse_ssh("ssh -o ProxyCommand=sh%20-c%20bad kali@host")
    except ValueError as error:
        assert "ProxyCommand" in str(error)
    else:
        raise AssertionError("ProxyCommand deveria ser rejeitado")


def test_ssh_error_categories_are_stable() -> None:
    assert classify_ssh_error("REMOTE HOST IDENTIFICATION HAS CHANGED") == "host_key_changed"
    assert classify_ssh_error("Host key is not trusted") == "host_key_unknown"
    assert classify_ssh_error("Permission denied (publickey,password)") == "credential_rejected"
    assert classify_ssh_error("Connection timed out") == "host_unavailable"


def test_ssh_error_redacts_temporary_secret() -> None:
    safe = AsyncSSHConnectionManager._safe_error(
        RuntimeError("authentication failed for temporary-secret"),
        secret="temporary-secret",
    )
    assert "temporary-secret" not in safe
    assert "<redacted>" in safe


def test_managed_local_terminal_has_independent_record(workspace) -> None:
    manager = TerminalManager(workspace)
    terminal = manager.start(TerminalCreate(name="local-test", kind=TerminalKind.LOCAL))
    assert terminal.status == TerminalStatus.ACTIVE
    manager.write(terminal.id, b"echo ctfws-test\n")

    output = b""
    deadline = time.time() + 3
    while b"ctfws-test" not in output and time.time() < deadline:
        output += manager.read(terminal.id, timeout=0.1) or b""
    assert b"ctfws-test" in output
    closed = manager.close(terminal.id)
    assert closed.status == TerminalStatus.CLOSED


def test_connection_profile_does_not_store_secret(workspace) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(
            name="jump",
            host="192.0.2.10",
            user="kali",
            auth_ref="ssh-agent",
        )
    )
    assert profile.auth_ref == "ssh-agent"
    with workspace.database.connection() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(connection_profiles)").fetchall()
        }
    assert "password" not in columns


def test_connection_auth_method_is_canonical_and_validated(workspace) -> None:
    service = ConnectionService(workspace)
    profile = service.add(
        ConnectionProfileCreate(
            name="passphrase-alias",
            host="192.0.2.14",
            user="kali",
            auth_method="passphrase",
        )
    )
    assert profile.auth_method == "key_passphrase"
    with pytest.raises(ValueError, match="Método de autenticação SSH inválido"):
        service.add(
            ConnectionProfileCreate(
                name="invalid-auth",
                host="192.0.2.15",
                user="kali",
                auth_method="not-supported",
            )
        )


def test_asyncssh_manager_reuses_connection_and_records_generation(workspace, monkeypatch) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="jump", host="192.0.2.10", user="kali")
    )
    calls: list[dict[str, object]] = []

    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    class FakeAsyncSSH:
        async def connect(self, **kwargs: object) -> FakeConnection:
            calls.append(kwargs)
            return FakeConnection()

    monkeypatch.setattr("ctfws.services.ssh.asyncssh", FakeAsyncSSH())
    manager = AsyncSSHConnectionManager(workspace)

    async def exercise() -> None:
        first = await manager.connect(profile.id, password="temporary")
        second = await manager.connect(profile.id, password="should-not-reconnect")
        assert first is second
        await manager.close(profile.id)

    asyncio.run(exercise())
    assert len(calls) == 1
    assert calls[0]["password"] == "temporary"
    current = workspace.connections.get(profile.id)
    assert current is not None
    assert current.generation == 1
    assert "sftp" in current.capabilities
    assert current.state.value == "disconnected"


def test_asyncssh_manager_concurrent_connect_waiters_receive_connection(
    workspace, monkeypatch
) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="concurrent", host="192.0.2.15", user="analyst")
    )
    calls = 0
    release = asyncio.Event()

    class FakeConnection:
        def is_closed(self) -> bool:
            return False

    class FakeAsyncSSH:
        async def connect(self, **_kwargs: object) -> FakeConnection:
            nonlocal calls
            calls += 1
            await release.wait()
            return FakeConnection()

    monkeypatch.setattr("ctfws.services.ssh.asyncssh", FakeAsyncSSH())
    manager = AsyncSSHConnectionManager(workspace)

    async def exercise() -> None:
        first_task = asyncio.create_task(manager.connect(profile.id))
        await asyncio.sleep(0)
        second_task = asyncio.create_task(manager.connect(profile.id))
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        first, second = await asyncio.gather(first_task, second_task)
        assert first is second

    asyncio.run(exercise())
    assert calls == 1


def test_asyncssh_manager_resolves_explicit_vault_reference_in_memory(
    workspace, monkeypatch
) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(
            name="vault-profile",
            host="192.0.2.11",
            user="kali",
            auth_ref="vault:ssh-test",
        )
    )
    calls: list[dict[str, object]] = []

    class FakeConnection:
        def is_closed(self) -> bool:
            return False

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeAsyncSSH:
        async def connect(self, **kwargs: object) -> FakeConnection:
            calls.append(kwargs)
            return FakeConnection()

    monkeypatch.setattr("ctfws.services.ssh.asyncssh", FakeAsyncSSH())
    manager = AsyncSSHConnectionManager(workspace, secret_resolver=lambda _ref: "in-memory-secret")
    asyncio.run(manager.connect(profile.id))
    assert calls[0]["password"] == "in-memory-secret"
    assert profile.auth_ref not in str(calls[0])


def test_asyncssh_manager_uses_vault_secret_as_key_passphrase(workspace, monkeypatch) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(
            name="encrypted-key",
            host="192.0.2.13",
            user="kali",
            identity_file="~/.ssh/id_ed25519",
            auth_ref="vault:key-passphrase",
            auth_method="key_passphrase",
        )
    )
    calls: list[dict[str, object]] = []

    class FakeConnection:
        def is_closed(self) -> bool:
            return False

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeAsyncSSH:
        async def connect(self, **kwargs: object) -> FakeConnection:
            calls.append(kwargs)
            return FakeConnection()

    monkeypatch.setattr("ctfws.services.ssh.asyncssh", FakeAsyncSSH())
    manager = AsyncSSHConnectionManager(
        workspace, secret_resolver=lambda _ref: "key-passphrase-value"
    )
    asyncio.run(manager.connect(profile.id))
    assert calls[0]["passphrase"] == "key-passphrase-value"
    assert "password" not in calls[0]


def test_asyncssh_jump_chain_keeps_credentials_scoped_to_each_profile(
    workspace, monkeypatch
) -> None:
    jump = workspace.connections.create(
        ConnectionProfileCreate(
            name="jump",
            host="192.0.2.12",
            user="jump-user",
            auth_ref="vault:jump-secret",
        )
    )
    target = workspace.connections.create(
        ConnectionProfileCreate(
            name="target",
            host="198.51.100.12",
            user="target-user",
            jump_profile_ids=(jump.id,),
        )
    )
    calls: list[dict[str, object]] = []

    class FakeConnection:
        def is_closed(self) -> bool:
            return False

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeAsyncSSH:
        async def connect(self, **kwargs: object) -> FakeConnection:
            calls.append(kwargs)
            return FakeConnection()

    monkeypatch.setattr("ctfws.services.ssh.asyncssh", FakeAsyncSSH())
    manager = AsyncSSHConnectionManager(workspace, secret_resolver=lambda ref: f"secret:{ref}")
    asyncio.run(manager.connect(target.id, password="target-temporary"))

    assert calls[0]["password"] == "secret:vault:jump-secret"
    assert calls[1]["password"] == "target-temporary"


def test_asyncssh_close_degrades_owned_dependents(workspace, monkeypatch) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="jump", host="192.0.2.10", user="kali")
    )
    host = workspace.add_host(HostCreate(name="jump", ip="192.0.2.10", user="kali"))
    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="owned-forward",
            via_host_id=host.id,
            connection_id=profile.id,
            local_port=0,
            target_address="10.20.0.5",
            target_port=80,
        )
    )
    workspace.forwards.set_process(forward.id, None, ForwardStatus.ACTIVE, health="process_running")
    context = NetworkContextService(workspace).plan(
        NetworkContextCreate(
            name="owned-socks",
            transport=ContextTransport.SOCKS,
            connection_id=profile.id,
            network_cidrs=("10.20.0.0/24",),
        )
    )
    workspace.contexts.update_runtime(context.id, status=ContextStatus.ACTIVE)

    class FakeConnection:
        def is_closed(self) -> bool:
            return False

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeAsyncSSH:
        async def connect(self, **_kwargs: object) -> FakeConnection:
            return FakeConnection()

    monkeypatch.setattr("ctfws.services.ssh.asyncssh", FakeAsyncSSH())
    manager = AsyncSSHConnectionManager(workspace)

    async def exercise() -> None:
        await manager.connect(profile.id)
        await manager.close(profile.id)

    asyncio.run(exercise())
    assert workspace.forwards.get(forward.id).status == ForwardStatus.DEGRADED  # type: ignore[union-attr]
    assert workspace.contexts.get(context.id).status == ContextStatus.DEGRADED  # type: ignore[union-attr]


def test_asyncssh_remote_terminal_uses_shared_pty_and_sequence(workspace, monkeypatch) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="remote", host="192.0.2.30", user="analyst")
    )
    calls: list[dict[str, object]] = []

    class FakeStream:
        def __init__(self) -> None:
            self.chunks = [b"remote-output\r\n", b""]

        async def read(self, _size: int) -> bytes:
            return self.chunks.pop(0)

    class FakeWriter:
        def __init__(self) -> None:
            self.data: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.data.append(data)

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = FakeStream()
            self.stdin = FakeWriter()
            self.exit_status = 0
            self.sizes: list[tuple[int, int]] = []
            self.closed = False

        def change_terminal_size(self, columns: int, rows: int) -> None:
            self.sizes.append((columns, rows))

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    process = FakeProcess()

    class FakeConnection:
        def is_closed(self) -> bool:
            return False

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

        async def create_process(self, **kwargs: object) -> FakeProcess:
            calls.append(kwargs)
            return process

    class FakeAsyncSSH:
        async def connect(self, **_kwargs: object) -> FakeConnection:
            return FakeConnection()

    monkeypatch.setattr("ctfws.services.ssh.asyncssh", FakeAsyncSSH())
    ssh = AsyncSSHConnectionManager(workspace)
    terminals = TerminalManager(workspace, ssh)

    async def exercise() -> None:
        terminal = await terminals.start_async(
            TerminalCreate(name="remote-pty", kind=TerminalKind.SSH, connection_id=profile.id)
        )
        token = terminals.subscribe(terminal.id)
        terminals.acquire_control(terminal.id, token)
        terminals.write(terminal.id, b"id\n", owner=token)
        terminals.resize(terminal.id, 120, 40)
        frame = await asyncio.to_thread(terminals.read_subscriber_frame, terminal.id, token, 1)
        assert frame is not None
        assert frame.sequence == 1
        assert frame.data == b"remote-output\r\n"
        await asyncio.sleep(0)

    asyncio.run(exercise())
    assert calls == [{"term_type": "xterm", "term_size": (80, 24), "encoding": None}]
    assert process.stdin.data == [b"id\n"]
    assert process.sizes == [(120, 40)]
    current = workspace.terminals.get(1)
    assert current is not None
    assert current.status == TerminalStatus.EXITED


def test_asyncssh_forward_owns_listener_and_closes_only_it(workspace) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="forward", host="192.0.2.50", user="analyst")
    )
    host = workspace.add_host(HostCreate(name="forward", ip="192.0.2.50"))

    class FakeListener:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    listener = FakeListener()

    class FakeConnection:
        async def forward_local_port(self, *_args: object, **_kwargs: object) -> FakeListener:
            return listener

    class FakeSSH:
        available = True

        async def connect(self, _profile_id: int) -> FakeConnection:
            return FakeConnection()

    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="async-forward",
            via_host_id=host.id,
            connection_id=profile.id,
            local_port=0,
            target_address="10.20.0.5",
            target_port=80,
        )
    )
    service = ForwardProcessService(workspace, FakeSSH())

    async def exercise() -> None:
        started = await service.start_async(forward.id)
        assert started.status == ForwardStatus.ACTIVE
        assert started.pid is None
        assert started.engine_id == workspace.engine_id
        assert started.listener_state == "open"
        checked = await service.check_async(forward.id)
        assert checked.health == "listener_open;destination_unprobed"
        stopped = await service.stop_async(forward.id)
        assert stopped.status == ForwardStatus.STOPPED
        assert stopped.engine_id is None
        assert forward.local_port is not None
        occupied = socket.socket()
        try:
            occupied.bind((forward.local_address, forward.local_port))
        finally:
            occupied.close()

    asyncio.run(exercise())
    assert listener.closed is True


def test_asyncssh_socks_context_owns_listener_and_preserves_network_scope(workspace) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="socks", host="192.0.2.60", user="analyst")
    )

    class FakeListener:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            return None

    listener = FakeListener()

    class FakeConnection:
        async def forward_socks(self, _address: str, _port: int | None) -> FakeListener:
            return listener

    class FakeSSH:
        available = True

        async def connect(self, _profile_id: int) -> FakeConnection:
            return FakeConnection()

    service = NetworkContextService(workspace, FakeSSH())
    context = service.plan(
        NetworkContextCreate(
            name="scoped-socks",
            transport=ContextTransport.SOCKS,
            connection_id=profile.id,
            network_cidrs=("10.30.0.0/24",),
        )
    )

    async def exercise() -> None:
        started = await service.start_async(context.id)
        assert started.status == ContextStatus.ACTIVE
        assert started.pid is None
        assert started.network_cidrs == ("10.30.0.0/24",)
        assert started.health == "listener_open;scope_unprobed"
        assert started.capabilities == ("socks", "tcp")
        assert started.engine_id == workspace.engine_id
        stopped = await service.stop_async(context.id)
        assert stopped.status == ContextStatus.STOPPED
        assert stopped.health == "stopped"
        assert stopped.capabilities == ()
        assert stopped.engine_id is None

    asyncio.run(exercise())
    assert listener.closed is True


def test_async_forward_start_is_serialized_per_resource(workspace) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="serialized", host="192.0.2.61", user="analyst")
    )
    host = workspace.add_host(HostCreate(name="serialized-host", ip="192.0.2.61"))
    calls = 0

    class FakeListener:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    listener = FakeListener()

    class FakeConnection:
        async def forward_local_port(self, *_args: object, **_kwargs: object) -> FakeListener:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return listener

    class FakeSSH:
        available = True

        async def connect(self, _profile_id: int) -> FakeConnection:
            return FakeConnection()

    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="serialized-forward",
            via_host_id=host.id,
            connection_id=profile.id,
            local_port=0,
            target_address="198.51.100.61",
            target_port=80,
        )
    )
    service = ForwardProcessService(workspace, FakeSSH())

    async def exercise() -> None:
        first, second = await asyncio.gather(
            service.start_async(forward.id), service.start_async(forward.id)
        )
        assert first.status == ForwardStatus.ACTIVE
        assert second.status == ForwardStatus.ACTIVE
        assert calls == 1
        await service.stop_async(forward.id)

    asyncio.run(exercise())


def test_web_api_manages_connection_profiles_and_local_terminal(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    client = TestClient(create_app(workspace.paths))
    response = client.post(
        "/api/v1/connections",
        json={"name": "jump", "host": "192.0.2.10", "user": "kali"},
    )
    assert response.status_code == 201
    profile_id = response.json()["id"]

    parsed = client.post("/api/v1/connections/parse", json={"ssh": "ssh kali@192.0.2.10 -p 2222"})
    assert parsed.status_code == 200
    assert parsed.json()["port"] == 2222

    terminal = client.post("/api/v1/terminals", json={"name": "local-api", "kind": "local"})
    assert terminal.status_code == 201
    assert terminal.json()["context_label"].startswith("LOCAL →")
    assert terminal.json()["engine_id"]
    terminal_id = terminal.json()["id"]
    renamed = client.patch(
        f"/api/v1/terminals/{terminal_id}/rename",
        json={"name": "terminal-principal"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "terminal-principal"
    with client.websocket_connect(f"/api/v1/terminals/{terminal_id}/stream") as socket:
        socket.send_text("echo websocket-test\n")
        output = b""
        for _ in range(20):
            message = socket.receive()
            if message.get("bytes"):
                output += message["bytes"]
            if b"websocket-test" in output:
                break
        assert b"websocket-test" in output
    client.delete(f"/api/v1/terminals/{terminal_id}")
    assert profile_id == 1
    audit = client.get("/api/v1/audit")
    assert audit.status_code == 200
    assert any(item["action"] == "POST /api/v1/connections" for item in audit.json())
    blocked = client.post(
        "/api/v1/connections",
        headers={"Origin": "https://external.example"},
        json={"name": "blocked", "host": "192.0.2.20", "user": "kali"},
    )
    assert blocked.status_code == 403


def test_web_v1_dashboard_read_aliases_are_available(workspace) -> None:
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    workspace.add_host(HostCreate(name="web01", ip="192.0.2.20"))
    with TestClient(create_app(workspace.paths)) as client:
        hosts = client.get("/api/v1/hosts")
        paths = client.get("/api/v1/paths")
    assert hosts.status_code == 200
    assert hosts.json()[0]["name"] == "web01"
    assert paths.status_code == 200
    assert paths.json() == []


def test_web_v2_terminal_stream_includes_sequence_metadata(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    with TestClient(create_app(workspace.paths)) as client:
        terminal = client.post(
            f"/api/v2/workspaces/{workspace.lab.id}/terminals",
            json={"name": "sequenced", "kind": "local"},
        )
        assert terminal.status_code == 201
        terminal_id = terminal.json()["id"]
        with client.websocket_connect(
            f"/api/v2/workspaces/{workspace.lab.id}/terminals/{terminal_id}/stream"
        ) as socket:
            socket.send_text("echo sequenced-output\n")
            metadata: dict[str, object] | None = None
            output = b""
            for _ in range(30):
                message = socket.receive()
                if message.get("text"):
                    metadata = json.loads(message["text"])
                if message.get("bytes"):
                    output += message["bytes"]
                if b"sequenced-output" in output:
                    break
            assert metadata is not None
            assert isinstance(metadata["sequence"], int)
            assert metadata["gap"] is False
            assert b"sequenced-output" in output
        client.delete(f"/api/v2/workspaces/{workspace.lab.id}/terminals/{terminal_id}")


def test_web_runtime_start_requires_server_side_confirmation(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    host = workspace.add_host(HostCreate(name="review-host", ip="192.0.2.90"))
    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="review-before-start",
            via_host_id=host.id,
            local_port=19090,
            target_address="198.51.100.20",
            target_port=80,
        )
    )

    with TestClient(create_app(workspace.paths)) as client:
        response = client.post(f"/api/v2/workspaces/{workspace.lab.id}/tunnels/{forward.id}/start")
        assert response.status_code == 409
        assert response.json()["code"] == "confirmation_required"
        assert workspace.forwards.get(forward.id).status == ForwardStatus.PLANNED


def test_read_only_terminal_view_does_not_steal_input_control(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    with TestClient(create_app(workspace.paths)) as client:
        terminal = client.post("/api/v1/terminals", json={"name": "split-view", "kind": "local"})
        assert terminal.status_code == 201
        terminal_id = terminal.json()["id"]
        with client.websocket_connect(f"/api/v1/terminals/{terminal_id}/stream") as controller:
            with client.websocket_connect(
                f"/api/v1/terminals/{terminal_id}/stream?readonly=1"
            ) as viewer:
                controller.send_text("echo split-control\n")
                output = b""
                for _ in range(30):
                    message = controller.receive()
                    if message.get("bytes"):
                        output += message["bytes"]
                    if b"split-control" in output:
                        break
                assert b"split-control" in output
                viewer.close()
        client.delete(f"/api/v1/terminals/{terminal_id}")


def test_observer_terminal_stream_requires_explicit_sharing(workspace, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from ctfws.models.membership import MembershipCreate, WorkspaceRole
    from ctfws.web import create_app

    monkeypatch.setenv("CTFWS_AUTH_REQUIRED", "1")
    monkeypatch.setenv("CTFWS_BOOTSTRAP_TOKEN", "terminal-share-bootstrap")
    monkeypatch.setenv("CTFWS_REQUIRE_MEMBERSHIP", "1")
    with TestClient(create_app(workspace.paths)) as client:
        login = client.post(
            "/api/v2/auth/login",
            json={"bootstrap_token": "terminal-share-bootstrap"},
        )
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        workspace_id = workspace.lab.id
        terminal = client.post(
            f"/api/v2/workspaces/{workspace_id}/terminals",
            headers=headers,
            json={"name": "private-share", "kind": "local"},
        )
        assert terminal.status_code == 201
        terminal_id = terminal.json()["id"]
        assert terminal.json()["sharing"] == TerminalSharing.PRIVATE.value

        workspace.memberships.upsert(
            MembershipCreate(subject="bootstrap-admin", role=WorkspaceRole.OBSERVER)
        )
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(
                f"/api/v2/workspaces/{workspace_id}/terminals/{terminal_id}/stream",
                headers=headers,
            ) as socket:
                socket.receive()
        assert denied.value.code == 4403

        workspace.memberships.upsert(
            MembershipCreate(subject="bootstrap-admin", role=WorkspaceRole.ADMIN)
        )
        shared = client.post(
            f"/api/v2/workspaces/{workspace_id}/terminals/{terminal_id}/share",
            headers=headers,
            json={"shared": True},
        )
        assert shared.status_code == 200
        assert shared.json()["sharing"] == TerminalSharing.SHARED.value

        workspace.memberships.upsert(
            MembershipCreate(subject="bootstrap-admin", role=WorkspaceRole.OBSERVER)
        )
        with client.websocket_connect(
            f"/api/v2/workspaces/{workspace_id}/terminals/{terminal_id}/stream",
            headers=headers,
        ) as socket:
            socket.close()
        client.delete(
            f"/api/v2/workspaces/{workspace_id}/terminals/{terminal_id}",
            headers=headers,
        )


def test_web_file_shelf_is_workspace_scoped(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    client = TestClient(create_app(workspace.paths))
    uploaded = client.post(
        "/api/v1/files/upload",
        params={"destination": "loot/inbox"},
        files={"file": ("notes.txt", b"network evidence")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["size"] == len(b"network evidence")
    assert len(uploaded.json()["sha256"]) == 64

    listing = client.get("/api/v1/files", params={"relative": "loot/inbox"})
    assert listing.status_code == 200
    assert listing.json()[0]["name"] == "notes.txt"

    blocked = client.get("/api/v1/files", params={"relative": "../outside"})
    assert blocked.status_code == 422


def test_file_conflict_policy_can_keep_both_or_replace(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    with TestClient(create_app(workspace.paths)) as client:
        first = client.post(
            f"/api/v2/workspaces/{workspace.lab.id}/files/upload",
            params={"destination": "loot/inbox"},
            files={"file": ("same name.txt", b"first")},
        )
        assert first.status_code == 200

        kept = client.post(
            f"/api/v2/workspaces/{workspace.lab.id}/files/upload",
            params={"destination": "loot/inbox", "conflict": "keep_both"},
            files={"file": ("same name.txt", b"second")},
        )
        assert kept.status_code == 200
        assert kept.json()["path"] == "loot/inbox/same name (1).txt"

        replaced = client.post(
            f"/api/v2/workspaces/{workspace.lab.id}/files/upload",
            params={"destination": "loot/inbox", "conflict": "replace"},
            files={"file": ("same name.txt", b"replacement")},
        )
        assert replaced.status_code == 200

    assert (
        workspace.paths.root / "loot" / "inbox" / "same name.txt"
    ).read_bytes() == b"replacement"
    assert (workspace.paths.root / "loot" / "inbox" / "same name (1).txt").read_bytes() == b"second"


def test_web_generates_a_socks_launcher_without_executing_it(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.models.context import ContextStatus, ContextTransport, NetworkContextCreate
    from ctfws.services.contexts import NetworkContextService
    from ctfws.web import create_app

    profile = workspace.connections.create(
        ConnectionProfileCreate(name="launcher-target", host="192.0.2.81", user="analyst")
    )
    context = NetworkContextService(workspace).plan(
        NetworkContextCreate(
            name="launcher-socks",
            transport=ContextTransport.SOCKS,
            connection_id=profile.id,
            local_address="127.0.0.1",
            local_port=19091,
        )
    )
    workspace.contexts.update_runtime(context.id, status=ContextStatus.ACTIVE)

    with TestClient(create_app(workspace.paths)) as client:
        response = client.post(
            f"/api/v2/workspaces/{workspace.lab.id}/contexts/{context.id}/launcher",
            json={
                "program": "curl",
                "arguments": ["http://target"],
                "launcher": "environment",
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["argv"] == ["curl", "http://target"]
    assert payload["environment"]["ALL_PROXY"] == "socks5h://127.0.0.1:19091"


def test_web_v2_exposes_history_evidence_search_and_topology(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    host = workspace.add_host(HostCreate(name="web01", ip="192.0.2.20"))
    client = TestClient(create_app(workspace.paths))
    snapshot = client.post(
        f"/api/v2/workspaces/{workspace.lab.id}/snapshots",
        json={"name": "round-01", "purpose": "baseline"},
    )
    assert snapshot.status_code == 201
    evidence = client.post(
        f"/api/v2/workspaces/{workspace.lab.id}/evidence",
        json={
            "host_id": host.id,
            "type": "note",
            "description": "observação manual",
            "path": "notes/baseline.txt",
        },
    )
    assert evidence.status_code == 201
    assert client.get("/api/v1/search", params={"query": "web01"}).json()[0]["kind"] == "Host"
    topology = client.get("/api/v1/topology", params={"format_name": "dot"})
    assert topology.status_code == 200
    assert topology.json()["format"] == "dot"
    assert "digraph ctfws" in topology.json()["content"]


def test_sftp_transfer_builds_structured_command(workspace, monkeypatch) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(
            name="jump",
            host="192.0.2.10",
            user="kali",
            port=2222,
            identity_file="/tmp/id_ed25519",
        )
    )
    source = workspace.paths.root / "loot" / "tool.bin"
    source.write_bytes(b"tool")
    calls: list[tuple[list[str], str]] = []

    class Result:
        returncode = 0
        stdout = "uploaded"
        stderr = ""

    def fake_run(command, **kwargs):
        if kwargs["input"].startswith("ls -d"):
            raise RuntimeError("Transferência SFTP falhou: No such file or directory")
        calls.append((command, kwargs["input"]))
        return Result()

    monkeypatch.setattr("ctfws.services.transfers.subprocess.run", fake_run)
    result = SFTPTransferService(workspace).upload(profile.id, "loot/tool.bin", "/tmp/tool.bin")
    assert result.sha256
    assert calls[0][0][:4] == ["sftp", "-b", "-", "-P"]
    assert "put" in calls[0][1] and "/tmp/tool.bin" in calls[0][1]

    with pytest.raises(ValueError):
        SFTPTransferService(workspace).upload(profile.id, "../outside", "/tmp/tool.bin")


def test_cataloged_tool_transfer_rechecks_hash_and_never_executes(workspace, monkeypatch) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="tool-target", host="192.0.2.41", user="analyst")
    )
    source = workspace.paths.root / "loot" / "ncat-static"
    source.write_bytes(b"cataloged tool")
    tool = ToolCatalogService(workspace).register(
        ToolCreate(name="ncat-static", path=str(source), architecture="x86_64", version="1.0")
    )
    calls: list[tuple[list[str], str]] = []

    class Result:
        returncode = 0
        stdout = "uploaded"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["input"]))
        return Result()

    monkeypatch.setattr("ctfws.services.transfers.subprocess.run", fake_run)
    result = SFTPTransferService(workspace).upload_tool(
        profile.id, tool.id, "/tmp/ncat-static", overwrite=True
    )
    assert result.local_path == f"tool:{tool.id}:ncat-static"
    assert "put" in calls[0][1]
    assert str(source) in calls[0][1]
    assert "exec" not in calls[0][1].lower()

    source.write_bytes(b"changed after catalog")
    with pytest.raises(ValueError, match="mudou desde o cadastro"):
        SFTPTransferService(workspace).upload_tool(profile.id, tool.id, "/tmp/ncat-static")


def test_asyncssh_sftp_streams_and_verifies_without_shell(workspace, monkeypatch) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="sftp", host="192.0.2.40", user="analyst")
    )
    source = workspace.paths.root / "loot" / "ferramenta ✓.bin"
    source.write_bytes("conteúdo remoto".encode())
    remote_store: dict[str, bytes] = {}

    class RemoteFile:
        def __init__(self, path: str, mode: str) -> None:
            self.path = path
            self.mode = mode
            self.position = 0
            if "w" in mode:
                remote_store[path] = b""

        async def write(self, chunk: bytes) -> None:
            remote_store[self.path] += chunk

        async def read(self, size: int) -> bytes:
            value = remote_store[self.path][self.position : self.position + size]
            self.position += len(value)
            return value

        async def close(self) -> None:
            return None

    class Entry:
        filename = "tool.bin"

    class SFTPNotFound(Exception):
        code = 2

    class FakeSFTP:
        async def open(self, path: str, mode: str) -> RemoteFile:
            return RemoteFile(path, mode)

        async def rename(self, source_path: str, target_path: str) -> None:
            remote_store[target_path] = remote_store.pop(source_path)

        async def posix_rename(self, source_path: str, target_path: str) -> None:
            remote_store[target_path] = remote_store.pop(source_path)

        async def stat(self, path: str) -> object:
            if path not in remote_store:
                raise SFTPNotFound(path)
            return object()

        async def readdir(self, _path: str) -> list[Entry]:
            return [Entry()]

    sftp = FakeSFTP()

    class FakeConnection:
        def is_closed(self) -> bool:
            return False

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

        async def start_sftp_client(self) -> FakeSFTP:
            return sftp

    class FakeAsyncSSH:
        async def connect(self, **_kwargs: object) -> FakeConnection:
            return FakeConnection()

    monkeypatch.setattr("ctfws.services.ssh.asyncssh", FakeAsyncSSH())
    manager = AsyncSSHConnectionManager(workspace)
    service = SFTPTransferService(workspace, manager)

    async def exercise() -> tuple[object, list[dict[str, str]], object]:
        uploaded = await service.upload_async(profile.id, "loot/ferramenta ✓.bin", "/tmp/tool.bin")
        entries = await service.list_remote_async(profile.id, "/tmp")
        downloaded = await service.download_async(profile.id, "/tmp/tool.bin", "loot/cópia.bin")
        return uploaded, entries, downloaded

    uploaded, entries, downloaded = asyncio.run(exercise())
    assert uploaded.integrity_verified is True  # type: ignore[union-attr]
    assert entries == [{"name": "tool.bin", "path": "/tmp/tool.bin", "kind": "entry"}]
    assert downloaded.integrity_verified is True  # type: ignore[union-attr]
    assert (workspace.paths.root / "loot" / "cópia.bin").read_bytes() == source.read_bytes()
    with pytest.raises(ValueError, match="destination_conflict"):
        asyncio.run(service.upload_async(profile.id, "loot/ferramenta ✓.bin", "/tmp/tool.bin"))

    kept = asyncio.run(
        service.upload_async(
            profile.id,
            "loot/ferramenta ✓.bin",
            "/tmp/tool.bin",
            conflict=ConflictPolicy.KEEP_BOTH,
        )
    )
    assert kept.remote_path == "/tmp/tool (1).bin"
    assert "/tmp/tool (1).bin" in remote_store

    replaced = asyncio.run(
        service.upload_async(
            profile.id,
            "loot/ferramenta ✓.bin",
            "/tmp/tool.bin",
            conflict=ConflictPolicy.REPLACE,
        )
    )
    assert replaced.integrity_verified is True
    assert remote_store["/tmp/tool.bin"] == source.read_bytes()

    async def cancelled_read(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise asyncio.CancelledError()

    monkeypatch.setattr(service, "_read_remote_file", cancelled_read)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.download_async(profile.id, "/tmp/tool.bin", "loot/cancel.bin"))
    assert workspace.transfers.list()[0].status.value == "cancelled"


def test_sftp_remote_listing_preserves_names_without_shell_execution(
    workspace, monkeypatch
) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="jump", host="192.0.2.10", user="kali")
    )
    calls: list[str] = []

    def fake_run(_service, _profile, batch: str) -> str:
        calls.append(batch)
        return "alpha file\nunicode-✓\n"

    monkeypatch.setattr(SFTPTransferService, "_run", fake_run)
    entries = SFTPTransferService(workspace).list_remote(profile.id, "/tmp")
    assert entries == [
        {"name": "alpha file", "path": "/tmp/alpha file", "kind": "entry"},
        {"name": "unicode-✓", "path": "/tmp/unicode-✓", "kind": "entry"},
    ]
    assert "ls -1 /tmp" in calls[0]


def test_web_creates_reviewable_tunnel_plan(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    host = workspace.add_host(HostCreate(name="jump", ip="192.0.2.10", user="kali"))
    client = TestClient(create_app(workspace.paths))
    response = client.post(
        "/api/v1/forwards",
        json={
            "name": "db-tunnel",
            "via_host_id": host.id,
            "kind": ForwardKind.LOCAL.value,
            "local_port": 13306,
            "target_address": "172.16.50.20",
            "target_port": 3306,
            "tool": "ssh",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "planned"
    assert "BatchMode=yes" in response.json()["command"]


def test_remote_inspection_imports_fixed_read_only_outputs(workspace, monkeypatch) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="jump", host="10.10.10.20", user="kali")
    )
    fixture_root = Path(__file__).parent / "fixtures"
    outputs = {
        ("ip", "addr"): (fixture_root / "ip_addr.txt").read_text(),
        ("ip", "route"): (fixture_root / "ip_route.txt").read_text(),
        ("ip", "neigh"): (fixture_root / "ip_neigh.txt").read_text(),
        ("ss", "-tunap"): (fixture_root / "ss.txt").read_text(),
        ("cat", "/etc/hosts"): (fixture_root / "hosts.txt").read_text(),
        ("cat", "/etc/resolv.conf"): (fixture_root / "resolv.conf.txt").read_text(),
    }

    class Completed:
        returncode = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake_run(command, **kwargs):
        return Completed(outputs[tuple(command[-2:])])

    monkeypatch.setattr("ctfws.services.inspection.subprocess.run", fake_run)
    result = RemoteInspectionService(workspace).inspect(profile.id)
    assert result.host_name == "jump"
    assert result.networks >= 1
    assert result.snapshot_id is not None
    assert workspace.collections.list(profile.host_id)[0].snapshot_id == result.snapshot_id


def test_remote_inspection_resolves_profile_hostname_without_using_name_as_identity(
    workspace, monkeypatch
) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="dns-target", host="internal.example", user="kali")
    )
    monkeypatch.setattr(
        "ctfws.services.inspection.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("198.51.100.45", 0))],
    )

    host = RemoteInspectionService(workspace)._ensure_host(profile)  # noqa: SLF001

    assert str(host.ip) == "198.51.100.45"
    assert host.hostname == "internal.example"
    assert host.name == "dns-target"
    assert workspace.hosts.get("internal.example") is None


def test_asyncssh_remote_inspection_reuses_manager_and_preserves_snapshot(workspace) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="async-inspection", host="10.10.10.20", user="kali")
    )
    fixture_root = Path(__file__).parent / "fixtures"
    outputs = {
        "ip addr": (fixture_root / "ip_addr.txt").read_text(),
        "ip route": (fixture_root / "ip_route.txt").read_text(),
        "ip neigh": (fixture_root / "ip_neigh.txt").read_text(),
        "ss -tunap": (fixture_root / "ss.txt").read_text(),
        "cat /etc/hosts": (fixture_root / "hosts.txt").read_text(),
        "cat /etc/resolv.conf": (fixture_root / "resolv.conf.txt").read_text(),
    }
    calls: list[tuple[int, str, float | None]] = []

    class Result:
        exit_status = 0
        stderr = ""

        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    class FakeSharedSSH:
        available = True

        async def run(self, connection_id: int, command: str, *, timeout: float) -> Result:
            calls.append((connection_id, command, timeout))
            key = command.removeprefix("LC_ALL=C ")
            return Result(outputs[key])

    progress: list[str] = []
    result = asyncio.run(
        RemoteInspectionService(workspace, FakeSharedSSH()).inspect_async(
            profile.id, lambda _completed, current: progress.append(current)
        )
    )
    assert result.host_name == "async-inspection"
    assert len(calls) == len(RemoteInspectionService.COMMANDS)
    assert all(call[0] == profile.id and call[2] == 15 for call in calls)
    assert progress == list(RemoteInspectionService.COMMANDS)
    assert result.failed_steps == ()
    assert result.snapshot_id is not None
