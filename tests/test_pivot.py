import socket

import pytest

from ctfws.models.forward import (
    ExecutionLocation,
    ForwardCreate,
    ForwardKind,
    TransportRole,
)
from ctfws.models.host import HostCreate
from ctfws.pivot.adapters import transport_capabilities
from ctfws.pivot.tools import build_forward_plan, generate_forward_command
from ctfws.services.pivot import PivotService
from ctfws.services.processes import ForwardProcessService


def test_forward_plan_is_stored_without_execution(workspace) -> None:
    host = workspace.add_host(HostCreate(name="web01", ip="10.10.10.20", user="kali"))
    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="internal-mysql",
            via_host_id=host.id,
            local_port=3306,
            target_address="172.16.50.20",
            target_port=3306,
        )
    )

    assert "ssh" in forward.command
    assert "172.16.50.20:3306" in forward.command
    assert forward.command_argv[0] == "ssh"
    assert "-L" in forward.command_argv
    assert workspace.forwards.list()[0].status.value == "planned"


def test_dynamic_ssh_command() -> None:
    command = generate_forward_command(
        ForwardCreate(
            name="socks",
            via_host_id=1,
            kind=ForwardKind.DYNAMIC,
            local_port=1080,
        ),
        "10.10.10.20",
        "kali",
    )

    assert "ssh" in command
    assert "-D" in command
    assert "127.0.0.1:1080" in command


def test_transport_contract_declares_role_location_and_limitations() -> None:
    plan = build_forward_plan(
        ForwardCreate(
            name="chisel-server",
            via_host_id=1,
            tool="chisel",
            kind=ForwardKind.DYNAMIC,
            endpoint=":8080",
            role=TransportRole.SERVER,
        ),
        "127.0.0.1",
        None,
    )

    assert plan.role == TransportRole.SERVER.value
    assert plan.execution_location == ExecutionLocation.MOTOR.value
    assert "chisel" in plan.command
    assert "--reverse" in plan.command
    assert {item.name for item in transport_capabilities()} == {
        "ssh",
        "chisel",
        "ligolo-ng",
        "nc",
        "gsocket",
    }

    with pytest.raises(ValueError, match="não suporta o papel"):
        build_forward_plan(
            ForwardCreate(
                name="ssh-server",
                via_host_id=1,
                role=TransportRole.SERVER,
                target_address="10.0.0.5",
                target_port=80,
            ),
            "127.0.0.1",
            None,
        )


def test_pivot_persists_effective_role_and_execution_location(workspace) -> None:
    host = workspace.add_host(HostCreate(name="pivot", ip="192.0.2.44"))
    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="nc-listener",
            via_host_id=host.id,
            kind=ForwardKind.DYNAMIC,
            local_port=0,
            tool="nc",
        )
    )

    assert forward.role == TransportRole.SERVER
    assert forward.execution_location == ExecutionLocation.MOTOR


def test_automatic_local_port_is_reserved_until_forward_starts(workspace) -> None:
    host = workspace.add_host(HostCreate(name="reservation", ip="192.0.2.47"))
    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="reserved-forward",
            via_host_id=host.id,
            local_port=0,
            target_address="10.0.0.5",
            target_port=80,
        )
    )

    assert forward.local_port > 0
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError):
            probe.bind((forward.local_address, forward.local_port))
    finally:
        probe.close()

    workspace.port_leases.release_for("forward", forward.id)
    released_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        released_probe.bind((forward.local_address, forward.local_port))
    finally:
        released_probe.close()


def test_ligolo_manifest_requires_a_pinned_matching_version(tmp_path, monkeypatch) -> None:
    from ctfws.pivot.manifest import ligolo_manifest_status

    manifest = tmp_path / "compatibility.toml"
    manifest.write_text(
        '[ligolo_ng]\ncontract = "ctfws-routed-context-v1"\nversion = "0.1.2"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CTFWS_LIGOLO_VERSION", "0.1.1")
    assert not ligolo_manifest_status(manifest).valid
    monkeypatch.setenv("CTFWS_LIGOLO_VERSION", "0.1.2")
    status = ligolo_manifest_status(manifest)
    assert status.valid
    assert status.reason == "contrato e versão compatíveis"


def test_ligolo_is_not_executed_by_the_local_motor(workspace) -> None:
    host = workspace.add_host(HostCreate(name="pivot", ip="192.0.2.45"))
    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="ligolo-agent",
            via_host_id=host.id,
            tool="ligolo-ng",
            endpoint="proxy.example:11601",
            execution_location=ExecutionLocation.REMOTE,
        )
    )

    with pytest.raises(RuntimeError, match="executor remoto"):
        ForwardProcessService(workspace).start(forward.id)
    assert workspace.forwards.get(forward.id).status.value == "error"  # type: ignore[union-attr]


def test_legacy_forward_command_requires_a_new_structured_review(workspace) -> None:
    host = workspace.add_host(HostCreate(name="legacy", ip="192.0.2.46"))
    forward = PivotService(workspace).add_forward(
        ForwardCreate(
            name="legacy-command",
            via_host_id=host.id,
            local_port=19001,
            target_address="10.0.0.5",
            target_port=80,
        )
    )
    with workspace.database.connection() as connection:
        connection.execute(
            "UPDATE forwards SET command_argv_json = '[]' WHERE lab_id = ? AND id = ?",
            (workspace.lab.id, forward.id),
        )

    with pytest.raises(RuntimeError, match="argumentos estruturados"):
        ForwardProcessService(workspace).start(forward.id)
    current = workspace.forwards.get(forward.id)
    assert current is not None
    assert current.health == "legacy_command_review_required"
