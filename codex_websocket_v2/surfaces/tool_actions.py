"""Internal action handlers shared by public Codex tool entrypoints."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def error(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False)


def optional_str(args: dict, name: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    return str(value).strip()


def require_str(
    args: dict,
    name: str,
    label: str | None = None,
    *,
    message: str | None = None,
) -> tuple[str, str | None]:
    value = (args.get(name) or "").strip()
    if not value:
        return "", error(message or f"{label or name} is required")
    return value, None


def tool_error_from_result(result: dict, default: str = "unknown error") -> str | None:
    if result.get("ok"):
        return None
    return error(result.get("error", default))


def validate_plan(plan: str | None) -> str | None:
    if plan is None:
        return None
    normalized = plan.strip().lower()
    if normalized not in ("on", "off"):
        raise ValueError("plan must be 'on' or 'off'")
    return normalized


def serialize_scope_result(result: dict, *fields: str) -> dict:
    data = {
        "scope": result.get("scope", "default"),
        "task_id": result.get("task_id"),
    }
    for field in fields:
        data[field] = result.get(field)
    return data


def _serialize_task(session, task) -> dict:
    pending = None
    if task.request_rpc_id is not None:
        pending = {"type": task.request_type}
    return {
        "task_id": task.task_id,
        "thread_id": task.thread_id,
        "cwd": task.cwd,
        "model": getattr(task, "model", session.get_default_model()),
        "plan": "on" if getattr(task, "plan", session.get_mode() == "plan") else "off",
        "sandbox_policy": getattr(task, "sandbox_policy", session.sandbox_policy),
        "approval_policy": getattr(task, "approval_policy", session.approval_policy),
        "pending": pending,
    }


def _tasks_list(session, args: dict) -> str:
    if args.get("show_threads"):
        try:
            result = session.list_threads()
        except Exception as exc:
            return error(f"list_threads failed: {exc}")
        if result_error := tool_error_from_result(result):
            return result_error
        threads = result.get("data") or []
        return ok(threads=threads, total=len(threads))

    return ok(tasks=[
        _serialize_task(session, task)
        for task in session.tasks.values()
    ])


def _tasks_reply(session, args: dict) -> str:
    task_id, result_error = require_str(
        args, "task_id", message="task_id is required for reply"
    )
    if result_error is not None:
        return result_error
    message, result_error = require_str(
        args, "message", message="message is required for reply"
    )
    if result_error is not None:
        return result_error
    try:
        result = session.send_reply(task_id, message)
    except Exception as exc:
        return error(f"send_reply failed: {exc}")
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(task_id=task_id)


def _tasks_answer(session, args: dict) -> str:
    task_id, result_error = require_str(
        args, "task_id", message="task_id is required for answer"
    )
    if result_error is not None:
        return result_error
    answers = args.get("answers")
    responses = args.get("responses")
    if answers is not None and responses is not None:
        return error("answers and responses are mutually exclusive")
    if answers is not None:
        if not isinstance(answers, list) or not answers:
            return error("answers must be a non-empty list of non-empty string arrays")
        if not all(
            isinstance(group, list)
            and group
            and all(isinstance(item, str) for item in group)
            for group in answers
        ):
            return error("answers must be a non-empty list of non-empty string arrays")
        try:
            result = session.input_task(task_id, answers=answers)
        except Exception as exc:
            return error(f"input_task failed: {exc}")
        if result_error := tool_error_from_result(result):
            return result_error
        return ok(task_id=task_id)

    if not isinstance(responses, list) or not responses:
        return error("responses must be a non-empty list of strings")
    if not all(isinstance(r, str) for r in responses):
        return error("responses must be a list of strings")
    try:
        result = session.input_task(task_id, responses=responses)
    except Exception as exc:
        return error(f"input_task failed: {exc}")
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(task_id=task_id)


def _tasks_approve(session, args: dict) -> str:
    task_id, result_error = require_str(
        args, "task_id", message="task_id is required for approve"
    )
    if result_error is not None:
        return result_error
    for_session = bool(args.get("for_session"))
    result = session.approve_task(task_id, "accept", for_session=for_session)
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(
        task_id=task_id,
        decision="acceptForSession" if for_session else "accept",
    )


def _tasks_deny(session, args: dict) -> str:
    task_id, result_error = require_str(
        args, "task_id", message="task_id is required for deny"
    )
    if result_error is not None:
        return result_error
    result = session.approve_task(task_id, "decline", for_session=False)
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(task_id=task_id, decision="decline")


def _tasks_archive(session, args: dict) -> str:
    target, result_error = require_str(
        args, "target", message="target is required for archive"
    )
    if result_error is not None:
        return result_error

    if target == "allthreads":
        result = session.archive_all_threads()
        return json.dumps({
            "ok": result.get("ok", False),
            "scope": "allthreads",
            "removed": result.get("removed", 0),
            "errors": result.get("errors", []),
        }, ensure_ascii=False)
    if target == "all":
        result = session.remove_all_tasks()
        return json.dumps({
            "ok": result.get("ok", False),
            "scope": "all",
            "removed": result.get("removed", 0),
            "errors": result.get("errors", []),
        }, ensure_ascii=False)

    result = session.remove_task(target)
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(scope="task", task_id=target)


ActionHandler = Callable[[Any, dict], str]


def _models_list(session, args: dict) -> str:
    result = session.list_models()
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(models=result.get("data") or [], current=session.get_default_model())


def _models_get(session, args: dict) -> str:
    result = session.get_model(optional_str(args, "task_id"))
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(**serialize_scope_result(result, "model"))


def _models_set(session, args: dict) -> str:
    task_id = optional_str(args, "task_id")
    model_id, result_error = require_str(args, "model_id", "model_id")
    if result_error is not None:
        return result_error
    started = session.ensure_started()
    if result_error := tool_error_from_result(started, "failed to start session"):
        return result_error
    result = session.set_model(model_id, task_id)
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(**serialize_scope_result(result, "model"))


CODEX_MODEL_ACTIONS: dict[str, ActionHandler] = {
    "list": _models_list,
    "get": _models_get,
    "set": _models_set,
}


def dispatch_model_action(session, action: str, args: dict) -> str:
    handler = CODEX_MODEL_ACTIONS.get(action)
    if handler is None:
        return error(f"unknown action {action!r}")
    return handler(session, args)


CODEX_TASK_ACTIONS: dict[str, ActionHandler] = {
    "list": _tasks_list,
    "reply": _tasks_reply,
    "answer": _tasks_answer,
    "approve": _tasks_approve,
    "deny": _tasks_deny,
    "archive": _tasks_archive,
}


def dispatch_task_action(session, action: str, args: dict) -> str:
    handler = CODEX_TASK_ACTIONS.get(action)
    if handler is None:
        return error(f"unknown action {action!r}")
    return handler(session, args)


def _session_status(session, args: dict) -> str:
    task_id = optional_str(args, "task_id")
    result = session.get_status(task_id)
    if result_error := tool_error_from_result(result):
        return result_error
    if task_id:
        return ok(**result)
    return ok(
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
    )


def _session_plan_get(session, args: dict) -> str:
    result = session.get_plan(optional_str(args, "task_id"))
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(**result)


def _session_plan_set(session, args: dict) -> str:
    plan = optional_str(args, "plan")
    if plan is None:
        return error("plan is required for plan_set (on/off)")
    try:
        plan = validate_plan(plan)
    except ValueError as exc:
        return error(str(exc))
    result = session.set_plan(plan, optional_str(args, "task_id"))
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(**result)


def _session_verbose_get(session, args: dict) -> str:
    return ok(verbose=session.get_verbose())


def _session_verbose_set(session, args: dict) -> str:
    level, result_error = require_str(args, "level", "level")
    if result_error is not None:
        return result_error
    result = session.set_verbose(level)
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(verbose=result["verbose"])


def _session_sandbox_get(session, args: dict) -> str:
    result = session.get_sandbox_policy(optional_str(args, "task_id"))
    if isinstance(result, dict):
        if result_error := tool_error_from_result(result):
            return result_error
        return ok(**result)
    return ok(scope="default", sandbox_policy=result)


def _session_sandbox_set(session, args: dict) -> str:
    policy, result_error = require_str(args, "sandbox_policy", "sandbox_policy")
    if result_error is not None:
        return result_error
    result = session.set_sandbox_policy(policy, optional_str(args, "task_id"))
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(**result)


def _session_approval_get(session, args: dict) -> str:
    result = session.get_approval_policy(optional_str(args, "task_id"))
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(**result)


def _session_approval_set(session, args: dict) -> str:
    policy, result_error = require_str(args, "approval_policy", "approval_policy")
    if result_error is not None:
        return result_error
    result = session.set_approval_policy(policy, optional_str(args, "task_id"))
    if result_error := tool_error_from_result(result):
        return result_error
    return ok(**result)


CODEX_SESSION_ACTIONS: dict[str, ActionHandler] = {
    "status": _session_status,
    "plan_get": _session_plan_get,
    "plan_set": _session_plan_set,
    "verbose_get": _session_verbose_get,
    "verbose_set": _session_verbose_set,
    "sandbox_get": _session_sandbox_get,
    "sandbox_set": _session_sandbox_set,
    "approval_get": _session_approval_get,
    "approval_set": _session_approval_set,
}


def dispatch_session_action(session, action: str, args: dict) -> str:
    handler = CODEX_SESSION_ACTIONS.get(action)
    if handler is None:
        return error(f"unknown action {action!r}")
    return handler(session, args)
