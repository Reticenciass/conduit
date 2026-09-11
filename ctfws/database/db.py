"""SQLite connection management and migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 33


class Database:
    """A small SQLite gateway with explicit migration handling."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection and close it after use."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Apply all migrations needed by the installed application."""

        with self.connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version " "(version INTEGER NOT NULL)"
            )
            current_row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_version"
            ).fetchone()
            current = int(current_row["version"] or 0)
            if 0 < current < SCHEMA_VERSION:
                self._migration_backup(current)
            if current < 1:
                connection.executescript("""
                    CREATE TABLE labs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        path TEXT NOT NULL UNIQUE,
                        platform TEXT NOT NULL DEFAULT 'unknown',
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE hosts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        ip TEXT NOT NULL,
                        hostname TEXT,
                        os TEXT,
                        user_name TEXT,
                        status TEXT NOT NULL DEFAULT 'discovered',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(lab_id, ip),
                        UNIQUE(lab_id, name)
                    );

                    CREATE TABLE notes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        entity_type TEXT NOT NULL,
                        entity_id INTEGER NOT NULL,
                        body TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        event_type TEXT NOT NULL,
                        entity_type TEXT,
                        entity_id INTEGER,
                        message TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_hosts_lab_id ON hosts(lab_id);
                    CREATE INDEX idx_notes_entity ON notes(lab_id, entity_type, entity_id);
                    CREATE INDEX idx_events_timeline ON events(lab_id, created_at);
                    """)
                connection.execute("INSERT INTO schema_version(version) VALUES (1)")
                current = 1

            if current < 2:
                connection.executescript("""
                    CREATE TABLE interfaces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        ip TEXT NOT NULL,
                        prefix INTEGER NOT NULL,
                        family TEXT NOT NULL,
                        network TEXT NOT NULL,
                        mac TEXT,
                        state TEXT,
                        observed_at TEXT NOT NULL,
                        UNIQUE(host_id, name, ip)
                    );

                    CREATE TABLE networks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        cidr TEXT NOT NULL,
                        gateway TEXT,
                        reachable_via_json TEXT NOT NULL DEFAULT '[]',
                        observed_at TEXT NOT NULL,
                        UNIQUE(lab_id, cidr)
                    );

                    CREATE TABLE routes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        destination TEXT NOT NULL,
                        via TEXT,
                        dev TEXT,
                        metric INTEGER,
                        source TEXT,
                        observed_at TEXT NOT NULL,
                        UNIQUE(host_id, destination, via, dev)
                    );

                    CREATE TABLE neighbors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        ip TEXT NOT NULL,
                        dev TEXT,
                        mac TEXT,
                        state TEXT,
                        observed_at TEXT NOT NULL,
                        UNIQUE(host_id, ip)
                    );

                    CREATE TABLE services (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        address TEXT NOT NULL,
                        port INTEGER,
                        protocol TEXT NOT NULL,
                        state TEXT NOT NULL,
                        process TEXT,
                        description TEXT,
                        observed_at TEXT NOT NULL,
                        UNIQUE(host_id, address, port, protocol)
                    );

                    CREATE TABLE connections (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        protocol TEXT NOT NULL,
                        local_address TEXT NOT NULL,
                        local_port INTEGER,
                        remote_address TEXT,
                        remote_port INTEGER,
                        state TEXT NOT NULL,
                        process TEXT,
                        observed_at TEXT NOT NULL,
                        UNIQUE(
                            host_id, protocol, local_address, local_port,
                            remote_address, remote_port
                        )
                    );

                    CREATE TABLE dns_entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        address TEXT NOT NULL,
                        hostname TEXT NOT NULL,
                        source TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        UNIQUE(host_id, address, hostname, source)
                    );

                    CREATE TABLE commands (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
                        command TEXT NOT NULL,
                        output TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_interfaces_host ON interfaces(host_id);
                    CREATE INDEX idx_networks_lab ON networks(lab_id);
                    CREATE INDEX idx_routes_host ON routes(host_id);
                    CREATE INDEX idx_neighbors_host ON neighbors(host_id);
                    CREATE INDEX idx_services_host ON services(host_id);
                    CREATE INDEX idx_connections_host ON connections(host_id);
                    CREATE INDEX idx_dns_entries_host ON dns_entries(host_id);
                    CREATE INDEX idx_commands_lab ON commands(lab_id, created_at);
                    """)
                connection.execute("INSERT INTO schema_version(version) VALUES (2)")
                current = 2

            if current < 3:
                connection.executescript("""
                    CREATE TABLE system_facts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        user TEXT,
                        uid INTEGER,
                        groups_json TEXT NOT NULL DEFAULT '[]',
                        hostname TEXT,
                        fqdn TEXT,
                        kernel TEXT,
                        architecture TEXT,
                        distribution TEXT,
                        observed_at TEXT NOT NULL,
                        UNIQUE(host_id)
                    );

                    CREATE INDEX idx_system_facts_host ON system_facts(host_id);
                    """)
                connection.execute("INSERT INTO schema_version(version) VALUES (3)")
                current = 3

            if current < 4:
                connection.executescript("""
                    CREATE TABLE pivots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        network_id INTEGER NOT NULL REFERENCES networks(id) ON DELETE CASCADE,
                        reason TEXT NOT NULL,
                        confidence INTEGER NOT NULL DEFAULT 50,
                        status TEXT NOT NULL DEFAULT 'candidate',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(host_id, network_id)
                    );

                    CREATE TABLE host_aliases (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        alias TEXT NOT NULL,
                        source TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(lab_id, host_id, alias)
                    );

                    CREATE TABLE relationships (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        source_host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        target_host_id INTEGER,
                        relation_type TEXT NOT NULL,
                        label TEXT NOT NULL,
                        evidence_json TEXT NOT NULL DEFAULT '{}',
                        confidence INTEGER NOT NULL DEFAULT 50,
                        observed_at TEXT NOT NULL,
                        UNIQUE(source_host_id, target_host_id, relation_type, label)
                    );

                    CREATE INDEX idx_pivots_host ON pivots(host_id);
                    CREATE INDEX idx_pivots_network ON pivots(network_id);
                    CREATE INDEX idx_aliases_alias ON host_aliases(lab_id, alias);
                    CREATE INDEX idx_relationships_source ON relationships(source_host_id);
                    """)
                connection.execute("INSERT INTO schema_version(version) VALUES (4)")
                current = 4

            if current < 5:
                connection.executescript("""
                    CREATE TABLE shells (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        type TEXT NOT NULL,
                        user TEXT,
                        terminal TEXT,
                        status TEXT NOT NULL DEFAULT 'active',
                        notes TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(lab_id, name)
                    );

                    CREATE TABLE tmux_windows (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        session_name TEXT NOT NULL,
                        window_name TEXT NOT NULL,
                        window_index INTEGER,
                        host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
                        status TEXT NOT NULL DEFAULT 'planned',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(lab_id, session_name, window_name)
                    );

                    CREATE INDEX idx_shells_host ON shells(host_id);
                    CREATE INDEX idx_tmux_windows_session ON tmux_windows(lab_id, session_name);
                    """)
                connection.execute("INSERT INTO schema_version(version) VALUES (5)")
                current = 5

            if current < 6:
                connection.executescript("""
                    CREATE TABLE forwards (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        via_host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        local_address TEXT NOT NULL,
                        local_port INTEGER NOT NULL,
                        target_address TEXT,
                        target_port INTEGER,
                        user TEXT,
                        tool TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'planned',
                        command TEXT NOT NULL,
                        command_argv_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(lab_id, name)
                    );

                    CREATE INDEX idx_forwards_host ON forwards(via_host_id);
                    """)
                connection.execute("INSERT INTO schema_version(version) VALUES (6)")
                current = 6

            if current < 7:
                connection.executescript("""
                    CREATE TABLE evidence (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
                        type TEXT NOT NULL,
                        description TEXT NOT NULL,
                        path TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_evidence_lab ON evidence(lab_id, created_at);
                    """)
                connection.execute("INSERT INTO schema_version(version) VALUES (7)")
                current = 7

            if current < 8:
                connection.executescript("""
                    CREATE TABLE host_tags (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
                        tag TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(lab_id, host_id, tag)
                    );

                    CREATE INDEX idx_host_tags_tag ON host_tags(lab_id, tag);
                    """)
                connection.execute("INSERT INTO schema_version(version) VALUES (8)")
                current = 8

            if current < 9:
                connection.execute("ALTER TABLE forwards ADD COLUMN pid INTEGER")
                connection.execute("INSERT INTO schema_version(version) VALUES (9)")
                current = 9

            if current < 10:
                connection.executescript("""
                    CREATE TABLE snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        purpose TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        UNIQUE(lab_id, name)
                    );

                    CREATE TABLE observation_sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
                        snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
                        kind TEXT NOT NULL,
                        command TEXT,
                        origin TEXT,
                        content_hash TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        collected_at TEXT NOT NULL
                    );

                    CREATE TABLE sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
                        name TEXT NOT NULL,
                        transport TEXT NOT NULL,
                        user TEXT,
                        endpoint TEXT,
                        terminal TEXT,
                        pid INTEGER,
                        tmux_session TEXT,
                        status TEXT NOT NULL DEFAULT 'planned',
                        health TEXT,
                        last_checked_at TEXT,
                        error TEXT,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(lab_id, name)
                    );

                    CREATE TABLE access_paths (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        target_host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
                        target_address TEXT NOT NULL,
                        target_port INTEGER,
                        service_id INTEGER REFERENCES services(id) ON DELETE SET NULL,
                        state TEXT NOT NULL DEFAULT 'candidate',
                        confidence INTEGER NOT NULL DEFAULT 50,
                        hop_session_ids_json TEXT NOT NULL DEFAULT '[]',
                        reason TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'inferred',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX idx_snapshots_lab ON snapshots(lab_id, created_at);
                    CREATE INDEX idx_sources_lab ON observation_sources(lab_id, collected_at);
                    CREATE INDEX idx_sources_host ON observation_sources(host_id);
                    CREATE INDEX idx_sessions_lab ON sessions(lab_id, status);
                    CREATE INDEX idx_sessions_host ON sessions(host_id);
                    CREATE INDEX idx_access_paths_lab ON access_paths(lab_id, state);
                    CREATE INDEX idx_access_paths_target
                        ON access_paths(target_host_id, target_port);
                    """)

                for table in (
                    "interfaces",
                    "networks",
                    "routes",
                    "neighbors",
                    "services",
                    "connections",
                    "dns_entries",
                    "system_facts",
                ):
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN source_id INTEGER")
                connection.execute("ALTER TABLE commands ADD COLUMN source_id INTEGER")
                connection.execute("ALTER TABLE commands ADD COLUMN snapshot_id INTEGER")
                connection.execute("ALTER TABLE forwards ADD COLUMN endpoint TEXT")
                connection.execute("ALTER TABLE forwards ADD COLUMN fingerprint TEXT")
                connection.execute("ALTER TABLE forwards ADD COLUMN auth_ref TEXT")
                connection.execute("ALTER TABLE forwards ADD COLUMN session_id INTEGER")
                connection.execute("ALTER TABLE forwards ADD COLUMN health TEXT")
                connection.execute("ALTER TABLE forwards ADD COLUMN last_checked_at TEXT")
                connection.execute("ALTER TABLE forwards ADD COLUMN error TEXT")
                connection.execute("ALTER TABLE forwards ADD COLUMN exit_code INTEGER")
                connection.execute("INSERT INTO schema_version(version) VALUES (10)")
                current = 10

            if current < 11:
                connection.execute(
                    "ALTER TABLE access_paths ADD COLUMN hop_host_ids_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
                connection.execute(
                    "CREATE INDEX idx_access_paths_host_path "
                    "ON access_paths(lab_id, target_host_id)"
                )
                connection.execute("INSERT INTO schema_version(version) VALUES (11)")
                current = 11

            if current < 12:
                connection.executescript("""
                    CREATE TABLE connection_profiles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        transport TEXT NOT NULL DEFAULT 'ssh',
                        host TEXT NOT NULL,
                        port INTEGER NOT NULL DEFAULT 22,
                        user TEXT NOT NULL,
                        identity_file TEXT,
                        known_hosts_file TEXT,
                        auth_ref TEXT,
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(lab_id, name)
                    );

                    CREATE INDEX idx_connection_profiles_lab
                        ON connection_profiles(lab_id, name);
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (12)")
                current = 12

            if current < 13:
                connection.executescript("""
                    CREATE TABLE terminal_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        connection_id INTEGER REFERENCES connection_profiles(id) ON DELETE SET NULL,
                        cwd TEXT,
                        status TEXT NOT NULL DEFAULT 'starting',
                        pid INTEGER,
                        exit_code INTEGER,
                        context_label TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(lab_id, name)
                    );

                    CREATE INDEX idx_terminal_sessions_lab
                        ON terminal_sessions(lab_id, status);
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (13)")
                current = 13

            if current < 14:
                connection.executescript("""
                    ALTER TABLE connection_profiles ADD COLUMN host_id INTEGER;
                    ALTER TABLE connection_profiles ADD COLUMN jump_profile_ids_json
                        TEXT NOT NULL DEFAULT '[]';
                    ALTER TABLE connection_profiles ADD COLUMN auth_method
                        TEXT NOT NULL DEFAULT 'agent_or_key';
                    ALTER TABLE connection_profiles ADD COLUMN state
                        TEXT NOT NULL DEFAULT 'disconnected';
                    ALTER TABLE connection_profiles ADD COLUMN last_checked_at TEXT;
                    ALTER TABLE connection_profiles ADD COLUMN last_error TEXT;
                    ALTER TABLE connection_profiles ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
                    CREATE INDEX idx_connection_profiles_host
                        ON connection_profiles(lab_id, host_id);

                    ALTER TABLE forwards ADD COLUMN connection_id INTEGER;
                    ALTER TABLE forwards ADD COLUMN dependency_ids_json TEXT NOT NULL DEFAULT '[]';
                    ALTER TABLE forwards ADD COLUMN listener_state TEXT;
                    ALTER TABLE forwards ADD COLUMN destination_state TEXT;
                    ALTER TABLE forwards ADD COLUMN process_started_at TEXT;
                    ALTER TABLE forwards ADD COLUMN process_executable TEXT;
                    ALTER TABLE forwards ADD COLUMN process_fingerprint TEXT;
                    CREATE INDEX idx_forwards_connection ON forwards(lab_id, connection_id);
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (14)")
                current = 14

            if current < 15:
                connection.executescript("""
                    CREATE TABLE workspace_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        resource_type TEXT,
                        resource_id INTEGER,
                        status TEXT NOT NULL DEFAULT 'queued',
                        progress INTEGER NOT NULL DEFAULT 0,
                        current_step TEXT,
                        total_steps INTEGER NOT NULL DEFAULT 0,
                        completed_steps INTEGER NOT NULL DEFAULT 0,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        error_code TEXT,
                        error_message TEXT,
                        idempotency_key TEXT,
                        requested_by TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(lab_id, idempotency_key)
                    );
                    CREATE INDEX idx_workspace_tasks_lab
                        ON workspace_tasks(lab_id, created_at DESC);
                    CREATE INDEX idx_workspace_tasks_status ON workspace_tasks(lab_id, status);
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (15)")
                current = 15

            if current < 16:
                connection.executescript("""
                    CREATE TABLE collection_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        host_id INTEGER REFERENCES hosts(id) ON DELETE SET NULL,
                        snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL,
                        status TEXT NOT NULL DEFAULT 'running',
                        parser_version TEXT NOT NULL DEFAULT 'v1',
                        total_steps INTEGER NOT NULL DEFAULT 0,
                        completed_steps INTEGER NOT NULL DEFAULT 0,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX idx_collection_runs_host
                        ON collection_runs(lab_id, host_id, created_at DESC);
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (16)")
                current = 16

            if current < 17:
                connection.executescript("""
                    ALTER TABLE access_paths ADD COLUMN verification_id INTEGER;
                    ALTER TABLE access_paths ADD COLUMN verified_at TEXT;

                    CREATE TABLE access_path_checks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        path_id INTEGER NOT NULL REFERENCES access_paths(id) ON DELETE CASCADE,
                        target_address TEXT NOT NULL,
                        target_port INTEGER NOT NULL,
                        context TEXT NOT NULL,
                        timeout_seconds REAL NOT NULL,
                        status TEXT NOT NULL,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        error TEXT,
                        checked_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );
                    CREATE INDEX idx_access_path_checks_path
                        ON access_path_checks(lab_id, path_id, checked_at DESC);
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (17)")
                current = 17

            if current < 18:
                connection.executescript("""
                    CREATE TABLE network_contexts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        transport TEXT NOT NULL,
                        connection_id INTEGER REFERENCES connection_profiles(id)
                            ON DELETE SET NULL,
                        network_cidrs_json TEXT NOT NULL DEFAULT '[]',
                        local_address TEXT NOT NULL DEFAULT '127.0.0.1',
                        local_port INTEGER,
                        dns_mode TEXT NOT NULL DEFAULT 'path',
                        status TEXT NOT NULL DEFAULT 'planned',
                        command_json TEXT NOT NULL DEFAULT '[]',
                        pid INTEGER,
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(lab_id, name)
                    );
                    CREATE INDEX idx_network_contexts_lab
                        ON network_contexts(lab_id, status);
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (18)")
                current = 18

            if current < 19:
                connection.executescript("""
                    CREATE TABLE transfers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        connection_id INTEGER NOT NULL REFERENCES connection_profiles(id),
                        direction TEXT NOT NULL,
                        local_path TEXT NOT NULL,
                        remote_path TEXT NOT NULL,
                        size INTEGER NOT NULL DEFAULT 0,
                        bytes_transferred INTEGER NOT NULL DEFAULT 0,
                        local_sha256 TEXT,
                        remote_sha256 TEXT,
                        integrity_verified INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'running',
                        error TEXT,
                        requested_by TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX idx_transfers_lab
                        ON transfers(lab_id, created_at DESC);
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (19)")
                current = 19

            if current < 20:
                connection.executescript("""
                    CREATE TABLE tool_catalog (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        path TEXT NOT NULL,
                        architecture TEXT NOT NULL DEFAULT 'unknown',
                        version TEXT,
                        notes TEXT,
                        size INTEGER NOT NULL DEFAULT 0,
                        sha256 TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX idx_tool_catalog_lab
                        ON tool_catalog(lab_id, name);
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (20)")
                current = 20

            if current < 21:
                connection.executescript("""
                    ALTER TABLE network_contexts ADD COLUMN process_started_at TEXT;
                    ALTER TABLE network_contexts ADD COLUMN process_executable TEXT;
                    ALTER TABLE network_contexts ADD COLUMN process_fingerprint TEXT;
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (21)")
                current = 21

            if current < 22:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.executescript("""
                    CREATE TABLE networks_v22 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        cidr TEXT NOT NULL,
                        scope TEXT NOT NULL DEFAULT 'default',
                        gateway TEXT,
                        reachable_via_json TEXT NOT NULL DEFAULT '[]',
                        observed_at TEXT NOT NULL,
                        source_id INTEGER,
                        UNIQUE(lab_id, scope, cidr)
                    );

                    INSERT INTO networks_v22(
                        id, lab_id, cidr, scope, gateway, reachable_via_json, observed_at, source_id
                    )
                    SELECT id, lab_id, cidr, 'default', gateway, reachable_via_json,
                           observed_at, source_id
                    FROM networks;

                    DROP TABLE networks;
                    ALTER TABLE networks_v22 RENAME TO networks;
                    CREATE INDEX idx_networks_lab ON networks(lab_id);
                    ALTER TABLE interfaces ADD COLUMN network_scope
                        TEXT NOT NULL DEFAULT 'default';
                """)
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("INSERT INTO schema_version(version) VALUES (22)")
                current = 22

            if current < 23:
                connection.execute(
                    "ALTER TABLE access_paths ADD COLUMN network_scope "
                    "TEXT NOT NULL DEFAULT 'default'"
                )
                connection.execute("INSERT INTO schema_version(version) VALUES (23)")
                current = 23

            if current < 24:
                connection.executescript("""
                    ALTER TABLE network_contexts ADD COLUMN namespace_name TEXT;
                    ALTER TABLE network_contexts ADD COLUMN resource_manifest_json
                        TEXT NOT NULL DEFAULT '[]';
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (24)")
                current = 24

            if current < 25:
                connection.executescript("""
                    CREATE TABLE audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        actor TEXT NOT NULL,
                        action TEXT NOT NULL,
                        resource_type TEXT NOT NULL,
                        resource_id INTEGER,
                        result TEXT NOT NULL,
                        correlation_id TEXT NOT NULL,
                        details_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX idx_audit_log_lab
                        ON audit_log(lab_id, created_at DESC);
                    CREATE INDEX idx_audit_log_correlation
                        ON audit_log(lab_id, correlation_id);
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (25)")
                current = 25

            if current < 26:
                # PRAGMA foreign_keys only changes outside a transaction.  The
                # previous migration records may have left the connection with
                # a pending write transaction; commit it before rebuilding the
                # parent table so ON DELETE CASCADE cannot erase child rows.
                connection.commit()
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.executescript("""
                    CREATE TABLE hosts_v26 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        ip TEXT NOT NULL,
                        network_scope TEXT NOT NULL DEFAULT 'default',
                        hostname TEXT,
                        os TEXT,
                        user_name TEXT,
                        status TEXT NOT NULL DEFAULT 'discovered',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(lab_id, ip, network_scope),
                        UNIQUE(lab_id, name)
                    );

                    INSERT INTO hosts_v26(
                        id, lab_id, name, ip, network_scope, hostname, os, user_name,
                        status, created_at, updated_at
                    )
                    SELECT id, lab_id, name, ip, 'default', hostname, os, user_name,
                           status, created_at, updated_at
                    FROM hosts;

                    DROP TABLE hosts;
                    ALTER TABLE hosts_v26 RENAME TO hosts;
                    CREATE INDEX idx_hosts_lab_id ON hosts(lab_id);
                """)
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("INSERT INTO schema_version(version) VALUES (26)")
                current = 26

            if current < 27:
                connection.executescript("""
                    CREATE TABLE workspace_memberships (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
                        subject TEXT NOT NULL,
                        role TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(lab_id, subject)
                    );
                    CREATE INDEX idx_workspace_memberships_lab
                        ON workspace_memberships(lab_id, subject);
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (27)")
                current = 27

            if current < 28:
                connection.executescript("""
                    ALTER TABLE connection_profiles ADD COLUMN generation
                        INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE connection_profiles ADD COLUMN capabilities_json
                        TEXT NOT NULL DEFAULT '[]';
                """)
                connection.execute("INSERT INTO schema_version(version) VALUES (28)")
                current = 28

            if current < 29:
                # Some development databases were already created from a
                # forward-compatible schema while their version marker still
                # said 23.  Check columns individually so recovery remains
                # safe and idempotent for those workspaces.
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(forwards)").fetchall()
                }
                if "role" not in columns:
                    connection.execute(
                        "ALTER TABLE forwards ADD COLUMN role TEXT NOT NULL DEFAULT 'client'"
                    )
                if "execution_location" not in columns:
                    connection.execute(
                        "ALTER TABLE forwards ADD COLUMN execution_location "
                        "TEXT NOT NULL DEFAULT 'motor'"
                    )
                connection.execute("INSERT INTO schema_version(version) VALUES (29)")
                current = 29

            if current < 30:
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(network_contexts)").fetchall()
                }
                if "health" not in columns:
                    connection.execute("ALTER TABLE network_contexts ADD COLUMN health TEXT")
                if "capabilities_json" not in columns:
                    connection.execute(
                        "ALTER TABLE network_contexts ADD COLUMN capabilities_json "
                        "TEXT NOT NULL DEFAULT '[]'"
                    )
                connection.execute("INSERT INTO schema_version(version) VALUES (30)")
                current = 30

            if current < 31:
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(terminal_sessions)").fetchall()
                }
                if "owner_subject" not in columns:
                    connection.execute(
                        "ALTER TABLE terminal_sessions ADD COLUMN owner_subject TEXT"
                    )
                if "sharing" not in columns:
                    connection.execute(
                        "ALTER TABLE terminal_sessions ADD COLUMN sharing "
                        "TEXT NOT NULL DEFAULT 'private'"
                    )
                connection.execute("INSERT INTO schema_version(version) VALUES (31)")
                current = 31

            if current < 32:
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(forwards)").fetchall()
                }
                if "command_argv_json" not in columns:
                    connection.execute(
                        "ALTER TABLE forwards ADD COLUMN command_argv_json "
                        "TEXT NOT NULL DEFAULT '[]'"
                    )
                connection.execute("INSERT INTO schema_version(version) VALUES (32)")
                current = 32

            if current < 33:
                for table in ("forwards", "network_contexts", "terminal_sessions"):
                    columns = {
                        str(row["name"])
                        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                    }
                    if "engine_id" not in columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN engine_id TEXT")
                connection.execute("INSERT INTO schema_version(version) VALUES (33)")
                current = 33

            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Banco mais novo ({current}) que esta versão da aplicação ({SCHEMA_VERSION})."
                )

    def _migration_backup(self, current: int) -> None:
        """Create one recoverable copy before changing an existing database."""

        timestamp = datetime.now(UTC).isoformat().replace(":", "").replace("+", "-")
        destination = self.path.parent / "backups" / f"before-migration-v{current}-{timestamp}.db"
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def backup_to(self, destination: Path) -> Path:
        """Create a consistent SQLite backup without copying a live file blindly."""

        source_path = self.path.expanduser().resolve()
        target_path = destination.expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Banco não encontrado: {source_path}")
        if source_path == target_path:
            raise ValueError("O destino do backup precisa ser diferente do banco ativo.")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return target_path

    def restore_from(self, source: Path) -> Path:
        """Restore a SQLite database from a backup into this database path."""

        source_path = source.expanduser().resolve()
        target_path = self.path.expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Backup não encontrado: {source_path}")
        if source_path == target_path:
            raise ValueError("O backup de origem precisa ser diferente do banco ativo.")
        validation = sqlite3.connect(source_path)
        try:
            integrity = str(validation.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            validation.close()
        if integrity != "ok":
            raise ValueError(f"Backup inválido: PRAGMA integrity_check retornou {integrity!r}.")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        source_connection = sqlite3.connect(source_path)
        target_connection = sqlite3.connect(target_path)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        return target_path
