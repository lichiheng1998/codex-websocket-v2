"""Tool handlers for the codex-websocket-v2 plugin.

All tool calls go through the ActionEventBus: create a typed event, submit
to the serial queue, and wait for the result future.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from .codex_websocket_v2.core.session_registry import resolve_current_session
from .codex_websocket_v2.surfaces.tool_actions import (
    error as _error,
    ok as _ok,
    optional_str as _optional_str,
    validate_plan as _validate_plan,
)

_RESULT_TIMEOUT = 60


def _resolve_session_or_error():
    try:
        return resolve_current_session(), None
    except (ImportError, AttributeError) as exc:
        return None, _error(f"hermes runtime unavailable: {exc}")


def _submit(
    session,
    event,
    on_ok: Callable[[dict], str] | None = None,
) -> str:
    """Submit an action event, wait for result, and return JSON string.

    *on_ok* receives the raw result dict (with ``ok`` removed) and must
    return a JSON string.  When *on_ok* is ``None`` the default ``ok()``
    response is built from the result dict.
    """
    session.action_bus.submit(event)
    try:
        result = event.result_future.result(timeout=_RESULT_TIMEOUT)
    except Exception as exc:
        return _error(f"action failed: {exc}")
    if not result.get("ok"):
        return _error(result.get("error", "unknown error"))
    data = {k: v for k, v in result.items() if k != "ok"}
    if on_ok is not None:
        return on_ok(data)
    return _ok(**data)


# ── codex_task ──────────────────────────────────────────────────────────────


def codex_task(args: dict, **kwargs: Any) -> str:
    cwd = args.get("cwd", "")
    prompt = args.get("prompt", "")

    if not cwd or not os.path.isabs(cwd):
        return _error("cwd must be an absolute path")
    if not os.path.isdir(cwd):
        return _error(f"cwd does not exist or is not a directory: {cwd}")
    if not prompt or not prompt.strip():
        return _error("prompt is required")

    plan = _optional_str(args, "plan")
    try:
        plan = _validate_plan(plan)
    except ValueError as exc:
        return _error(str(exc))
    args["plan"] = plan

    session, error = _resolve_session_or_error()
    if error is not None:
        return error

    def _wrap(data: dict) -> str:
        task_id = data["task_id"]
        return _ok(
            status="started",
            task_id=task_id,
            cwd=cwd,
            model=data.get("model", session.get_default_model()),
            plan=data.get("plan"),
            sandbox_policy=data.get("sandbox_policy"),
            approval_policy=data.get("approval_policy"),
            message=(
                f"Codex task {task_id} started in the background. "
                f"Progress, approval requests, and the final result will be "
                f"pushed to the current channel as separate messages. "
                f"You do NOT need to poll — return control to the user."
            ),
        )

    event = session.action_factory.create("codex_task", args)
    return _submit(session, event, on_ok=_wrap)


# ── codex_tasks ─────────────────────────────────────────────────────────────


def codex_tasks(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")
    session, error = _resolve_session_or_error()
    if error is not None:
        return error
    event = session.action_factory.create("codex_tasks", args)
    return _submit(session, event)


# ── codex_remove ────────────────────────────────────────────────────────────


def codex_remove(args: dict, **kwargs: Any) -> str:
    session, error = _resolve_session_or_error()
    if error is not None:
        return error
    event = session.action_factory.create("codex_remove", args)
    return _submit(session, event)


# ── codex_approval ──────────────────────────────────────────────────────────


def codex_approval(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")
    session, error = _resolve_session_or_error()
    if error is not None:
        return error

    for_session = bool(args.get("for_session"))

    def _wrap(data: dict) -> str:
        task_id = data.get("task_id", args.get("task_id", ""))
        decision = data.get("decision", action)
        if action == "approve" and for_session:
            decision = "acceptForSession"
        return _ok(task_id=task_id, decision=decision)

    event = session.action_factory.create("codex_approval", args)
    return _submit(session, event, on_ok=_wrap)


# ── codex_action ────────────────────────────────────────────────────────────


def codex_action(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")
    session, error = _resolve_session_or_error()
    if error is not None:
        return error
    event = session.action_factory.create("codex_action", args)
    return _submit(session, event)


# ── codex_models ────────────────────────────────────────────────────────────


def codex_models(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")
    session, error = _resolve_session_or_error()
    if error is not None:
        return error

    def _wrap(data: dict) -> str:
        if action == "list":
            return _ok(
                models=data.get("data") or [],
                current=session.get_default_model(),
            )
        return _ok(**data)

    event = session.action_factory.create("codex_models", args)
    return _submit(session, event, on_ok=_wrap)


# ── codex_session ───────────────────────────────────────────────────────────


def codex_session_tool(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")
    session, error = _resolve_session_or_error()
    if error is not None:
        return error

    def _wrap(data: dict) -> str:
        if action == "status" and not args.get("task_id"):
            data["session_key"] = session.session_key
        return _ok(**data)

    event = session.action_factory.create("codex_session", args)
    return _submit(session, event, on_ok=_wrap)


# ── codex_revive ────────────────────────────────────────────────────────────


def codex_revive(args: dict, **kwargs: Any) -> str:
    thread_id = (args.get("thread_id") or "").strip()
    if not thread_id:
        return _error("thread_id is required")

    plan = _optional_str(args, "plan")
    try:
        plan = _validate_plan(plan)
    except ValueError as exc:
        return _error(str(exc))
    args["plan"] = plan

    session, error = _resolve_session_or_error()
    if error is not None:
        return error

    def _wrap(data: dict) -> str:
        return _ok(
            task_id=data["task_id"],
            thread_id=data["thread_id"],
            model=data.get("model", session.get_default_model()),
            plan=data.get("plan"),
            sandbox_policy=data.get("sandbox_policy"),
            approval_policy=data.get("approval_policy"),
            message=(
                f"Thread revived as task {data['task_id']}. "
                f"Use `/codex reply {data['task_id']} <message>` to continue."
            ),
        )

    event = session.action_factory.create("codex_revive", args)
    return _submit(session, event, on_ok=_wrap)
