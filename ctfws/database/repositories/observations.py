"""Repositories for imported network observations."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from ctfws.core.time import utc_now
from ctfws.database.db import Database
from ctfws.parsers.ip_addr import InterfaceRecord
from ctfws.parsers.ip_neigh import NeighborRecord
from ctfws.parsers.ip_route import RouteRecord
from ctfws.parsers.ss import SocketRecord
from ctfws.parsers.system import SystemFacts


class ObservationRepository:
    """Persist idempotent observations for one lab."""

    def __init__(self, database: Database, lab_id: int) -> None:
        self.database = database
        self.lab_id = lab_id

    def save_interfaces(
        self,
        host_id: int,
        records: Iterable[InterfaceRecord],
        source_id: int | None = None,
        network_scope: str = "default",
    ) -> int:
        count = 0
        with self.database.connection() as connection:
            for record in records:
                for address in record.addresses:
                    connection.execute(
                        """
                        INSERT INTO interfaces(
                            lab_id, host_id, name, ip, prefix, family, network, mac, state,
                            observed_at, source_id, network_scope
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(host_id, name, ip) DO UPDATE SET
                            prefix=excluded.prefix, family=excluded.family,
                            network=excluded.network,
                            mac=excluded.mac, state=excluded.state,
                            observed_at=excluded.observed_at, source_id=excluded.source_id,
                            network_scope=excluded.network_scope
                        """,
                        (
                            self.lab_id,
                            host_id,
                            record.name,
                            address.address,
                            address.prefix,
                            address.family,
                            address.network,
                            record.mac,
                            record.state,
                            utc_now(),
                            source_id,
                            network_scope,
                        ),
                    )
                    count += 1
        return count

    def save_network(
        self,
        cidr: str,
        gateway: str | None = None,
        reachable_via: Iterable[int] = (),
        source_id: int | None = None,
        scope: str = "default",
    ) -> int:
        """Create or update a network and return its ID."""

        now = utc_now()
        with self.database.connection() as connection:
            existing = connection.execute(
                "SELECT reachable_via_json FROM networks "
                "WHERE lab_id = ? AND scope = ? AND cidr = ?",
                (self.lab_id, scope, cidr),
            ).fetchone()
            previous = (
                set(json.loads(existing["reachable_via_json"] or "[]")) if existing else set()
            )
            via_json = json.dumps(sorted(previous | set(reachable_via)))
            connection.execute(
                """
                INSERT INTO networks(
                    lab_id, cidr, scope, gateway, reachable_via_json, observed_at, source_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lab_id, scope, cidr) DO UPDATE SET
                    gateway=COALESCE(excluded.gateway, networks.gateway),
                    reachable_via_json=excluded.reachable_via_json,
                    observed_at=excluded.observed_at,
                    source_id=COALESCE(excluded.source_id, networks.source_id)
                """,
                (self.lab_id, cidr, scope, gateway, via_json, now, source_id),
            )
            row = connection.execute(
                "SELECT id FROM networks WHERE lab_id = ? AND scope = ? AND cidr = ?",
                (self.lab_id, scope, cidr),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def save_routes(
        self, host_id: int, records: Iterable[RouteRecord], source_id: int | None = None
    ) -> int:
        count = 0
        with self.database.connection() as connection:
            for record in records:
                where = "host_id = ? AND destination = ? AND via IS ? AND dev IS ?"
                params = (host_id, record.destination, record.via, record.dev)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO routes(
                        lab_id, host_id, destination, via, dev, metric, source, observed_at,
                        source_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.lab_id,
                        host_id,
                        record.destination,
                        record.via,
                        record.dev,
                        record.metric,
                        record.source,
                        utc_now(),
                        source_id,
                    ),
                )
                connection.execute(
                    "UPDATE routes SET metric = ?, source = ?, observed_at = ?, source_id = ? "
                    "WHERE " + where,
                    (record.metric, record.source, utc_now(), source_id, *params),
                )
                count += 1
        return count

    def save_neighbors(
        self, host_id: int, records: Iterable[NeighborRecord], source_id: int | None = None
    ) -> int:
        count = 0
        with self.database.connection() as connection:
            for record in records:
                connection.execute(
                    """
                    INSERT INTO neighbors(
                        lab_id, host_id, ip, dev, mac, state, observed_at, source_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(host_id, ip) DO UPDATE SET
                        dev=excluded.dev, mac=excluded.mac, state=excluded.state,
                        observed_at=excluded.observed_at, source_id=excluded.source_id
                    """,
                    (
                        self.lab_id,
                        host_id,
                        record.ip,
                        record.dev,
                        record.mac,
                        record.state,
                        utc_now(),
                        source_id,
                    ),
                )
                count += 1
        return count

    def save_sockets(
        self, host_id: int, records: Iterable[SocketRecord], source_id: int | None = None
    ) -> tuple[int, int]:
        """Persist sockets as services or connections and return both counts."""

        services = 0
        connections = 0
        with self.database.connection() as connection:
            for record in records:
                if record.is_listener:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO services(
                            lab_id, host_id, address, port, protocol, state, process,
                            description, observed_at, source_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self.lab_id,
                            host_id,
                            record.local_address,
                            record.local_port,
                            record.protocol,
                            record.state,
                            record.process,
                            _service_description(record.local_port),
                            utc_now(),
                            source_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE services SET state = ?, process = ?, description = ?,
                            observed_at = ?, source_id = ?
                        WHERE host_id = ? AND address = ? AND port IS ? AND protocol = ?
                        """,
                        (
                            record.state,
                            record.process,
                            _service_description(record.local_port),
                            utc_now(),
                            source_id,
                            host_id,
                            record.local_address,
                            record.local_port,
                            record.protocol,
                        ),
                    )
                    services += 1
                else:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO connections(
                            lab_id, host_id, protocol, local_address, local_port,
                            remote_address, remote_port, state, process, observed_at, source_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self.lab_id,
                            host_id,
                            record.protocol,
                            record.local_address,
                            record.local_port,
                            record.peer_address,
                            record.peer_port,
                            record.state,
                            record.process,
                            utc_now(),
                            source_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE connections SET
                            state = ?, process = ?, observed_at = ?, source_id = ?
                        WHERE host_id = ? AND protocol = ? AND local_address = ?
                          AND local_port IS ? AND remote_address IS ? AND remote_port IS ?
                        """,
                        (
                            record.state,
                            record.process,
                            utc_now(),
                            source_id,
                            host_id,
                            record.protocol,
                            record.local_address,
                            record.local_port,
                            record.peer_address,
                            record.peer_port,
                        ),
                    )
                    connections += 1
        return services, connections

    def save_hosts_entries(
        self,
        host_id: int,
        entries: Iterable[tuple[str, str]],
        source: str,
        source_id: int | None = None,
    ) -> int:
        count = 0
        with self.database.connection() as connection:
            for address, hostname in entries:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO dns_entries(
                        lab_id, host_id, address, hostname, source, observed_at, source_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (self.lab_id, host_id, address, hostname, source, utc_now(), source_id),
                )
                count += 1
        return count

    def save_command(
        self,
        host_id: int | None,
        command: str,
        output: str,
        source_id: int | None = None,
        snapshot_id: int | None = None,
    ) -> int:
        with self.database.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO commands(
                    lab_id, host_id, command, output, created_at, source_id, snapshot_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (self.lab_id, host_id, command, output, utc_now(), source_id, snapshot_id),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite não retornou o ID do comando.")
            return cursor.lastrowid

    def save_system_facts(
        self, host_id: int, facts: SystemFacts, source_id: int | None = None
    ) -> int:
        """Create or replace the latest normalized facts for a host."""

        with self.database.connection() as connection:
            connection.execute(
                """
                INSERT INTO system_facts(
                    lab_id, host_id, user, uid, groups_json, hostname, fqdn, kernel,
                    architecture, distribution, observed_at, source_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    user=excluded.user, uid=excluded.uid, groups_json=excluded.groups_json,
                    hostname=excluded.hostname, fqdn=excluded.fqdn, kernel=excluded.kernel,
                    architecture=excluded.architecture, distribution=excluded.distribution,
                    observed_at=excluded.observed_at, source_id=excluded.source_id
                """,
                (
                    self.lab_id,
                    host_id,
                    facts.user,
                    facts.uid,
                    json.dumps(list(facts.groups)),
                    facts.hostname,
                    facts.fqdn,
                    facts.kernel,
                    facts.architecture,
                    facts.distribution,
                    utc_now(),
                    source_id,
                ),
            )
            row = connection.execute(
                "SELECT id FROM system_facts WHERE host_id = ?", (host_id,)
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def list_table(self, table: str, host_id: int | None = None) -> list[dict[str, Any]]:
        """List rows for read-only views; table names are an internal allowlist."""

        allowed = {
            "interfaces",
            "networks",
            "routes",
            "neighbors",
            "services",
            "connections",
            "dns_entries",
            "commands",
            "system_facts",
        }
        if table not in allowed:
            raise ValueError(f"Tabela de observação inválida: {table}")
        query = (
            "SELECT networks.*, networks.scope AS network_scope "
            "FROM networks WHERE networks.lab_id = ?"
            if table == "networks"
            else f"SELECT * FROM {table} WHERE lab_id = ?"
        )
        params: list[object] = [self.lab_id]
        if host_id is not None and table != "networks":
            query += " AND host_id = ?"
            params.append(host_id)
        query += " ORDER BY id"
        with self.database.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def _service_description(port: int | None) -> str | None:
    """Give a cautious hint based only on a well-known port."""

    descriptions = {
        22: "SSH",
        53: "DNS?",
        80: "HTTP?",
        443: "HTTPS?",
        3306: "MySQL",
        5432: "PostgreSQL",
        6379: "Redis?",
        8080: "HTTP?",
    }
    return descriptions.get(port) if port is not None else None
