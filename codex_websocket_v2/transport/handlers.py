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


class MessageHandler:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session
        self.event_factory = EventFactory(session)

    async def dispatch(self, raw: dict) -> None:
        event = self.event_factory.from_raw(raw)
        handled = await self.session.event_bus.publish(event)
        if not handled:
            logger.debug("codex handler: event not handled: %r", event)
