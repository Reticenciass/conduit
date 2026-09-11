"""Small synchronous event bus used by application services."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ctfws.core.time import utc_now


@dataclass(frozen=True, slots=True)
class Event:
    """An event emitted after a successful state change."""

    event_type: str
    message: str
    entity_type: str | None = None
    entity_id: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


EventHandler = Callable[[Event], None]


class EventBus:
    """Publish events to type-specific and wildcard subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for an event type or `*`."""

        self._subscribers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        """Call matching handlers in registration order."""

        handlers = [*self._subscribers.get("*", []), *self._subscribers.get(event.event_type, [])]
        for handler in handlers:
            handler(event)
