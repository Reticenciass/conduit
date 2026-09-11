from pathlib import Path

from ctfws.models.host import HostCreate
from ctfws.services.enumeration import EnumerationService

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_imports_are_persisted_idempotently(workspace) -> None:
    host = workspace.add_host(HostCreate(name="web01", ip="10.10.10.20"))
    service = EnumerationService(workspace)

    first = service.import_ip_addr(host.name, fixture("ip_addr.txt"))
    second = service.import_ip_addr(host.name, fixture("ip_addr.txt"))
    route = service.import_route(host.name, fixture("ip_route.txt"))
    neigh = service.import_neigh(host.name, fixture("ip_neigh.txt"))
    sockets = service.import_ss(host.name, fixture("ss.txt"))
    hosts = service.import_hosts(host.name, fixture("hosts.txt"))
    system = service.import_system(host.name, fixture("system.env"))

    observations = service.observations
    assert first.details == {"interfaces": 3, "networks": 3}
    assert second.details == first.details
    assert route.details["routes"] == 3
    assert neigh.details["neighbors"] == 3
    assert sockets.details == {"services": 3, "connections": 1}
    assert hosts.details["entries"] == 5
    assert system.details["facts"] == 1
    assert len(observations.list_table("interfaces", host.id)) == 3
    assert len(observations.list_table("networks")) == 3
    assert len(observations.list_table("routes", host.id)) == 3
    assert len(observations.list_table("neighbors", host.id)) == 3
    assert len(observations.list_table("services", host.id)) == 3
    assert len(observations.list_table("connections", host.id)) == 1
    assert len(observations.list_table("dns_entries", host.id)) == 5
    assert observations.list_table("system_facts", host.id)[0]["hostname"] == "web01"


def test_import_requires_known_host(workspace) -> None:
    service = EnumerationService(workspace)

    try:
        service.import_ip_addr("missing", "1: lo: <LOOPBACK>")
    except Exception as error:
        assert "não encontrado" in str(error)
    else:
        raise AssertionError("importar em host inexistente deveria falhar")


def test_import_quick_agent_payload(workspace) -> None:
    host = workspace.add_host(HostCreate(name="web01", ip="10.10.10.20"))
    results = EnumerationService(workspace).import_quick(
        host.name,
        {
            "format": "ctfws-agent-v1",
            "system": {"USER": "www-data", "UID": "33", "HOSTNAME": "web01"},
            "outputs": {"ip_addr": "1: lo: <LOOPBACK>\n    inet 127.0.0.1/8 scope host lo"},
        },
    )

    assert [result.parser for result in results] == ["system", "ip_addr"]
