"""Action subscribers for request resolution (approve/deny/respond/input)."""

from __future__ import annotations

import logging

from ...surfaces.tool_actions import error, ok, tool_error_from_result
from ..action_models import ApproveEvent, DenyEvent, InputEvent, RespondEvent

logger = logging.getLogger(__name__)


class ApproveSubscriber:
    async def __call__(self, event: ApproveEvent) -> bool:
        args = event.args
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            event.result_future.set_result(error("task_id is required for approve"))
            return True

        for_session = bool(args.get("for_session"))
        try:
            result = await event.session.approve_task(task_id, "accept", for_session=for_session)
        except Exception as exc:
            event.result_future.set_result(error(f"approve_task failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(
            task_id=task_id,
            decision="acceptForSession" if for_session else "accept",
        ))
        return True


class DenySubscriber:
    async def __call__(self, event: DenyEvent) -> bool:
        args = event.args
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            event.result_future.set_result(error("task_id is required for deny"))
            return True

        try:
            result = await event.session.approve_task(task_id, "decline", for_session=False)
        except Exception as exc:
            event.result_future.set_result(error(f"deny failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(task_id=task_id, decision="decline"))
        return True


class RespondSubscriber:
    async def __call__(self, event: RespondEvent) -> bool:
        args = event.args
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            event.result_future.set_result(error("task_id is required for respond"))
            return True

        content = args.get("content")
        try:
            result = await event.session.respond_task(task_id, content)
        except Exception as exc:
            event.result_future.set_result(error(f"respond_task failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(task_id=task_id, decision="respond"))
        return True


class InputSubscriber:
    async def __call__(self, event: InputEvent) -> bool:
        args = event.args
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            event.result_future.set_result(error("task_id is required for answer"))
            return True

        answers = args.get("answers")
        responses = args.get("responses")

        if answers is not None and responses is not None:
            event.result_future.set_result(error("answers and responses are mutually exclusive"))
            return True

        if answers is not None:
            if not isinstance(answers, list) or not answers:
                event.result_future.set_result(
                    error("answers must be a non-empty list of non-empty string arrays")
                )
                return True
            if not all(
                isinstance(group, list) and group and all(isinstance(item, str) for item in group)
                for group in answers
            ):
                event.result_future.set_result(
                    error("answers must be a non-empty list of non-empty string arrays")
                )
                return True
            try:
                result = await event.session.input_task(task_id, answers=answers)
            except Exception as exc:
                event.result_future.set_result(error(f"input_task failed: {exc}"))
                return True
            if err_str := tool_error_from_result(result):
                event.result_future.set_result(err_str)
                return True
            event.result_future.set_result(ok(task_id=task_id))
            return True

        if not isinstance(responses, list) or not responses:
            event.result_future.set_result(error("responses must be a non-empty list of strings"))
            return True
        if not all(isinstance(r, str) for r in responses):
            event.result_future.set_result(error("responses must be a list of strings"))
            return True

        try:
            result = await event.session.input_task(task_id, responses=responses)
        except Exception as exc:
            event.result_future.set_result(error(f"input_task failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(task_id=task_id))
        return True
