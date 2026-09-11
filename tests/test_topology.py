from pathlib import Path

from ctfws.models.host import HostCreate
from ctfws.services.enumeration import EnumerationService
from ctfws.services.topology import TopologyService

FIXTURES = Path(__file__).parent / "fixtures"


def test_detect_pivot_and_render_topology(workspace, tmp_path: Path) -> None:
    host = workspace.add_host(HostCreate(name="web01", ip="10.10.10.20"))
    enum = EnumerationService(workspace)
    enum.import_ip_addr(host.name, (FIXTURES / "ip_addr.txt").read_text())

    topology = TopologyService(workspace)
    assert topology.detect_pivots() == 1

    text = topology.render_text()
    dot = topology.render_dot()
    assert "172.16.50.0/24" in text
    assert "web01" in text
    assert "digraph ctfws" in dot

    paths = topology.export(tmp_path / "reports")
    assert (tmp_path / "reports" / "topology.txt") in paths
    assert (tmp_path / "reports" / "topology.dot") in paths


def test_overlapping_cidrs_keep_separate_network_scopes(workspace) -> None:
    first = workspace.add_host(HostCreate(name="segment-a", ip="10.0.0.10"))
    second = workspace.add_host(HostCreate(name="segment-b", ip="10.0.0.20"))
    enum = EnumerationService(workspace)
    output = "2: eth0: <BROADCAST>\n    inet {ip}/24 scope global eth0\n"
    enum.import_ip_addr(first.name, output.format(ip="10.0.0.10"), network_scope="blue")
    enum.import_ip_addr(second.name, output.format(ip="10.0.0.20"), network_scope="green")

    rows = enum.observations.list_table("networks")
    assert {(row["scope"], row["cidr"]) for row in rows} == {
        ("blue", "10.0.0.0/24"),
        ("green", "10.0.0.0/24"),
    }
    rendered = TopologyService(workspace).render_text()
    assert "scope=blue" in rendered and "scope=green" in rendered


def test_same_ip_in_isolated_scopes_remains_two_hosts(workspace) -> None:
    first = workspace.add_host(HostCreate(name="blue-host", ip="10.0.0.5", network_scope="blue"))
    second = workspace.add_host(HostCreate(name="green-host", ip="10.0.0.5", network_scope="green"))

    assert first.id != second.id
    assert workspace.hosts.get("10.0.0.5", network_scope="blue").id == first.id
    assert workspace.hosts.get("10.0.0.5", network_scope="green").id == second.id
