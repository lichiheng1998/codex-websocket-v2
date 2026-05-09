"""Action subscribers for task lifecycle operations.

Handles: StartTaskEvent, ReplyEvent, SteerEvent, StopEvent, ReviveEvent,
         RemoveEvent.

Each subscriber is an async callable that:
  1. Extracts args from ``event.args``
  2. Calls the corresponding async session method
  3. Sets the formatted JSON result on ``event.result_future``
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...surfaces.tool_actions import (
    error,
    ok,
    optional_str,
    tool_error_from_result,
    validate_plan,
)
from ..action_models import (
    RemoveEvent,
    ReplyEvent,
    ReviveEvent,
    StartTaskEvent,
    SteerEvent,
    StopEvent,
)
from ...core.state import err

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class StartTaskSubscriber:
    async def __call__(self, event: StartTaskEvent) -> bool:
        args = event.args
        session = event.session
        cwd = args.get("cwd", "")
        prompt = args.get("prompt", "")
        approval_policy = optional_str(args, "approval_policy")
        sandbox_policy = optional_str(args, "sandbox_policy")
        model = optional_str(args, "model")
        plan = optional_str(args, "plan")
        base_instructions = args.get("base_instructions")

        try:
            plan = validate_plan(plan)
        except ValueError as exc:
            event.result_future.set_result(error(str(exc)))
            return True

        try:
            result = await session.start_task(
                cwd=cwd,
                prompt=prompt.strip() if prompt else "",
                model=model,
                plan=plan,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
                base_instructions=base_instructions,
            )
        except Exception as exc:
            event.result_future.set_result(error(f"start_task failed: {exc}"))
            return True

        if not result.get("ok"):
            event.result_future.set_result(error(f"codex session error: {result.get('error', 'unknown')}"))
            return True

        task_id = result["task_id"]
        event.result_future.set_result(ok(
            status="started",
            task_id=task_id,
            cwd=cwd,
            model=result.get("model", session.get_default_model()),
            plan=result.get("plan"),
            sandbox_policy=result.get("sandbox_policy"),
            approval_policy=result.get("approval_policy"),
            message=(
                f"Codex task {task_id} started in the background. "
                f"Progress, approval requests, and the final result will be "
                f"pushed to the current channel as separate messages. "
                f"You do NOT need to poll — return control to the user."
            ),
        ))
        return True


class ReplySubscriber:
    async def __call__(self, event: ReplyEvent) -> bool:
        args = event.args
        task_id = (args.get("task_id") or "").strip()
        message = (args.get("message") or "").strip()

        if not task_id:
            event.result_future.set_result(error("task_id is required for reply"))
            return True
        if not message:
            event.result_future.set_result(error("message is required for reply"))
            return True

        try:
            result = await event.session.send_reply(task_id, message)
        except Exception as exc:
            event.result_future.set_result(error(f"send_reply failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(task_id=task_id))
        return True


class SteerSubscriber:
    async def __call__(self, event: SteerEvent) -> bool:
        args = event.args
        task_id = (args.get("task_id") or "").strip()
        message = (args.get("message") or "").strip()

        if not task_id:
            event.result_future.set_result(error("task_id is required for steer"))
            return True
        if not message:
            event.result_future.set_result(error("message is required for steer"))
            return True

        try:
            result = await event.session.steer_task(task_id, message)
        except Exception as exc:
            event.result_future.set_result(error(f"steer_task failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(task_id=task_id, turn_id=result.get("turn_id")))
        return True


class StopSubscriber:
    async def __call__(self, event: StopEvent) -> bool:
        args = event.args
        task_id = (args.get("task_id") or "").strip()

        if not task_id:
            event.result_future.set_result(error("task_id is required for stop"))
            return True

        try:
            result = await event.session.stop_task(task_id)
        except Exception as exc:
            event.result_future.set_result(error(f"stop_task failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(task_id=task_id, turn_id=result.get("turn_id")))
        return True


class ReviveSubscriber:
    async def __call__(self, event: ReviveEvent) -> bool:
        args = event.args
        session = event.session
        thread_id = (args.get("thread_id") or "").strip()

        if not thread_id:
            event.result_future.set_result(error("thread_id is required"))
            return True

        approval_policy = optional_str(args, "approval_policy")
        sandbox_policy = optional_str(args, "sandbox_policy")
        model = optional_str(args, "model")
        plan = optional_str(args, "plan")

        try:
            plan = validate_plan(plan)
        except ValueError as exc:
            event.result_future.set_result(error(str(exc)))
            return True

        try:
            result = await session.revive_task(
                thread_id,
                model=model,
                plan=plan,
                sandbox_policy=sandbox_policy,
                approval_policy=approval_policy,
            )
        except Exception as exc:
            event.result_future.set_result(error(f"revive_task failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(
            task_id=result["task_id"],
            thread_id=result["thread_id"],
            model=result.get("model", session.get_default_model()),
            plan=result.get("plan"),
            sandbox_policy=result.get("sandbox_policy"),
            approval_policy=result.get("approval_policy"),
            message=(
                f"Thread revived as task {result['task_id']}. "
                f"Use `/codex reply {result['task_id']} <message>` to continue."
            ),
        ))
        return True


class RemoveSubscriber:
    async def __call__(self, event: RemoveEvent) -> bool:
        args = event.args
        session = event.session

        if args.get("all") is True:
            result = session.remove_all_tasks()
            if err_str := tool_error_from_result(result):
                event.result_future.set_result(err_str)
                return True
            event.result_future.set_result(ok(
                scope="all",
                removed=result.get("removed", 0),
                tasks=result.get("tasks", []),
            ))
            return True

        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            event.result_future.set_result(
                error("task_id is required for remove unless all=true")
            )
            return True

        result = session.remove_task(task_id)
        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(
            scope="task",
            task_id=result.get("task_id", task_id),
            thread_id=result.get("thread_id"),
        ))
        return True
