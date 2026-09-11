from pathlib import Path

from ctfws.parsers import (
    parse_hosts,
    parse_ip_addr,
    parse_ip_neigh,
    parse_ip_route,
    parse_resolv_conf,
    parse_ss,
    parse_system_facts,
)

FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_ip_addr_infers_interfaces_and_networks() -> None:
    records = parse_ip_addr(read_fixture("ip_addr.txt"))

    assert [record.name for record in records] == ["lo", "eth0", "eth1"]
    assert records[1].mac == "02:42:ac:11:00:02"
    assert records[1].addresses[0].network == "10.10.10.0/24"
    assert records[2].addresses[0].address == "172.16.50.4"


def test_parse_routes_and_neighbors() -> None:
    routes = parse_ip_route(read_fixture("ip_route.txt"))
    neighbors = parse_ip_neigh(read_fixture("ip_neigh.txt"))

    assert routes[0].destination == "0.0.0.0/0"
    assert routes[0].via == "10.10.10.1"
    assert routes[1].dev == "eth0"
    assert neighbors[2].state == "FAILED"
    assert neighbors[2].mac is None


def test_parse_sockets_classifies_services_and_connections() -> None:
    records = parse_ss(read_fixture("ss.txt"))

    assert len(records) == 4
    assert records[0].is_listener is True
    assert records[0].local_port == 22
    assert records[1].local_address == "127.0.0.1"
    assert records[2].peer_address == "172.16.50.20"
    assert records[2].peer_port == 3306
    assert records[3].process == "dnsmasq"


def test_parse_hosts_and_resolver() -> None:
    entries = parse_hosts(read_fixture("hosts.txt"))
    resolver = parse_resolv_conf(read_fixture("resolv.conf.txt"))

    assert entries[1].names == ("web01", "web01.lab")
    assert resolver.nameservers == ("172.16.50.10", "1.1.1.1")
    assert resolver.search_domains == ("lab.internal", "corp.internal")


def test_parse_normalized_system_facts() -> None:
    facts = parse_system_facts(read_fixture("system.env"))

    assert facts.user == "www-data"
    assert facts.uid == 33
    assert facts.hostname == "web01"
    assert facts.distribution == "Debian GNU/Linux 12"
