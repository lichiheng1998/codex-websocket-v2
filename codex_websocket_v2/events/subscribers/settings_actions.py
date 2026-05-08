"""Action subscriber for session/task settings.

Handles: Model, Plan, Verbose, Sandbox, Approval get/set.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..action_models import (
    BaseActionEvent,
    GetApprovalActionEvent,
    GetModelActionEvent,
    GetPlanActionEvent,
    GetSandboxActionEvent,
    GetVerboseActionEvent,
    SetApprovalActionEvent,
    SetModelActionEvent,
    SetPlanActionEvent,
    SetSandboxActionEvent,
    SetVerboseActionEvent,
)

if TYPE_CHECKING:
    from ...core.session import CodexSession

logger = logging.getLogger(__name__)


class SettingsActionSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    async def __call__(self, event: BaseActionEvent) -> bool:
        s = self.session
        result = None

        try:
            if isinstance(event, SetModelActionEvent):
                result = await s.set_model(event.model_id, event.task_id)
            elif isinstance(event, GetModelActionEvent):
                result = await s.get_model(event.task_id)
            elif isinstance(event, SetPlanActionEvent):
                result = await s.set_plan(event.plan, event.task_id)
            elif isinstance(event, GetPlanActionEvent):
                result = await s.get_plan(event.task_id)
            elif isinstance(event, SetVerboseActionEvent):
                result = s.set_verbose(event.level)
            elif isinstance(event, GetVerboseActionEvent):
                result = {"ok": True, "verbose": s.get_verbose()}
            elif isinstance(event, SetSandboxActionEvent):
                result = await s.set_sandbox_policy(event.policy, event.task_id)
            elif isinstance(event, GetSandboxActionEvent):
                result = await s.get_sandbox_policy(event.task_id)
            elif isinstance(event, SetApprovalActionEvent):
                result = await s.set_approval_policy(event.policy, event.task_id)
            elif isinstance(event, GetApprovalActionEvent):
                result = await s.get_approval_policy(event.task_id)
            else:
                return False
        except Exception as exc:
            logger.exception("settings action failed: %s", exc)
            result = {"ok": False, "error": str(exc)}

        if not event.result_future.done():
            event.result_future.set_result(result)
        return True
