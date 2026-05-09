"""Tool handlers for the codex-websocket-v2 plugin."""

from __future__ import annotations

import concurrent.futures
import os
from typing import Any

from .codex_websocket_v2.core.session_registry import resolve_current_session
from .codex_websocket_v2.events.action_models import (
    RemoveEvent,
    ReviveEvent,
    StartTaskEvent,
    make_event,
)
from .codex_websocket_v2.surfaces.tool_actions import (
    error as _error,
    optional_str as _optional_str,
    validate_plan as _validate_plan,
)

_TOOL_TIMEOUT = 60.0


def _resolve_session_or_error():
    try:
        return resolve_current_session(), None
    except (ImportError, AttributeError) as exc:
        return None, _error(f"hermes runtime unavailable: {exc}")


def _ensure_started(session) -> str | None:
    started = session.ensure_started()
    if not started.get("ok"):
        return _error(started.get("error", "failed to start session"))
    return None


def _submit_and_wait(session, event) -> str:
    session.action_bus.submit(event)
    try:
        return event.result_future.result(timeout=_TOOL_TIMEOUT)
    except concurrent.futures.TimeoutError:
        return _error("action timed out after 60 seconds")
    except Exception as exc:
        return _error(str(exc))


def _dispatch_event(event_cls, args: dict) -> str:
    session, err = _resolve_session_or_error()
    if err is not None:
        return err
    if err := _ensure_started(session):
        return err
    event = event_cls(session=session, result_future=concurrent.futures.Future(), args=args)
    return _submit_and_wait(session, event)


def _dispatch_action_tool(map_name: str, args: dict) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")

    session, err = _resolve_session_or_error()
    if err is not None:
        return err

    if err := _ensure_started(session):
        return err

    try:
        event = make_event(map_name, action, session, args)
    except KeyError:
        return _error(f"unknown action {action!r}")

    return _submit_and_wait(session, event)


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

    return _dispatch_event(StartTaskEvent, {**args, "prompt": prompt.strip(), "plan": plan})


def codex_tasks(args: dict, **kwargs: Any) -> str:
    return _dispatch_action_tool("task", args)


def codex_remove(args: dict, **kwargs: Any) -> str:
    return _dispatch_event(RemoveEvent, args)


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

    plan = _optional_str(args, "plan")
    try:
        plan = _validate_plan(plan)
    except ValueError as exc:
        return _error(str(exc))

    return _dispatch_event(ReviveEvent, {**args, "plan": plan})
