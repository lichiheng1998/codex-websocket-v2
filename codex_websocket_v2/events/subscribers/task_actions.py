"""Action subscriber for task lifecycle operations.

Handles: StartTask, Reply, Steer, Stop, Revive, Remove.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..action_models import (
    BaseActionEvent,
    RemoveActionEvent,
    ReplyActionEvent,
    ReviveActionEvent,
    StartTaskActionEvent,
    SteerActionEvent,
    StopActionEvent,
)

if TYPE_CHECKING:
    from ...core.session import CodexSession

logger = logging.getLogger(__name__)


def _resolve(session: "CodexSession", event: BaseActionEvent):
    """Return (session, result_future) for convenience."""
    return session, event.result_future


class TaskActionSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    async def __call__(self, event: BaseActionEvent) -> bool:
        s = self.session
        result = None

        try:
            if isinstance(event, StartTaskActionEvent):
                result = await s.start_task(
                    cwd=event.cwd, prompt=event.prompt,
                    model=event.model, plan=event.plan,
                    sandbox_policy=event.sandbox_policy,
                    approval_policy=event.approval_policy,
                    base_instructions=event.base_instructions,
                )
            elif isinstance(event, ReplyActionEvent):
                result = await s.send_reply(event.task_id, event.message)
            elif isinstance(event, SteerActionEvent):
                result = await s.steer_task(event.task_id, event.message)
            elif isinstance(event, StopActionEvent):
                result = await s.stop_task(event.task_id)
            elif isinstance(event, ReviveActionEvent):
                result = await s.revive_task(
                    event.thread_id, model=event.model, plan=event.plan,
                    sandbox_policy=event.sandbox_policy,
                    approval_policy=event.approval_policy,
                )
            elif isinstance(event, RemoveActionEvent):
                if event.all:
                    result = s.remove_all_tasks()
                else:
                    result = s.remove_task(event.task_id)
            else:
                return False
        except Exception as exc:
            logger.exception("task action failed: %s", exc)
            result = {"ok": False, "error": str(exc)}

        if not event.result_future.done():
            event.result_future.set_result(result)
        return True
