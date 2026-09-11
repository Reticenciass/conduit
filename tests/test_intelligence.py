from ctfws.database.repositories import ObservationRepository
from ctfws.models.host import HostCreate
from ctfws.parsers.ss import SocketRecord
from ctfws.services.intelligence import IntelligenceService


def test_alias_and_relationship_engine_uses_observed_evidence(workspace) -> None:
    web = workspace.add_host(HostCreate(name="web01", ip="10.10.10.20"))
    db = workspace.add_host(HostCreate(name="db01", ip="172.16.50.20"))
    observations = ObservationRepository(workspace.database, workspace.lab.id)
    observations.save_hosts_entries(web.id, [(str(web.ip), "web.alias")], "/etc/hosts")
    observations.save_sockets(
        web.id,
        [
            SocketRecord(
                protocol="tcp",
                state="ESTAB",
                local_address=str(web.ip),
                local_port=4444,
                peer_address=str(db.ip),
                peer_port=3306,
                process="app",
            )
        ],
    )

    service = IntelligenceService(workspace)
    assert service.sync_aliases() == 1
    assert service.rebuild_relationships() == 1
    assert service.repository.aliases()[0]["alias"] == "web.alias"
    assert service.repository.relationships()[0]["target_host_id"] == db.id
