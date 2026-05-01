"""Fallback subscribers for unhandled events."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ..models import UnknownFrameEvent, UnknownNotificationEvent, UnknownRequestEvent

if TYPE_CHECKING:
    from ...core.session import CodexSession

logger = logging.getLogger(__name__)


class UnhandledRequestSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    async def __call__(self, event: UnknownRequestEvent) -> bool:
        logger.debug("codex handler: unhandled server request %s", event.method)
        await self.session.bridge.ws_send(json.dumps({
            "jsonrpc": "2.0", "id": event.rpc_id,
            "error": {"code": -32601, "message": f"unhandled: {event.method}"},
        }))
        return True


class UnhandledNotificationSubscriber:
    async def __call__(self, event: UnknownNotificationEvent) -> bool:
        if event.method != "item/agentMessage/delta":
            logger.debug("codex handler: notification %s ignored", event.method)
        return True


class UnknownFrameSubscriber:
    async def __call__(self, event: UnknownFrameEvent) -> bool:
        logger.debug("codex bridge: unparseable frame dropped")
        return True
