"""Inbound WebSocket frame dispatch for codex-websocket-v2.

Business logic is handled by event subscribers. This module only converts raw
frames into typed events and publishes them on the session event bus.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..events.factory import EventFactory

if TYPE_CHECKING:
    from ..core.session import CodexSession

logger = logging.getLogger(__name__)


def _raw_get(raw: dict, *path: str):
    value = raw
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


class MessageHandler:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session
        self.event_factory = EventFactory(session)

    async def dispatch(self, raw: dict) -> None:
        event = self.event_factory.from_raw(raw)
        params = raw.get("params") if isinstance(raw, dict) else None
        logger.warning(
            "codex event debug: dispatch raw_obj=%s event_obj=%s event_type=%s "
            "method=%r raw_id=%r request_id=%r thread_id=%r item_id=%r "
            "item_type=%r turn_id=%r task_id=%r",
            id(raw),
            id(event),
            type(event).__name__,
            raw.get("method") if isinstance(raw, dict) else None,
            raw.get("id") if isinstance(raw, dict) else None,
            _raw_get(raw, "params", "requestId"),
            _raw_get(raw, "params", "threadId") or _raw_get(raw, "params", "conversationId"),
            _raw_get(raw, "params", "item", "id"),
            _raw_get(raw, "params", "item", "type"),
            _raw_get(raw, "params", "turn", "id"),
            getattr(getattr(event, "task", None), "task_id", None),
        )
        handled = await self.session.event_bus.publish(event)
        if not handled:
            logger.debug("codex handler: event not handled: %r", event)
