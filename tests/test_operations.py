import os
import time
from pathlib import Path

import pytest

from ctfws.database.repositories import ObservationRepository
from ctfws.models.audit import AuditCreate
from ctfws.models.connection import ConnectionProfileCreate
from ctfws.models.forward import ForwardCreate, ForwardKind
from ctfws.models.host import HostCreate
from ctfws.models.session import SessionCreate, SessionStatus, SessionTransport
from ctfws.models.snapshot import SnapshotCreate
from ctfws.pivot.tools import generate_forward_command
from ctfws.services.access_paths import AccessPathService
from ctfws.services.doctor import WorkspaceDoctor
from ctfws.services.enumeration import EnumerationService
from ctfws.services.maintenance import WorkspaceMaintenanceService, workspace_usage
from ctfws.services.pivot import PivotService
from ctfws.services.sessions import SessionService


def test_imports_record_provenance_and_paths(workspace) -> None:
    router = workspace.add_host(HostCreate(name="router", ip="10.10.10.20"))
    target = workspace.add_host(HostCreate(name="db01", ip="10.10.10.30"))
    enum = EnumerationService(workspace)
    snapshot = workspace.snapshots.create(
        SnapshotCreate(name="round-01", purpose="initial network evidence")
    )
    enum.import_ip_addr(
        router.name,
        "2: eth0: <BROADCAST>\n    inet 10.10.10.20/24 scope global eth0\n",
        snapshot_id=snapshot.id,
    )
    enum.import_ip_addr(
        target.name,
        "2: eth0: <BROADCAST>\n    inet 10.10.10.30/24 scope global eth0\n",
    )

    sources = workspace.sources.list(router.id)
    assert sources
    assert sources[0].snapshot_id == snapshot.id
    assert len(sources[0].content_hash) == 64
    network_rows = ObservationRepository(workspace.database, workspace.lab.id).list_table(
        "networks"
    )
    assert json_reachable(network_rows, router.id)

    result = AccessPathService(workspace).rebuild()
    assert result.paths >= 2
    paths = workspace.access_paths.list(target.id)
    assert any(router.id in path.hop_host_ids for path in paths)


def test_sessions_and_health_checks(workspace) -> None:
    host = workspace.add_host(HostCreate(name="jump", ip="10.10.10.20"))
    service = SessionService(workspace)
    session = service.add(
        SessionCreate(
            name="ssh-jump",
            host_id=host.id,
            transport=SessionTransport.SSH,
            user="kali",
            endpoint="10.10.10.20:22",
            pid=os.getpid(),
        )
    )
    checked = service.check(session.id)
    assert checked.status == SessionStatus.ACTIVE
    assert checked.health == "process_alive"
    assert service.close(session.id).status == SessionStatus.CLOSED


def test_forward_transport_metadata_and_backup_restore(workspace, tmp_path: Path) -> None:
    host = workspace.add_host(HostCreate(name="jump", ip="10.10.10.20", user="kali"))
    session = SessionService(workspace).add(
        SessionCreate(name="ligolo", host_id=host.id, transport=SessionTransport.LIGOLO_AGENT)
    )
    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="internal-db",
            via_host_id=host.id,
            # Use an OS-assigned port so this metadata/backup test cannot
            # collide with an ephemeral reservation from another workspace.
            local_port=0,
            target_address="172.16.50.20",
            target_port=3306,
            endpoint="proxy.example:8080",
            fingerprint="sha256:abc",
            auth_ref="CHISEL_AUTH",
            session_id=session.id,
            tool="chisel",
        )
    )
    assert "proxy.example:8080" in forward.command
    assert forward.fingerprint == "sha256:abc"

    backup = tmp_path / "workspace-backup.db"
    result = workspace.database.backup_to(backup)
    assert result == backup.resolve()
    workspace.add_host(HostCreate(name="temporary", ip="10.10.10.99"))
    workspace.database.restore_from(backup)
    workspace.database.initialize()
    assert workspace.hosts.get("temporary") is None
    assert workspace.hosts.get("jump") is not None


def test_transport_adapters_generate_reviewable_commands() -> None:
    common = {
        "name": "pivot",
        "via_host_id": 1,
        "local_port": 1080,
        "kind": ForwardKind.DYNAMIC,
        "endpoint": "proxy.example:8080",
    }
    ssh = generate_forward_command(ForwardCreate(tool="ssh", **common), "10.0.0.2", "kali")
    chisel = generate_forward_command(ForwardCreate(tool="chisel", **common), "10.0.0.2", None)
    ligolo = generate_forward_command(
        ForwardCreate(tool="ligolo-ng", fingerprint="sha256:abc", **common),
        "10.0.0.2",
        None,
    )
    assert "ExitOnForwardFailure=yes" in ssh
    assert "proxy.example:8080" in chisel and "socks" in chisel
    assert "ligolo-agent" in ligolo and "sha256:abc" in ligolo


def test_forward_profile_command_includes_jump_chain(workspace) -> None:
    jump = workspace.connections.create(
        ConnectionProfileCreate(name="bastion", host="192.0.2.10", user="kali", port=2200)
    )
    target = workspace.connections.create(
        ConnectionProfileCreate(
            name="internal",
            host="198.51.100.20",
            user="analyst",
            jump_profile_ids=(jump.id,),
        )
    )
    host = workspace.add_host(HostCreate(name="internal", ip="198.51.100.20"))
    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="internal-http",
            via_host_id=host.id,
            connection_id=target.id,
            local_port=0,
            target_address="10.20.0.10",
            target_port=80,
        )
    )
    assert "-J" in forward.command
    assert "kali@192.0.2.10:2200" in forward.command


def test_doctor_reports_healthy_workspace(workspace) -> None:
    report = WorkspaceDoctor().run(workspace.paths)
    assert report.ok
    assert any(check.name == "database-integrity" and check.ok for check in report.checks)


def test_optional_web_app_has_read_only_routes(workspace) -> None:
    pytest.importorskip("fastapi")
    from ctfws.web import create_app

    app = create_app(workspace.paths)
    route_paths = {route.path for route in app.routes}
    assert "/api/summary" in route_paths
    assert "/api/paths/rebuild" in route_paths
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get(f"/api/v2/workspaces/{workspace.lab.id}/capabilities")
    assert response.status_code == 200
    assert {item["name"] for item in response.json()["transport_adapters"]} == {
        "ssh",
        "chisel",
        "ligolo-ng",
        "nc",
        "gsocket",
    }


def test_web_tunnel_preview_does_not_persist_a_forward(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.models.host import HostCreate
    from ctfws.web import create_app

    host = workspace.add_host(HostCreate(name="preview", ip="192.0.2.90"))
    with TestClient(create_app(workspace.paths)) as client:
        response = client.post(
            f"/api/v2/workspaces/{workspace.lab.id}/tunnels/preview",
            json={
                "name": "preview-tunnel",
                "via_host_id": host.id,
                "tool": "nc",
                "kind": "dynamic",
                "local_port": 0,
            },
        )
        assert response.status_code == 200
        assert response.json()["role"] == "server"
        assert response.json()["execution_location"] == "motor"
        assert client.get(f"/api/v2/workspaces/{workspace.lab.id}/tunnels").json() == []


def test_file_limit_is_operator_configurable(workspace, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    monkeypatch.setenv("CTFWS_MAX_FILE_BYTES", "1048576")
    with TestClient(create_app(workspace.paths)) as client:
        response = client.post(
            f"/api/v2/workspaces/{workspace.lab.id}/files/upload",
            params={"destination": "loot/inbox"},
            files={"file": ("over-limit.bin", b"x" * (1048576 + 1))},
        )
    assert response.status_code == 413


def test_workspace_quota_and_retention_never_remove_evidence(workspace, monkeypatch) -> None:
    old_log = workspace.paths.root / "logs" / "old.log"
    old_part = workspace.paths.root / "loot" / ".upload.ctfws-0123456789abcdef.part"
    evidence = workspace.paths.root / "evidence" / "keep.txt"
    old_log.write_text("old log", encoding="utf-8")
    old_part.write_bytes(b"partial")
    evidence.write_text("preserve", encoding="utf-8")
    old_time = time.time() - 3 * 86400
    os.utime(old_log, (old_time, old_time))
    os.utime(old_part, (old_time, old_time))
    os.utime(evidence, (old_time, old_time))

    monkeypatch.setenv("CTFWS_RETENTION_DAYS", "1")
    service = WorkspaceMaintenanceService(workspace)
    preview = service.inspect()
    assert "logs/old.log" in preview.removable
    assert "loot/.upload.ctfws-0123456789abcdef.part" in preview.removable
    assert "evidence/keep.txt" not in preview.removable
    after = service.prune()
    assert after.removable == ()
    assert not old_log.exists()
    assert not old_part.exists()
    assert evidence.read_text(encoding="utf-8") == "preserve"

    quota_floor_file = workspace.paths.root / "loot" / "quota-floor.bin"
    quota_floor_file.write_bytes(b"x" * (10 * 1024 * 1024))
    monkeypatch.setenv("CTFWS_MAX_WORKSPACE_BYTES", str(workspace_usage(workspace.paths.root) + 1))
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    with TestClient(create_app(workspace.paths)) as client:
        response = client.post(
            f"/api/v2/workspaces/{workspace.lab.id}/files/upload",
            params={"destination": "loot/inbox"},
            files={"file": ("quota.bin", b"too-large-for-quota")},
        )
    assert response.status_code == 413


def test_web_queues_cataloged_tool_transfer(workspace, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.models.tool import ToolCreate
    from ctfws.services.transfers import SFTPTransferService, TransferResult
    from ctfws.web import create_app

    profile = workspace.connections.create(
        ConnectionProfileCreate(name="tool-target", host="192.0.2.42", user="analyst")
    )
    source = workspace.paths.root / "loot" / "tool"
    source.write_bytes(b"safe tool")
    tool = workspace.tools.create(ToolCreate(name="tool", path=str(source)), 9, "0" * 64)

    async def fake_upload_tool_async(
        self, connection_id, tool_id, remote_path, *, overwrite=False, conflict="cancel"
    ):
        del self, connection_id, tool_id, overwrite, conflict
        return TransferResult("upload", "tool:1:tool", remote_path, 9, "0" * 64, "", None, True)

    def fake_upload_tool(
        self, connection_id, tool_id, remote_path, *, overwrite=False, conflict="cancel"
    ):
        del self, connection_id, tool_id, overwrite, conflict
        return TransferResult("upload", "tool:1:tool", remote_path, 9, "0" * 64, "", None, False)

    monkeypatch.setattr(SFTPTransferService, "upload_tool_async", fake_upload_tool_async)
    monkeypatch.setattr(SFTPTransferService, "upload_tool", fake_upload_tool)
    with TestClient(create_app(workspace.paths)) as client:
        response = client.post(
            f"/api/v2/workspaces/{workspace.lab.id}/tools/{tool.id}/transfer/{profile.id}",
            headers={"Idempotency-Key": "tool-transfer-1"},
            json={"remote_path": "/tmp/tool"},
        )
        assert response.status_code == 202
        assert response.json()["job_id"] >= 1
    assert workspace.tasks.list()[0].kind == "tool_transfer"


def test_web_lifespan_owns_one_workspace_motor(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    first = create_app(workspace.paths)
    second = create_app(workspace.paths)
    with TestClient(first):
        assert (workspace.paths.root / "motor.lock").is_file()
        with pytest.raises(RuntimeError, match="motor ativo"):
            with TestClient(second):
                pass
    assert not (workspace.paths.root / "motor.lock").exists()


def test_web_auth_required_protects_v1_and_accepts_bootstrap(workspace, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    monkeypatch.setenv("CTFWS_AUTH_REQUIRED", "1")
    monkeypatch.setenv("CTFWS_BOOTSTRAP_TOKEN", "test-bootstrap-token")
    client = TestClient(create_app(workspace.paths))
    assert client.get("/api/v1/summary").status_code == 401
    login = client.post("/api/v2/auth/login", json={"bootstrap_token": "test-bootstrap-token"})
    assert login.status_code == 200
    token = login.json()["token"]
    assert (
        client.get("/api/v1/summary", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )


def test_web_errors_have_stable_retry_metadata(workspace) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.web import create_app

    with TestClient(create_app(workspace.paths)) as client:
        response = client.get(f"/api/v2/workspaces/{workspace.lab.id + 1}/summary")

    payload = response.json()
    assert response.status_code == 404
    assert payload["code"] == "http_404"
    assert payload["retryable"] is False
    assert payload["diagnostic_id"]
    assert payload["detail"] == "Workspace não encontrado."


def test_web_internal_errors_have_safe_diagnostic_metadata(workspace, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import ctfws.web as web_module

    monkeypatch.setattr(web_module, "_summary", lambda *_args: 1 / 0)
    with TestClient(
        web_module.create_app(workspace.paths), raise_server_exceptions=False
    ) as client:
        response = client.get("/api/summary")

    payload = response.json()
    assert response.status_code == 500
    assert payload["code"] == "internal_error"
    assert payload["retryable"] is True
    assert payload["diagnostic_id"]
    assert "traceback" not in payload["detail"].lower()


def test_workspace_memberships_limit_effective_role(workspace, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.models.membership import MembershipCreate, WorkspaceRole
    from ctfws.web import create_app

    monkeypatch.setenv("CTFWS_AUTH_REQUIRED", "1")
    monkeypatch.setenv("CTFWS_BOOTSTRAP_TOKEN", "membership-bootstrap")
    monkeypatch.setenv("CTFWS_REQUIRE_MEMBERSHIP", "1")
    with TestClient(create_app(workspace.paths)) as client:
        login = client.post("/api/v2/auth/login", json={"bootstrap_token": "membership-bootstrap"})
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        workspace_id = workspace.lab.id
        assert (
            client.get(f"/api/v2/workspaces/{workspace_id}/summary", headers=headers).status_code
            == 200
        )

        created = client.post(
            f"/api/v2/workspaces/{workspace_id}/members",
            headers=headers,
            json={"subject": "alice", "role": "operator"},
        )
        assert created.status_code == 201
        assert created.json()["role"] == "operator"

        workspace.memberships.upsert(
            MembershipCreate(subject="bootstrap-admin", role=WorkspaceRole.OBSERVER)
        )
        denied = client.post(
            f"/api/v2/workspaces/{workspace_id}/connections",
            headers=headers,
            json={"name": "denied", "host": "192.0.2.10", "user": "kali"},
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "insufficient_role"
        denied_legacy = client.post(
            "/api/v1/connections",
            headers=headers,
            json={"name": "denied-v1", "host": "192.0.2.11", "user": "kali"},
        )
        assert denied_legacy.status_code == 403

        workspace.memberships.upsert(
            MembershipCreate(subject="bootstrap-admin", role=WorkspaceRole.ADMIN)
        )
        allowed = client.get(f"/api/v2/workspaces/{workspace_id}/members", headers=headers)
        assert allowed.status_code == 200
        assert {item["subject"] for item in allowed.json()} == {"alice", "bootstrap-admin"}
        protected = client.delete(
            f"/api/v2/workspaces/{workspace_id}/members/bootstrap-admin",
            headers=headers,
        )
        assert protected.status_code == 409


def test_team_vault_secret_creation_requires_workspace_admin(workspace, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ctfws.models.membership import MembershipCreate, WorkspaceRole
    from ctfws.web import create_app

    monkeypatch.setenv("CTFWS_AUTH_REQUIRED", "1")
    monkeypatch.setenv("CTFWS_BOOTSTRAP_TOKEN", "vault-bootstrap")
    monkeypatch.setenv("CTFWS_REQUIRE_MEMBERSHIP", "1")
    with TestClient(create_app(workspace.paths)) as client:
        admin_login = client.post("/api/v2/auth/login", json={"bootstrap_token": "vault-bootstrap"})
        assert admin_login.status_code == 200
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['token']}"}

        account = client.post(
            "/api/v2/auth/accounts",
            headers=admin_headers,
            json={
                "username": "operator-vault",
                "password": "operator-vault-password",
                "role": "operator",
            },
        )
        assert account.status_code == 201
        workspace.memberships.upsert(
            MembershipCreate(subject="operator-vault", role=WorkspaceRole.OPERATOR)
        )
        operator_login = client.post(
            "/api/v2/auth/login",
            json={"username": "operator-vault", "password": "operator-vault-password"},
        )
        assert operator_login.status_code == 200
        operator_headers = {"Authorization": f"Bearer {operator_login.json()['token']}"}

        denied = client.post(
            "/api/v2/auth/secrets",
            headers=operator_headers,
            json={"name": "shared-key", "value": "temporary-secret"},
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "http_403"

        allowed = client.post(
            "/api/v2/auth/secrets",
            headers=admin_headers,
            json={"name": "shared-key", "value": "temporary-secret"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["auth_ref"].startswith("vault:")


def test_audit_repository_redacts_sensitive_detail_keys(workspace) -> None:
    item = workspace.audit.create(
        AuditCreate(
            actor="operator",
            action="test",
            resource_type="fixture",
            result="succeeded",
            correlation_id="audit-test",
            details={"password": "never-store", "nested": {"token": "also-secret"}},
        )
    )
    assert item.details == {"password": "[redacted]", "nested": {"token": "[redacted]"}}


def json_reachable(rows: list[dict[str, object]], host_id: int) -> bool:
    import json

    return any(host_id in json.loads(str(row["reachable_via_json"])) for row in rows)
