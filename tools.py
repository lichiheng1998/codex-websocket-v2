"""Tool handlers for the codex-websocket-v2 plugin."""

from __future__ import annotations

import os
from typing import Any

from .codex_websocket_v2.core.session_registry import resolve_current_session
from .codex_websocket_v2.surfaces.tool_actions import (
    dispatch_tool_action,
    error as _error,
    ok as _ok,
    optional_str as _optional_str,
    tool_error_from_result as _tool_error_from_result,
    validate_plan as _validate_plan,
)


def _resolve_session_or_error():
    try:
        return resolve_current_session(), None
    except (ImportError, AttributeError) as exc:
        return None, _error(f"hermes runtime unavailable: {exc}")


def _dispatch_action_tool(map_name: str, args: dict) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")

    session, error = _resolve_session_or_error()
    if error is not None:
        return error
    return dispatch_tool_action(map_name, session, action, args)


def codex_task(args: dict, **kwargs: Any) -> str:
    cwd = args.get("cwd", "")
    prompt = args.get("prompt", "")
    approval_policy = _optional_str(args, "approval_policy")
    sandbox_policy = _optional_str(args, "sandbox_policy")
    model = _optional_str(args, "model")
    plan = _optional_str(args, "plan")
    base_instructions = args.get("base_instructions")

    if not cwd or not os.path.isabs(cwd):
        return _error("cwd must be an absolute path")
    if not os.path.isdir(cwd):
        return _error(f"cwd does not exist or is not a directory: {cwd}")
    if not prompt or not prompt.strip():
        return _error("prompt is required")
    try:
        plan = _validate_plan(plan)
    except ValueError as exc:
        return _error(str(exc))

    session, error = _resolve_session_or_error()
    if error is not None:
        return error

    result = session.start_task(
        cwd=cwd,
        prompt=prompt.strip(),
        model=model,
        plan=plan,
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
        base_instructions=base_instructions,
    )
    if not result["ok"]:
        return _error(f"codex session error: {result['error']}")

    task_id = result["task_id"]
    return _ok(
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
    )


def codex_tasks(args: dict, **kwargs: Any) -> str:
    return _dispatch_action_tool("task", args)


def codex_approval(args: dict, **kwargs: Any) -> str:
    return _dispatch_action_tool("approval", args)


def codex_action(args: dict, **kwargs: Any) -> str:
    return _dispatch_action_tool("action", args)


def codex_models(args: dict, **kwargs: Any) -> str:
    return _dispatch_action_tool("model", args)


def codex_session(args: dict, **kwargs: Any) -> str:
    return _dispatch_action_tool("session", args)


def codex_revive(args: dict, **kwargs: Any) -> str:
    thread_id = (args.get("thread_id") or "").strip()
    if not thread_id:
        return _error("thread_id is required")

    approval_policy = _optional_str(args, "approval_policy")
    sandbox_policy = _optional_str(args, "sandbox_policy")
    model = _optional_str(args, "model")
    plan = _optional_str(args, "plan")
    try:
        plan = _validate_plan(plan)
    except ValueError as exc:
        return _error(str(exc))

    session, error = _resolve_session_or_error()
    if error is not None:
        return error

    result = session.revive_task(
        thread_id,
        model=model,
        plan=plan,
        sandbox_policy=sandbox_policy,
        approval_policy=approval_policy,
    )
    if error := _tool_error_from_result(result):
        return error
    return _ok(
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
    )
