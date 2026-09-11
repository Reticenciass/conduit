from ctfws.database.repositories import ObservationRepository
from ctfws.models.host import HostCreate
from ctfws.services.search import DiffService, SearchService


def test_tags_search_and_diff(workspace) -> None:
    host = workspace.add_host(HostCreate(name="web01", ip="10.10.10.20", os="Linux"))
    workspace.tags.add(host.id, "web")
    observations = ObservationRepository(workspace.database, workspace.lab.id)
    observations.save_command(host.id, "ip route", "10.0.0.0/24 dev eth0")
    observations.save_command(host.id, "ip route", "10.0.0.0/24 dev eth0\n172.16.0.0/24 dev eth1")

    assert workspace.tags.hosts_with_tag("web") == {host.id}
    results = SearchService(workspace).search("10.10.10.20")
    assert any(item["kind"] == "Host" for item in results)
    diff = DiffService(workspace).command_diff(host.id, "ip route")
    assert diff["added"] == ["172.16.0.0/24 dev eth1"]
