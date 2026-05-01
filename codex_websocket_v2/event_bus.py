"""Small async event bus for Codex WebSocket events."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Awaitable, Callable, DefaultDict

Subscriber = Callable[[Any], Awaitable[bool | None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: DefaultDict[type, list[Subscriber]] = defaultdict(list)

    def subscribe(self, event_type: type, subscriber: Subscriber) -> None:
        self._subscribers[event_type].append(subscriber)

    async def publish(self, event: Any) -> bool:
        handled = False
        for event_type in type(event).__mro__:
            for subscriber in self._subscribers.get(event_type, []):
                handled = True
                consumed = await subscriber(event)
                if consumed is True:
                    return True
        return handled
