from pathlib import Path

from ctfws.core.paths import WorkspacePaths
from ctfws.database.db import Database
from ctfws.database.repositories import ContextRepository, HostRepository, LabRepository
from ctfws.models.context import ContextTransport, NetworkContextCreate
from ctfws.models.host import HostCreate
from ctfws.models.lab import LabCreate
from ctfws.services.doctor import WorkspaceDoctor


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    paths = WorkspacePaths.create(tmp_path / "lab")
    database = Database(paths.database)

    database.initialize()
    database.initialize()

    with database.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        version = connection.execute(
            "SELECT MAX(version) AS version FROM schema_version"
        ).fetchone()

    assert {
        "labs",
        "hosts",
        "notes",
        "events",
        "interfaces",
        "pivots",
        "shells",
        "forwards",
        "evidence",
        "host_tags",
        "snapshots",
        "observation_sources",
        "sessions",
        "access_paths",
        "audit_log",
        "workspace_memberships",
    }.issubset(tables)
    assert version["version"] == 33
    with database.connection() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(forwards)").fetchall()
        }
    assert {"role", "execution_location", "command_argv_json", "engine_id"}.issubset(columns)
    with database.connection() as connection:
        terminal_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(terminal_sessions)").fetchall()
        }
    assert {"owner_subject", "sharing", "engine_id"}.issubset(terminal_columns)
    with database.connection() as connection:
        context_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(network_contexts)").fetchall()
        }
    assert "engine_id" in context_columns


def test_migrate_schema_23_preserves_context_state(tmp_path: Path) -> None:
    paths = WorkspacePaths.create(tmp_path / "lab")
    database = Database(paths.database)
    database.initialize()
    lab = LabRepository(database).create(LabCreate(name="migration"), paths.root)
    host = HostRepository(database, lab.id).create(HostCreate(name="legacy-host", ip="10.30.0.5"))
    context = ContextRepository(database, lab.id).create(
        NetworkContextCreate(
            name="routed",
            transport=ContextTransport.ROUTED,
            network_cidrs=("10.20.0.0/16",),
        ),
        ("routed-context", "10.20.0.0/16"),
    )
    with database.connection() as connection:
        connection.execute(
            """
            INSERT INTO interfaces(
                lab_id, host_id, name, ip, prefix, family, network,
                observed_at, network_scope
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lab.id,
                host.id,
                "eth0",
                "10.30.0.5",
                24,
                "ipv4",
                "10.30.0.0/24",
                "2026-01-01T00:00:00+00:00",
                "default",
            ),
        )
        connection.execute("ALTER TABLE network_contexts DROP COLUMN resource_manifest_json")
        connection.execute("ALTER TABLE network_contexts DROP COLUMN namespace_name")
        connection.execute("DROP TABLE audit_log")
        connection.execute("DROP TABLE workspace_memberships")
        connection.execute("ALTER TABLE connection_profiles DROP COLUMN generation")
        connection.execute("ALTER TABLE connection_profiles DROP COLUMN capabilities_json")
        connection.execute("DELETE FROM schema_version")
        connection.execute("INSERT INTO schema_version(version) VALUES (23)")

    database.initialize()
    migrated = ContextRepository(database, lab.id).get(context.id)
    assert migrated is not None
    assert migrated.name == "routed"
    assert migrated.network_cidrs == ("10.20.0.0/16",)
    assert migrated.namespace_name is None
    assert migrated.resource_manifest == ()
    migrated_host = HostRepository(database, lab.id).get(str(host.id))
    assert migrated_host is not None
    assert str(migrated_host.ip) == "10.30.0.5"
    assert migrated_host.network_scope == "default"
    with database.connection() as connection:
        interface = connection.execute(
            "SELECT host_id, ip FROM interfaces WHERE lab_id = ?",
            (lab.id,),
        ).fetchone()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert interface is not None
    assert interface["host_id"] == host.id
    assert interface["ip"] == "10.30.0.5"
    assert foreign_key_errors == []


def test_doctor_applies_pending_schema_migration(tmp_path: Path) -> None:
    paths = WorkspacePaths.create(tmp_path / "lab")
    database = Database(paths.database)
    database.initialize()
    with database.connection() as connection:
        connection.execute("ALTER TABLE forwards DROP COLUMN command_argv_json")
        connection.execute("DELETE FROM schema_version")
        connection.execute("INSERT INTO schema_version(version) VALUES (31)")

    report = WorkspaceDoctor().run(paths)
    assert report.ok
    with database.connection() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(forwards)").fetchall()
        }
    assert version == 33
    assert {"command_argv_json", "engine_id"}.issubset(columns)
