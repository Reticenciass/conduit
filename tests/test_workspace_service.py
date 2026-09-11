import pytest

from ctfws.core.errors import EntityNotFoundError
from ctfws.models.host import HostCreate
from ctfws.models.note import NoteCreate, NoteEntityType


def test_add_host_persists_event_and_default_name(workspace) -> None:
    host = workspace.add_host(HostCreate(ip="10.10.10.20"))

    assert host.name == "host-10-10-10-20"
    assert [str(item.ip) for item in workspace.hosts.list()] == ["10.10.10.20"]
    events = workspace.events.list()
    assert events[0]["event_type"] == "HOST_ADDED"


def test_host_notes_are_validated_against_known_hosts(workspace) -> None:
    workspace.add_host(HostCreate(name="web01", ip="10.10.10.20"))
    note = workspace.add_note(
        NoteCreate(
            entity_type=NoteEntityType.HOST, entity_id=1, body="eth1 é uma interface interna"
        )
    )

    assert note.entity_type == NoteEntityType.HOST
    assert workspace.notes.list(entity_type="host", entity_id=1)[0].body.startswith("eth1")

    with pytest.raises(EntityNotFoundError):
        workspace.add_note(
            NoteCreate(entity_type=NoteEntityType.HOST, entity_id=999, body="missing")
        )
