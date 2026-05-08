"""Action subscriber for approval and input resolution.

Handles: Approve, Deny, Answer, Respond.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..action_models import (
    AnswerActionEvent,
    ApproveActionEvent,
    BaseActionEvent,
    DenyActionEvent,
    RespondActionEvent,
)

if TYPE_CHECKING:
    from ...core.session import CodexSession

logger = logging.getLogger(__name__)


class ApprovalActionSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    async def __call__(self, event: BaseActionEvent) -> bool:
        s = self.session
        result = None

        try:
            if isinstance(event, ApproveActionEvent):
                result = await s.approve_task(
                    event.task_id, "accept", for_session=event.for_session,
                )
            elif isinstance(event, DenyActionEvent):
                result = await s.approve_task(event.task_id, "decline")
            elif isinstance(event, AnswerActionEvent):
                result = await s.input_task(
                    event.task_id,
                    responses=event.responses,
                    answers=event.answers,
                )
            elif isinstance(event, RespondActionEvent):
                result = await s.respond_task(event.task_id, event.content)
            else:
                return False
        except Exception as exc:
            logger.exception("approval action failed: %s", exc)
            result = {"ok": False, "error": str(exc)}

        if not event.result_future.done():
            event.result_future.set_result(result)
        return True
