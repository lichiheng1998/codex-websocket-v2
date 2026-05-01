"""Subscriber for approval request events."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..approval_handler import ApprovalRequestHandler
from ..events import ApprovalRequestedEvent

if TYPE_CHECKING:
    from ..session import CodexSession


class ApprovalRequestSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.handler = ApprovalRequestHandler(session)

    async def __call__(self, event: ApprovalRequestedEvent) -> bool:
        await self.handler.handle(event.method, event.params, event.rpc_id)
        return True
