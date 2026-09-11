"""Regression coverage for the operational evolution layers."""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import stat
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from ctfws.core.errors import IdempotencyConflictError
from ctfws.models.access import AccessPathCreate, AccessPathState
from ctfws.models.connection import ConnectionProfileCreate
from ctfws.models.context import ContextStatus, ContextTransport, NetworkContextCreate
from ctfws.models.forward import ForwardCreate, ForwardStatus
from ctfws.models.host import HostCreate
from ctfws.models.task import TaskCreate, TaskStatus
from ctfws.models.terminal import TerminalCreate, TerminalStatus
from ctfws.services.access_paths import AccessPathService
from ctfws.services.backups import WorkspaceBackupService
from ctfws.services.contexts import NetworkContextService
from ctfws.services.engine import EngineLock
from ctfws.services.pivot import PivotService
from ctfws.services.processes import ForwardProcessService
from ctfws.services.resume import ResumeService
from ctfws.services.routed_helper import (
    NamespaceApplyResult,
    NamespaceOperation,
    RoutedNamespaceHelper,
)
from ctfws.services.routed_socket import RoutedNamespaceSocketClient, RoutedNamespaceSocketServer
from ctfws.services.tasks import TaskManager, TaskProgress
from ctfws.services.terminals import TerminalManager
from ctfws.services.vault import SecretVault


def test_path_verification_persists_explicit_endpoint_proof(workspace) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])
    try:
        path = workspace.access_paths.upsert(
            AccessPathCreate(
                target_address="127.0.0.1",
                target_port=port,
                state=AccessPathState.CANDIDATE,
                reason="endpoint explicitly observed",
            )
        )
        verified = AccessPathService(workspace).verify(path.id)
        assert verified.state == AccessPathState.VERIFIED
        assert verified.verification_id is not None
        checks = workspace.access_verifications.list_for_path(path.id)
        assert checks[0].status == "reachable"
    finally:
        listener.close()


def test_degraded_session_does_not_verify_paths(workspace) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="jump", host="192.0.2.10", user="kali")
    )
    assert profile.state.value == "disconnected"
    assert all(path.state != AccessPathState.VERIFIED for path in workspace.access_paths.list())


def test_task_manager_has_durable_success_and_idempotency(workspace) -> None:
    manager = TaskManager(workspace)
    data = TaskCreate(kind="fixture", total_steps=1, idempotency_key="fixture-1")
    first = manager.submit(data, lambda _update: {"ok": True})
    same = manager.submit(data, lambda _update: {"ok": False})
    assert same.id == first.id
    deadline = time.time() + 3
    while time.time() < deadline:
        current = manager.get(first.id)
        if current.status == TaskStatus.SUCCEEDED:
            break
        time.sleep(0.02)
    assert manager.get(first.id).result == {"ok": True}
    manager.close()


def test_task_manager_rejects_idempotency_key_with_different_content(workspace) -> None:
    manager = TaskManager(workspace)
    first = TaskCreate(
        kind="fixture",
        total_steps=1,
        idempotency_key="fixture-conflict",
        idempotency_hash="a" * 64,
    )
    manager.submit(first, lambda _update: {"ok": True})
    with pytest.raises(IdempotencyConflictError, match="requisição diferente"):
        manager.submit(
            first.model_copy(update={"idempotency_hash": "b" * 64}),
            lambda _update: {"ok": False},
        )
    manager.close()


def test_runtime_services_recover_only_after_the_motor_owns_the_workspace(workspace) -> None:
    task = workspace.tasks.create(TaskCreate(kind="pending-recovery", total_steps=1))
    terminal = workspace.terminals.create(
        TerminalCreate(name="pending-terminal"), "LOCAL → Kali/Linux", "operator", "old-engine"
    )

    tasks = TaskManager(workspace)
    terminals = TerminalManager(workspace)
    assert workspace.tasks.get(task.id).status == TaskStatus.QUEUED  # type: ignore[union-attr]
    assert workspace.terminals.get(terminal.id).status == TerminalStatus.STARTING  # type: ignore[union-attr]

    tasks.recover()
    terminals.recover()
    assert workspace.tasks.get(task.id).status == TaskStatus.INTERRUPTED  # type: ignore[union-attr]
    assert workspace.terminals.get(terminal.id).status == TerminalStatus.EXITED  # type: ignore[union-attr]
    tasks.close()


def test_task_manager_async_work_has_durable_success(workspace) -> None:
    async def exercise() -> None:
        manager = TaskManager(workspace)
        task = manager.submit_async(
            TaskCreate(kind="async-fixture", total_steps=1),
            lambda _update: _async_result(),
        )
        for _ in range(20):
            if manager.get(task.id).status == TaskStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        current = manager.get(task.id)
        assert current.status == TaskStatus.SUCCEEDED
        assert current.result == {"transport": "shared"}
        manager.close()

    async def _async_result() -> dict[str, object]:
        await asyncio.sleep(0)
        return {"transport": "shared"}

    asyncio.run(exercise())


def test_task_manager_limits_async_work_to_configured_capacity(workspace) -> None:
    async def exercise() -> None:
        manager = TaskManager(workspace, max_workers=2)
        active = 0
        maximum = 0
        release = asyncio.Event()

        async def work(_update) -> dict[str, object]:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await release.wait()
            active -= 1
            return {"ok": True}

        tasks = [
            manager.submit_async(TaskCreate(kind=f"capacity-{index}", total_steps=1), work)
            for index in range(5)
        ]
        for _ in range(20):
            if active == 2:
                break
            await asyncio.sleep(0)
        assert active == 2
        assert maximum == 2
        assert sum(manager.get(task.id).status == TaskStatus.RUNNING for task in tasks) == 2
        assert sum(manager.get(task.id).status == TaskStatus.QUEUED for task in tasks) == 3
        release.set()
        for _ in range(40):
            if all(manager.get(task.id).status == TaskStatus.SUCCEEDED for task in tasks):
                break
            await asyncio.sleep(0)
        assert all(manager.get(task.id).status == TaskStatus.SUCCEEDED for task in tasks)
        manager.close()

    asyncio.run(exercise())


def test_task_manager_publishes_progress_and_failure_events(workspace) -> None:
    seen: list[str] = []
    workspace.bus.subscribe("*", lambda event: seen.append(event.event_type))
    manager = TaskManager(workspace)

    def failing_work(update) -> dict[str, object]:
        update(TaskProgress(1, 2, "primeira etapa"))
        raise RuntimeError("falha de laboratório")

    task = manager.submit(TaskCreate(kind="failing-fixture", total_steps=2), failing_work)
    deadline = time.time() + 3
    while time.time() < deadline and "TASK_FAILED" not in seen:
        time.sleep(0.02)

    assert manager.get(task.id).status == TaskStatus.FAILED
    assert "TASK_RUNNING" in seen
    assert "TASK_PROGRESS" in seen
    assert "TASK_FAILED" in seen
    manager.close()


def test_task_state_and_event_roll_back_together(workspace) -> None:
    task = workspace.tasks.create(TaskCreate(kind="atomic-fixture", total_steps=1))
    with workspace.database.connection() as connection:
        connection.execute("""
            CREATE TRIGGER reject_task_events
            BEFORE INSERT ON events
            WHEN NEW.entity_type = 'task'
            BEGIN
                SELECT RAISE(ABORT, 'event sink unavailable');
            END
            """)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="event sink unavailable"):
            workspace.tasks.update_with_event(
                task.id,
                status=TaskStatus.RUNNING,
                current_step="transição",
            )
    finally:
        with workspace.database.connection() as connection:
            connection.execute("DROP TRIGGER reject_task_events")
    assert workspace.tasks.get(task.id).status == TaskStatus.QUEUED


def test_socks_context_generates_scoped_launchers_without_execution(workspace, monkeypatch) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="socks-target", host="192.0.2.80", user="analyst")
    )
    service = NetworkContextService(workspace)
    context = service.plan(
        NetworkContextCreate(
            name="operator-socks",
            transport=ContextTransport.SOCKS,
            connection_id=profile.id,
            local_address="127.0.0.1",
            local_port=19090,
        )
    )
    workspace.contexts.update_runtime(
        context.id,
        status=ContextStatus.ACTIVE,
        health="listener_open;scope_unprobed",
        capabilities=("socks", "tcp"),
    )

    environment_plan = service.launcher_plan(
        context.id, "curl", ("--proxy", "socks5h://example.invalid", "http://target")
    )
    assert environment_plan["argv"] == [
        "curl",
        "--proxy",
        "socks5h://example.invalid",
        "http://target",
    ]
    assert environment_plan["environment"]["ALL_PROXY"] == "socks5h://127.0.0.1:19090"

    monkeypatch.setattr(
        "ctfws.services.contexts.shutil.which", lambda name: "/usr/bin/proxychains4"
    )
    proxychains_plan = service.launcher_plan(
        context.id, "nmap", ("-sT", "10.20.0.0/24"), launcher="proxychains"
    )
    config_path = Path(str(proxychains_plan["config_path"]))
    assert config_path.is_file()
    assert "proxy_dns" in config_path.read_text(encoding="utf-8")
    assert proxychains_plan["argv"][0] == "/usr/bin/proxychains4"

    routed = service.plan(
        NetworkContextCreate(
            name="launcher-routed",
            transport=ContextTransport.ROUTED,
            network_cidrs=("10.30.0.0/24",),
        )
    )
    routed_namespace = service.routed_helper.namespace_for(routed.id)
    workspace.contexts.update_runtime(
        routed.id,
        status=ContextStatus.ACTIVE,
        namespace_name=routed_namespace,
        resource_manifest=(f"namespace:{routed_namespace}",),
    )
    routed_plan = service.launcher_plan(
        routed.id, "nmap", ("-sT", "10.30.0.0/24"), launcher="namespace"
    )
    assert routed_plan["argv"] == ["nmap", "-sT", "10.30.0.0/24"]
    assert routed_plan["execution"] == "context-worker"

    execution = service.execute_launcher(
        context.id,
        sys.executable,
        ("-c", "print('context-execution-ok')"),
    )
    assert execution["returncode"] == 0
    assert execution["failed_steps"] == []
    assert "context-execution-ok" in str(execution["output"])


def test_routed_namespace_prepare_and_remove_persist_exact_manifest(workspace, monkeypatch) -> None:
    service = NetworkContextService(workspace)
    context = service.plan(
        NetworkContextCreate(
            name="prepared-routed",
            transport=ContextTransport.ROUTED,
            network_cidrs=("10.50.0.0/24", "2001:db8:50::/64"),
        )
    )
    apply_plan = service.routed_helper.plan(context.id, context.network_cidrs, device="ctfws0")
    apply_result = NamespaceApplyResult(
        apply_plan[0].namespace,
        tuple(service.routed_helper._resource_key(item) for item in apply_plan),  # noqa: SLF001
    )
    applied: list[tuple[NamespaceOperation, ...]] = []
    removed: list[tuple[NamespaceOperation, ...]] = []
    monkeypatch.setattr(
        service.routed_helper,
        "apply",
        lambda operations: (applied.append(operations) or apply_result),
    )
    monkeypatch.setattr(
        service.routed_helper,
        "remove",
        lambda operations: removed.append(operations),
    )

    prepared = service.prepare_namespace(context.id, device="ctfws0")
    assert prepared.status == ContextStatus.PLANNED
    assert prepared.namespace_name == apply_result.namespace
    assert prepared.resource_manifest == apply_result.resources
    assert applied == [apply_plan]

    removed_context = service.remove_namespace(context.id)
    assert removed_context.namespace_name is None
    assert removed_context.resource_manifest == ()
    assert [item.action for item in removed[0]] == ["route-delete", "route-delete", "delete"]
    assert removed[0][0].cidr == "2001:db8:50::/64"


def test_resume_async_recreates_unowned_ssh_listener_explicitly(workspace) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="resume", host="192.0.2.70", user="analyst")
    )
    host = workspace.add_host(HostCreate(name="resume", ip="192.0.2.70"))
    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="resume-forward",
            via_host_id=host.id,
            connection_id=profile.id,
            local_port=18080,
            target_address="10.40.0.5",
            target_port=80,
        )
    )
    workspace.forwards.set_process(forward.id, None, ForwardStatus.ACTIVE)

    class FakeListener:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class FakeConnection:
        async def forward_local_port(self, *_args: object, **_kwargs: object) -> FakeListener:
            return FakeListener()

    class FakeSSH:
        available = True

        async def connect(self, _profile_id: int) -> FakeConnection:
            return FakeConnection()

    service = ForwardProcessService(workspace, FakeSSH())
    result = asyncio.run(ResumeService(workspace, service).apply_async((forward.id,), None))
    assert result[0]["resource_type"] == "forward"
    assert result[0]["resource"]["status"] == "active"  # type: ignore[index]


def test_vault_never_returns_secret_from_reference(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "config")
    reference = vault.put("ssh-test", "temporary-secret", owner="operator")
    assert reference == "vault:ssh-test"
    assert vault.get(reference, owner="operator") == "temporary-secret"
    assert vault.list_refs() == [reference]
    vault.revoke(reference)
    assert vault.list_refs() == []


def test_context_plan_is_scoped_and_does_not_use_default_route(workspace) -> None:
    profile = workspace.connections.create(
        ConnectionProfileCreate(name="jump", host="192.0.2.10", user="kali")
    )
    context = NetworkContextService(workspace).plan(
        NetworkContextCreate(
            name="internal-socks",
            transport=ContextTransport.SOCKS,
            connection_id=profile.id,
            network_cidrs=("10.10.10.0/24",),
        )
    )
    assert context.local_port is not None
    assert "192.0.2.10" in " ".join(context.command)

    try:
        NetworkContextService(workspace).plan(
            NetworkContextCreate(
                name="default-route",
                transport=ContextTransport.SOCKS,
                connection_id=profile.id,
                network_cidrs=("0.0.0.0/0",),
            )
        )
    except ValueError as error:
        assert "rota padrão" in str(error)
    else:
        raise AssertionError("rota padrão deveria ser rejeitada")


def test_routed_namespace_plan_is_typed_and_workspace_scoped(workspace) -> None:
    service = NetworkContextService(workspace)
    context = service.plan(
        NetworkContextCreate(
            name="internal-routed",
            transport=ContextTransport.ROUTED,
            network_cidrs=("10.20.0.0/16",),
        )
    )
    operations = service.namespace_plan(context.id, device="ctfws0")
    namespace = str(operations[0]["namespace"])
    assert operations[0]["action"] == "create"
    assert namespace == f"ctfws-{workspace.lab.id}-{context.id}"
    assert operations[1]["action"] == "route-add"
    assert operations[1]["cidr"] == "10.20.0.0/16"

    helper = RoutedNamespaceHelper(workspace)
    with pytest.raises(ValueError, match="não pertence"):
        helper._validate(  # noqa: SLF001 - contract boundary under test
            NamespaceOperation(
                workspace.lab.id + 1,
                context.id,
                "create",
                namespace,
            )
        )


def test_routed_runtime_allocates_isolated_link_per_context(workspace) -> None:
    helper = RoutedNamespaceHelper(workspace)
    first = helper.runtime_addresses(1)
    second = helper.runtime_addresses(2)
    assert first != second
    assert first == ("169.254.0.1", "169.254.0.2")
    assert second == ("169.254.0.5", "169.254.0.6")
    resources = helper.runtime_resources(1)
    assert "veth-host:" in " ".join(resources)
    assert "address-namespace:" in " ".join(resources)
    with pytest.raises(ValueError, match="limite"):
        helper.runtime_addresses(16385)


def test_routed_namespace_batches_are_atomic_and_cleanup_is_reversed(workspace) -> None:
    helper = RoutedNamespaceHelper(workspace)
    apply_plan = helper.plan(7, ("10.20.0.0/16", "10.21.0.0/16"), device="ctfws0")
    assert [item.action for item in helper.validate(apply_plan)] == [
        "create",
        "route-add",
        "route-add",
    ]

    cleanup = helper.cleanup_plan(7, ("10.20.0.0/16", "10.21.0.0/16"), device="ctfws0")
    assert [item.action for item in cleanup] == ["route-delete", "route-delete", "delete"]
    assert cleanup[0].cidr == "10.21.0.0/16"
    assert helper.validate(cleanup, mode="remove") == cleanup

    with pytest.raises(ValueError, match="modo 'apply'"):
        helper.validate(cleanup)

    with pytest.raises(ValueError, match="mesmo contexto"):
        helper.validate(
            (
                apply_plan[0],
                NamespaceOperation(
                    workspace.lab.id,
                    8,
                    "route-add",
                    helper.namespace_for(8),
                    "10.22.0.0/16",
                ),
            )
        )


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets are unavailable")
def test_routed_namespace_socket_protocol_is_typed_and_reusable(workspace, tmp_path: Path) -> None:
    class ContractHelper:
        def __init__(self) -> None:
            self.applied: list[tuple[NamespaceOperation, ...]] = []
            self.removed: list[tuple[NamespaceOperation, ...]] = []

        def apply(self, operations: tuple[NamespaceOperation, ...]) -> NamespaceApplyResult:
            self.applied.append(operations)
            return NamespaceApplyResult(
                operations[0].namespace, ("namespace:" + operations[0].namespace,)
            )

        def remove(self, operations: tuple[NamespaceOperation, ...]) -> None:
            self.removed.append(operations)

    helper = ContractHelper()
    socket_path = tmp_path / "namespace-helper.sock"
    server = RoutedNamespaceSocketServer(helper, socket_path)
    thread_errors: list[BaseException] = []

    def run_server() -> None:
        try:
            server.serve_forever()
        except BaseException as error:  # propagate background failures through the test
            thread_errors.append(error)

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        namespace = f"ctfws-{workspace.lab.id}-7"
        operation = NamespaceOperation(workspace.lab.id, 7, "create", namespace)
        result = RoutedNamespaceSocketClient(socket_path).apply((operation,))
        assert result.namespace == namespace
        assert helper.applied == [(operation,)]
        RoutedNamespaceSocketClient(socket_path).remove((operation,))
        assert helper.removed == [(operation,)]
    finally:
        server.close()
        thread.join(timeout=2)
    assert not thread_errors
    assert not thread.is_alive()
    assert not socket_path.exists()


def test_forward_stop_permission_failure_is_not_reported_as_stopped(workspace, monkeypatch) -> None:
    import ctfws.services.processes as processes_module
    from ctfws.core.process import ProcessIdentity
    from ctfws.models.host import HostCreate

    host = workspace.add_host(HostCreate(name="owned", ip="192.0.2.80"))
    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="owned-process",
            via_host_id=host.id,
            local_port=0,
            target_address="10.0.0.5",
            target_port=80,
        )
    )
    workspace.forwards.set_process(forward.id, 4321, ForwardStatus.ACTIVE)
    monkeypatch.setattr(
        processes_module,
        "process_identity",
        lambda _pid: ProcessIdentity(4321, None, None, None),
    )
    monkeypatch.setattr(
        processes_module.os, "kill", lambda *_args: (_ for _ in ()).throw(PermissionError("denied"))
    )

    result = ForwardProcessService(workspace).stop(forward.id)
    assert result.status == ForwardStatus.ERROR
    assert result.health == "stop_permission_denied"


def test_full_backup_restores_files_and_hash_manifest(workspace, tmp_path: Path) -> None:
    evidence = workspace.paths.root / "evidence" / "proof.txt"
    evidence.write_text("observed", encoding="utf-8")
    archive = WorkspaceBackupService().create(workspace, tmp_path / "backup.zip")
    restored = WorkspaceBackupService().restore(archive, tmp_path / "restored")
    assert (restored / "evidence" / "proof.txt").read_text(encoding="utf-8") == "observed"
    assert (restored / "workspace.db").is_file()


def test_engine_lock_blocks_a_second_live_owner(tmp_path: Path) -> None:
    path = tmp_path / "motor.lock"
    first = EngineLock(path, "engine-a")
    second = EngineLock(path)
    first.acquire()
    try:
        assert EngineLock.read_owner(path) == {"engine_id": "engine-a", "pid": os.getpid()}
        with pytest.raises(RuntimeError, match="motor ativo"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_forward_plan_rejects_an_occupied_local_port(workspace) -> None:
    from ctfws.models.host import HostCreate

    host = workspace.add_host(HostCreate(name="jump", ip="192.0.2.10"))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        with pytest.raises(ValueError, match="ocupada"):
            PivotService(workspace).add_forward(
                ForwardCreate(
                    name="occupied",
                    via_host_id=host.id,
                    local_port=int(listener.getsockname()[1]),
                    target_address="10.0.0.5",
                    target_port=80,
                )
            )
    finally:
        listener.close()


def test_backup_rejects_symbolic_link_entries(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(info, "outside")
    with pytest.raises(ValueError, match="link simbólico"):
        WorkspaceBackupService().restore(archive, tmp_path / "restored")
