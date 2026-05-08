"""Tool handlers for the codex-websocket-v2 plugin.

All tool calls go through the ActionEventBus: create a typed event, submit
to the serial queue, and wait for the result future.
"""

from __future__ import annotations

import os
from typing import Any

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


def _submit_and_wait(session, event) -> str:
    """Submit an action event and wait for the result."""
    session.action_bus.submit(event)
    try:
        result = event.result_future.result(timeout=_RESULT_TIMEOUT)
    except Exception as exc:
        return _error(f"action failed: {exc}")
    if not result.get("ok"):
        return _error(result.get("error", "unknown error"))
    return _ok(**{k: v for k, v in result.items() if k != "ok"})


def _submit_and_wait_custom(session, event, *, extra_ok: dict | None = None) -> str:
    """Submit, wait, and merge extra fields into the ok response."""
    session.action_bus.submit(event)
    try:
        result = event.result_future.result(timeout=_RESULT_TIMEOUT)
    except Exception as exc:
        return _error(f"action failed: {exc}")
    if not result.get("ok"):
        return _error(result.get("error", "unknown error"))
    data = {k: v for k, v in result.items() if k != "ok"}
    if extra_ok:
        data.update(extra_ok)
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

    event = session.action_factory.create("codex_task", args)
    session.action_bus.submit(event)
    try:
        result = event.result_future.result(timeout=_RESULT_TIMEOUT)
    except Exception as exc:
        return _error(f"action failed: {exc}")
    if not result.get("ok"):
        return _error(result.get("error", "unknown error"))

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


# ── codex_tasks ─────────────────────────────────────────────────────────────


def codex_tasks(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")
    session, error = _resolve_session_or_error()
    if error is not None:
        return error
    event = session.action_factory.create("codex_tasks", args)
    return _submit_and_wait(session, event)


# ── codex_remove ────────────────────────────────────────────────────────────


def codex_remove(args: dict, **kwargs: Any) -> str:
    session, error = _resolve_session_or_error()
    if error is not None:
        return error
    event = session.action_factory.create("codex_remove", args)
    return _submit_and_wait(session, event)


# ── codex_approval ──────────────────────────────────────────────────────────


def codex_approval(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")
    session, error = _resolve_session_or_error()
    if error is not None:
        return error
    event = session.action_factory.create("codex_approval", args)
    session.action_bus.submit(event)
    try:
        result = event.result_future.result(timeout=_RESULT_TIMEOUT)
    except Exception as exc:
        return _error(f"action failed: {exc}")
    if not result.get("ok"):
        return _error(result.get("error", "unknown error"))

    task_id = result.get("task_id", args.get("task_id", ""))
    decision = result.get("decision", action)
    for_session = bool(args.get("for_session"))
    if action == "approve" and for_session:
        decision = "acceptForSession"
    return _ok(task_id=task_id, decision=decision)


# ── codex_action ────────────────────────────────────────────────────────────


def codex_action(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")
    session, error = _resolve_session_or_error()
    if error is not None:
        return error
    event = session.action_factory.create("codex_action", args)
    return _submit_and_wait(session, event)


# ── codex_models ────────────────────────────────────────────────────────────


def codex_models(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")
    session, error = _resolve_session_or_error()
    if error is not None:
        return error
    event = session.action_factory.create("codex_models", args)
    session.action_bus.submit(event)
    try:
        result = event.result_future.result(timeout=_RESULT_TIMEOUT)
    except Exception as exc:
        return _error(f"action failed: {exc}")
    if not result.get("ok"):
        return _error(result.get("error", "unknown error"))

    if action == "list":
        return _ok(
            models=result.get("data") or [],
            current=session.get_default_model(),
        )
    return _ok(**{k: v for k, v in result.items() if k != "ok"})


# ── codex_session ───────────────────────────────────────────────────────────


def codex_session_tool(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")
    session, error = _resolve_session_or_error()
    if error is not None:
        return error
    event = session.action_factory.create("codex_session", args)
    session.action_bus.submit(event)
    try:
        result = event.result_future.result(timeout=_RESULT_TIMEOUT)
    except Exception as exc:
        return _error(f"action failed: {exc}")
    if not result.get("ok"):
        return _error(result.get("error", "unknown error"))

    data = {k: v for k, v in result.items() if k != "ok"}
    if action == "status" and not args.get("task_id"):
        data["session_key"] = session.session_key
    return _ok(**data)


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

    event = session.action_factory.create("codex_revive", args)
    session.action_bus.submit(event)
    try:
        result = event.result_future.result(timeout=_RESULT_TIMEOUT)
    except Exception as exc:
        return _error(f"action failed: {exc}")
    if not result.get("ok"):
        return _error(result.get("error", "unknown error"))

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
