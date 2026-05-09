"""Action subscribers for session/model settings operations."""

from __future__ import annotations

import logging

from ...surfaces.tool_actions import (
    error,
    ok,
    optional_str,
    serialize_scope_result,
    tool_error_from_result,
    validate_plan,
)
from ..action_models import (
    GetApprovalPolicyEvent,
    GetModelEvent,
    GetPlanEvent,
    GetSandboxEvent,
    GetStatusEvent,
    GetVerboseEvent,
    ListModelsEvent,
    SetApprovalPolicyEvent,
    SetModelEvent,
    SetPlanEvent,
    SetSandboxEvent,
    SetVerboseEvent,
)

logger = logging.getLogger(__name__)


class ListModelsSubscriber:
    async def __call__(self, event: ListModelsEvent) -> bool:
        try:
            result = await event.session.list_models()
        except Exception as exc:
            event.result_future.set_result(error(f"list_models failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(
            ok(models=result.get("data") or [], current=event.session.get_default_model())
        )
        return True


class GetModelSubscriber:
    async def __call__(self, event: GetModelEvent) -> bool:
        task_id = optional_str(event.args, "task_id")
        try:
            result = await event.session.get_model(task_id)
        except Exception as exc:
            event.result_future.set_result(error(f"get_model failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(**serialize_scope_result(result, "model")))
        return True


class SetModelSubscriber:
    async def __call__(self, event: SetModelEvent) -> bool:
        args = event.args
        task_id = optional_str(args, "task_id")
        model_id = (args.get("model_id") or "").strip()
        if not model_id:
            event.result_future.set_result(error("model_id is required"))
            return True

        try:
            result = await event.session.set_model(model_id, task_id)
        except Exception as exc:
            event.result_future.set_result(error(f"set_model failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(**serialize_scope_result(result, "model")))
        return True


class GetStatusSubscriber:
    async def __call__(self, event: GetStatusEvent) -> bool:
        task_id = optional_str(event.args, "task_id")
        try:
            result = await event.session.get_status(task_id)
        except Exception as exc:
            event.result_future.set_result(error(f"get_status failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        if task_id:
            event.result_future.set_result(ok(**result))
        else:
            session = event.session
            event.result_future.set_result(ok(
                session_key=session.session_key,
                connected=result["connected"],
                active_tasks=result["active_tasks"],
                total_threads=result["total_threads"],
                model=result["model"],
                mode=result["mode"],
                plan=result["plan"],
                verbose=result["verbose"],
                sandbox_policy=result["sandbox_policy"],
                approval_policy=result["approval_policy"],
            ))
        return True


class GetPlanSubscriber:
    async def __call__(self, event: GetPlanEvent) -> bool:
        task_id = optional_str(event.args, "task_id")
        try:
            result = await event.session.get_plan(task_id)
        except Exception as exc:
            event.result_future.set_result(error(f"get_plan failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(**result))
        return True


class SetPlanSubscriber:
    async def __call__(self, event: SetPlanEvent) -> bool:
        args = event.args
        plan = optional_str(args, "plan")
        if plan is None:
            event.result_future.set_result(error("plan is required for plan_set (on/off)"))
            return True
        try:
            plan = validate_plan(plan)
        except ValueError as exc:
            event.result_future.set_result(error(str(exc)))
            return True

        try:
            result = await event.session.set_plan(plan, optional_str(args, "task_id"))
        except Exception as exc:
            event.result_future.set_result(error(f"set_plan failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(**result))
        return True


class GetVerboseSubscriber:
    async def __call__(self, event: GetVerboseEvent) -> bool:
        event.result_future.set_result(ok(verbose=event.session.get_verbose()))
        return True


class SetVerboseSubscriber:
    async def __call__(self, event: SetVerboseEvent) -> bool:
        level = (event.args.get("level") or "").strip()
        if not level:
            event.result_future.set_result(error("level is required"))
            return True
        result = event.session.set_verbose(level)
        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True
        event.result_future.set_result(ok(verbose=result["verbose"]))
        return True


class GetSandboxSubscriber:
    async def __call__(self, event: GetSandboxEvent) -> bool:
        task_id = optional_str(event.args, "task_id")
        try:
            result = await event.session.get_sandbox_policy(task_id)
        except Exception as exc:
            event.result_future.set_result(error(f"get_sandbox_policy failed: {exc}"))
            return True

        if isinstance(result, dict):
            if err_str := tool_error_from_result(result):
                event.result_future.set_result(err_str)
                return True
            event.result_future.set_result(ok(**result))
        else:
            event.result_future.set_result(ok(scope="default", sandbox_policy=result))
        return True


class SetSandboxSubscriber:
    async def __call__(self, event: SetSandboxEvent) -> bool:
        args = event.args
        policy = (args.get("sandbox_policy") or "").strip()
        if not policy:
            event.result_future.set_result(error("sandbox_policy is required"))
            return True

        try:
            result = await event.session.set_sandbox_policy(policy, optional_str(args, "task_id"))
        except Exception as exc:
            event.result_future.set_result(error(f"set_sandbox_policy failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(**result))
        return True


class GetApprovalPolicySubscriber:
    async def __call__(self, event: GetApprovalPolicyEvent) -> bool:
        task_id = optional_str(event.args, "task_id")
        try:
            result = await event.session.get_approval_policy(task_id)
        except Exception as exc:
            event.result_future.set_result(error(f"get_approval_policy failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(**result))
        return True


class SetApprovalPolicySubscriber:
    async def __call__(self, event: SetApprovalPolicyEvent) -> bool:
        args = event.args
        policy = (args.get("approval_policy") or "").strip()
        if not policy:
            event.result_future.set_result(error("approval_policy is required"))
            return True

        try:
            result = await event.session.set_approval_policy(
                policy, optional_str(args, "task_id")
            )
        except Exception as exc:
            event.result_future.set_result(error(f"set_approval_policy failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(**result))
        return True
