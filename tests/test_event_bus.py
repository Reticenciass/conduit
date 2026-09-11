from ctfws.events import Event, EventBus


def test_event_bus_delivers_wildcard_and_specific_handlers() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("*", lambda event: seen.append(f"all:{event.event_type}"))
    bus.subscribe("HOST_ADDED", lambda event: seen.append(f"host:{event.entity_id}"))

    bus.publish(Event(event_type="HOST_ADDED", message="added", entity_id=7))

    assert seen == ["all:HOST_ADDED", "host:7"]
