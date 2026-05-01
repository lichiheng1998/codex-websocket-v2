"""Tool handlers for the codex-websocket-v2 plugin."""

from __future__ import annotations

import json
import os
from typing import Any

from .codex_websocket_v2.core.policies import DEFAULT_APPROVAL_POLICY
from .codex_websocket_v2.core.session_registry import resolve_current_session


def _error(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def codex_task(args: dict, **kwargs: Any) -> str:
    cwd = args.get("cwd", "")
    prompt = args.get("prompt", "")
    approval_policy = args.get("approval_policy", DEFAULT_APPROVAL_POLICY)
    base_instructions = args.get("base_instructions")

    if not cwd or not os.path.isabs(cwd):
        return _error("cwd must be an absolute path")
    if not os.path.isdir(cwd):
        return _error(f"cwd does not exist or is not a directory: {cwd}")
    if not prompt or not prompt.strip():
        return _error("prompt is required")

    try:
        session = resolve_current_session()
    except (ImportError, AttributeError) as exc:
        return _error(f"hermes runtime unavailable: {exc}")

    result = session.start_task(
        cwd=cwd,
        prompt=prompt.strip(),
        approval_policy=approval_policy,
        base_instructions=base_instructions,
    )
    if not result["ok"]:
        return _error(f"codex session error: {result['error']}")

    task_id = result["task_id"]
    return json.dumps({
        "ok": True,
        "status": "started",
        "task_id": task_id,
        "cwd": cwd,
        "model": result.get("model", session.get_default_model()),
        "sandbox_policy": session.get_sandbox_policy(),
        "message": (
            f"Codex task {task_id} started in the background. "
            f"Progress, approval requests, and the final result will be "
            f"pushed to the current channel as separate messages. "
            f"You do NOT need to poll — return control to the user."
        ),
    }, ensure_ascii=False)


def codex_tasks(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")

    try:
        session = resolve_current_session()
    except (ImportError, AttributeError) as exc:
        return _error(f"hermes runtime unavailable: {exc}")

    if action == "list":
        if args.get("show_threads"):
            try:
                result = session.list_threads()
            except Exception as exc:
                return _error(f"list_threads failed: {exc}")
            if not result.get("ok"):
                return _error(result.get("error", "unknown error"))
            threads = result.get("data") or []
            return json.dumps({
                "ok": True,
                "threads": threads,
                "total": len(threads),
            }, ensure_ascii=False)

        tasks = []
        for task in session.tasks.values():
            pending = None
            if task.request_rpc_id is not None:
                pending = {"type": task.request_type}
            tasks.append({
                "task_id": task.task_id,
                "thread_id": task.thread_id,
                "cwd": task.cwd,
                "pending": pending,
            })
        return json.dumps({"ok": True, "tasks": tasks}, ensure_ascii=False)

    if action == "reply":
        task_id = (args.get("task_id") or "").strip()
        message = (args.get("message") or "").strip()
        if not task_id:
            return _error("task_id is required for reply")
        if not message:
            return _error("message is required for reply")
        try:
            result = session.send_reply(task_id, message)
        except Exception as exc:
            return _error(f"send_reply failed: {exc}")
        if not result.get("ok"):
            return _error(result.get("error", "unknown error"))
        return json.dumps({"ok": True, "task_id": task_id}, ensure_ascii=False)

    if action == "answer":
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return _error("task_id is required for answer")
        responses = args.get("responses")
        if not isinstance(responses, list) or not responses:
            return _error("responses must be a non-empty list of strings")
        if not all(isinstance(r, str) for r in responses):
            return _error("responses must be a list of strings")
        try:
            result = session.input_task(task_id, responses=responses)
        except Exception as exc:
            return _error(f"input_task failed: {exc}")
        if not result.get("ok"):
            return _error(result.get("error", "unknown error"))
        return json.dumps({"ok": True, "task_id": task_id}, ensure_ascii=False)

    if action in ("approve", "deny"):
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return _error(f"task_id is required for {action}")
        decision = "accept" if action == "approve" else "decline"
        for_session = bool(args.get("for_session")) if action == "approve" else False
        result = session.approve_task(task_id, decision, for_session=for_session)
        if not result.get("ok"):
            return _error(result.get("error", "unknown error"))
        return json.dumps({
            "ok": True,
            "task_id": task_id,
            "decision": "acceptForSession" if for_session else decision,
        }, ensure_ascii=False)

    if action == "archive":
        target = (args.get("target") or "").strip()
        if not target:
            return _error("target is required for archive")
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
        if not result.get("ok"):
            return _error(result.get("error", "unknown error"))
        return json.dumps({
            "ok": True,
            "scope": "task",
            "task_id": target,
        }, ensure_ascii=False)

    return _error(f"unknown action {action!r}")


def codex_models(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")

    try:
        session = resolve_current_session()
    except (ImportError, AttributeError) as exc:
        return _error(f"hermes runtime unavailable: {exc}")

    if action == "list":
        result = session.list_models()
        if not result.get("ok"):
            return _error(result.get("error", "unknown error"))
        return json.dumps({
            "ok": True,
            "models": result.get("data") or [],
            "current": session.get_default_model(),
        }, ensure_ascii=False)

    if action == "get_default":
        return json.dumps({
            "ok": True,
            "model": session.get_default_model(),
        }, ensure_ascii=False)

    if action == "set_default":
        model_id = (args.get("model_id") or "").strip()
        if not model_id:
            return _error("model_id is required for set_default")
        started = session.ensure_started()
        if not started.get("ok"):
            return _error(started.get("error", "failed to start session"))
        result = session.set_default_model(model_id)
        if not result.get("ok"):
            return _error(result.get("error", "unknown error"))
        return json.dumps({
            "ok": True,
            "model": result["model"],
        }, ensure_ascii=False)

    return _error(f"unknown action {action!r}")


def codex_session(args: dict, **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    if not action:
        return _error("action is required")

    try:
        session = resolve_current_session()
    except (ImportError, AttributeError) as exc:
        return _error(f"hermes runtime unavailable: {exc}")

    if action == "status":
        result = session.get_status()
        if not result.get("ok"):
            return _error(result.get("error", "unknown error"))
        return json.dumps({
            "ok": True,
            "session_key": session.session_key,
            "connected": result["connected"],
            "active_tasks": result["active_tasks"],
            "total_threads": result["total_threads"],
            "model": result["model"],
            "mode": result["mode"],
            "verbose": result["verbose"],  # "off" | "mid" | "on"
            "sandbox_policy": result["sandbox_policy"],
        }, ensure_ascii=False)

    if action == "plan_get":
        return json.dumps({
            "ok": True,
            "mode": session.get_mode(),
        }, ensure_ascii=False)

    if action == "plan_set":
        if "enabled" not in args:
            return _error("enabled is required for plan_set")
        mode = "plan" if bool(args["enabled"]) else "default"
        result = session.set_mode(mode)
        if not result.get("ok"):
            return _error(result.get("error", "unknown error"))
        return json.dumps({
            "ok": True,
            "mode": result["mode"],
        }, ensure_ascii=False)

    if action == "verbose_get":
        return json.dumps({
            "ok": True,
            "verbose": session.get_verbose(),
        }, ensure_ascii=False)

    if action == "verbose_set":
        level = (args.get("level") or "").strip()
        if not level:
            return _error("level is required for verbose_set (off/mid/on)")
        result = session.set_verbose(level)
        if not result.get("ok"):
            return _error(result.get("error", "unknown error"))
        return json.dumps({
            "ok": True,
            "verbose": result["verbose"],
        }, ensure_ascii=False)

    if action == "sandbox_get":
        return json.dumps({
            "ok": True,
            "sandbox_policy": session.get_sandbox_policy(),
        }, ensure_ascii=False)

    if action == "sandbox_set":
        policy = (args.get("sandbox_policy") or "").strip()
        if not policy:
            return _error("sandbox_policy is required for sandbox_set")
        result = session.set_sandbox_policy(policy)
        if not result.get("ok"):
            return _error(result.get("error", "unknown error"))
        return json.dumps({
            "ok": True,
            "sandbox_policy": result["sandbox_policy"],
        }, ensure_ascii=False)

    return _error(f"unknown action {action!r}")


def codex_revive(args: dict, **kwargs: Any) -> str:
    thread_id = (args.get("thread_id") or "").strip()
    if not thread_id:
        return _error("thread_id is required")

    approval_policy = args.get("approval_policy", DEFAULT_APPROVAL_POLICY)

    try:
        session = resolve_current_session()
    except (ImportError, AttributeError) as exc:
        return _error(f"hermes runtime unavailable: {exc}")

    result = session.revive_task(
        thread_id,
        approval_policy=approval_policy,
    )
    if not result.get("ok"):
        return _error(result.get("error", "unknown error"))
    return json.dumps({
        "ok": True,
        "task_id": result["task_id"],
        "thread_id": result["thread_id"],
        "model": result.get("model", session.get_default_model()),
        "message": (
            f"Thread revived as task {result['task_id']}. "
            f"Use `/codex reply {result['task_id']} <message>` to continue."
        ),
    }, ensure_ascii=False)
