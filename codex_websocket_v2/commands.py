"""Slash command handlers for /codex (WebSocket v2 — per-session).

Subcommands: list [--threads], models, model, reply, answer, approve, deny,
archive, plan, verbose, status, help.

Each subcommand parses argv with argparse, then delegates to a registered
tool via ``_DISPATCH`` (wired by ``__init__.register()``). The tool's JSON
result is reformatted into the human display string (byte-for-byte
compatible with the pre-refactor output).
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MAX_TASKS_DISPLAY = 20
MAX_PREVIEW_LENGTH = 60


# ---------------------------------------------------------------------------
# Dispatch bridge — wired at register() time by __init__.py
# ---------------------------------------------------------------------------

_DISPATCH: Optional[Callable[[str, dict], str]] = None


def set_dispatch(dispatch_fn: Callable[[str, dict], str]) -> None:
    """Wire ``ctx.dispatch_tool`` so slash handlers can invoke registered tools."""
    global _DISPATCH
    _DISPATCH = dispatch_fn


def _call(tool_name: str, args: dict) -> dict:
    if _DISPATCH is None:
        return {"ok": False, "error": "codex commands: dispatch not wired (plugin not registered?)"}
    try:
        raw = _DISPATCH(tool_name, args)
    except Exception as exc:  # pragma: no cover — registry should swallow most errors
        logger.exception("codex commands: dispatch %s failed", tool_name)
        return {"ok": False, "error": f"dispatch failed: {exc}"}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"ok": False, "error": f"non-JSON tool response: {raw!r}"}
    if not isinstance(parsed, dict):
        return {"ok": False, "error": f"unexpected tool response shape: {parsed!r}"}
    return parsed


# ---------------------------------------------------------------------------
# argparse setup
# ---------------------------------------------------------------------------


class _CodexHelpRequested(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class _CodexArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:
        raise _CodexHelpRequested(message or self.format_help())


def _build_parser() -> argparse.ArgumentParser:
    parser = _CodexArgumentParser(prog="/codex", add_help=True)
    sub = parser.add_subparsers(dest="command")

    list_p = sub.add_parser("list", add_help=True, help="list tasks or threads")
    list_p.add_argument("--threads", "-t", action="store_true")

    sub.add_parser("models", add_help=True, help="list available models")

    model_p = sub.add_parser("model", add_help=True, help="show or set default model")
    model_p.add_argument("model_id", nargs="?", help="set or show default model")

    reply_p = sub.add_parser("reply", add_help=True, help="send follow-up to a task")
    reply_p.add_argument("task_id")
    reply_p.add_argument("message", nargs=argparse.REMAINDER)

    answer_p = sub.add_parser(
        "answer", add_help=True,
        help="answer a pending requestUserInput (separate multiple answers with ' | ')",
    )
    answer_p.add_argument("task_id")
    answer_p.add_argument("answers", nargs=argparse.REMAINDER)

    approve_p = sub.add_parser("approve", add_help=True, help="approve a pending request")
    approve_p.add_argument("task_id")
    approve_p.add_argument(
        "--all", "-a", dest="for_session", action="store_true",
        help="send acceptForSession (stop prompting for similar commands this session)",
    )

    deny_p = sub.add_parser("deny", add_help=True, help="deny a pending request")
    deny_p.add_argument("task_id")

    archive_p = sub.add_parser("archive", add_help=True, help="archive tasks or threads")
    archive_p.add_argument("task_id", nargs="?", help="task_id to archive")
    archive_p.add_argument(
        "--all", "-a", dest="all_tasks", action="store_true",
        help="archive all tasks in this session",
    )
    archive_p.add_argument(
        "--threads", "-t", dest="all_threads", action="store_true",
        help="archive every thread on the server",
    )

    plan_p = sub.add_parser("plan", add_help=True, help="show or toggle plan mode")
    plan_p.add_argument("toggle", nargs="?", help="'on' or 'off'; omit to query")

    verbose_p = sub.add_parser("verbose", add_help=True, help="show or set verbose level")
    verbose_p.add_argument("level", nargs="?", help="'off', 'mid', or 'on'; omit to query")

    sub.add_parser("status", add_help=True, help="show session status")

    help_p = sub.add_parser("help", add_help=True, help="show help")
    help_p.add_argument("topic", nargs="?", help="optional subcommand name")

    return parser


PARSER = _build_parser()


def _parse_args(raw: str) -> Optional[argparse.Namespace]:
    try:
        tokens = shlex.split(raw) if raw else []
    except ValueError:
        tokens = raw.split()
    if not tokens:
        return argparse.Namespace(command=None)
    try:
        return PARSER.parse_args(tokens)
    except _CodexHelpRequested as exc:
        return argparse.Namespace(command="__help__", help_text=exc.text)
    except (SystemExit, Exception):
        return None


# ---------------------------------------------------------------------------
# Help text — kept local (not routed through any tool)
# ---------------------------------------------------------------------------


def _cmd_help() -> str:
    return (
        "Usage:\n"
        "  `/codex` or `/codex list` — list this session's tasks\n"
        "  `/codex list --threads` — list all threads on the server\n"
        "  `/codex models` — list available models from app-server\n"
        "  `/codex model` — show current default model (this session)\n"
        "  `/codex model <model_id>` — set default model for this session\n"
        "  `/codex reply <task_id> <message>` — send follow-up turn to Codex\n"
        "  `/codex answer <task_id> <answer>` — answer a Codex question\n"
        "  `/codex answer <task_id> <a1> | <a2> | <a3>` — answer multiple questions (separate with ' | ')\n"
        "  `/codex approve <task_id>` — approve a pending Codex request\n"
        "  `/codex approve --all <task_id>` — approve and stop prompting for similar commands this session\n"
        "  `/codex deny <task_id>` — deny a pending Codex request\n"
        "  `/codex archive <task_id>` — archive a specific task\n"
        "  `/codex archive --all` — archive all tasks in this session\n"
        "  `/codex archive --threads` — archive every thread on the server\n"
        "  `/codex plan on|off` — toggle plan mode (this session)\n"
        "  `/codex verbose off|mid|on` — set verbosity (off = last item + turn end; mid = agentMessage + turn end; on = all)\n"
        "  `/codex status` — show session status"
    )


def _cmd_help_topic(topic: Optional[str]) -> str:
    if not topic:
        return _cmd_help()
    try:
        return PARSER.parse_args([topic, "--help"]).help_text
    except _CodexHelpRequested as exc:
        return exc.text.strip()
    except Exception:
        return f"Unknown help topic `{topic}`. Try `/codex --help`."


# ---------------------------------------------------------------------------
# Tool-routed subcommand handlers — each parses argv into args dict, calls
# the matching tool via _call(), then formats the JSON result for display.
# ---------------------------------------------------------------------------


def _cmd_list(show_threads: bool = False) -> str:
    if show_threads:
        return _list_threads()
    return _list_tasks()


def _list_tasks() -> str:
    result = _call("codex_tasks", {"action": "list", "show_threads": False})
    if not result.get("ok"):
        return f"Failed: {result.get('error', 'unknown error')}"
    tasks = result.get("tasks") or []
    if not tasks:
        return "No Codex tasks in this session."
    lines = ["Codex tasks:"]
    for task in tasks[:MAX_TASKS_DISPLAY]:
        flag = ""
        pending = task.get("pending")
        if pending:
            flag = f"  ⚠️ pending {pending.get('type')}"
        thread_id = task.get("thread_id") or ""
        lines.append(f"  `{task.get('task_id')}` → `{thread_id[:8]}…`{flag}")
    lines.append("\nReply: `/codex reply <task_id> <message>`")
    return "\n".join(lines)


def _list_threads() -> str:
    result = _call("codex_tasks", {"action": "list", "show_threads": True})
    if not result.get("ok"):
        return f"Failed to list threads: {result.get('error', 'unknown error')}"
    threads = result.get("threads") or []
    if not threads:
        return "No threads on server."
    total = result.get("total", len(threads))
    lines = [f"Codex threads ({total}):"]
    for t in threads[:MAX_TASKS_DISPLAY]:
        tid = t.get("id", "?")
        cwd = t.get("cwd", "?")
        preview = (t.get("preview") or "").replace("\n", " ")[:MAX_PREVIEW_LENGTH]
        lines.append(f"  `{tid}` — `{cwd}` {preview}")
    if total > MAX_TASKS_DISPLAY:
        lines.append(f"  … and {total - MAX_TASKS_DISPLAY} more")
    return "\n".join(lines)


def _cmd_models() -> str:
    result = _call("codex_models", {"action": "list"})
    if not result.get("ok"):
        return f"Failed to list models: {result.get('error', 'unknown error')}"

    models = result.get("models") or []
    if not models:
        return "No models returned by app-server."

    current = result.get("current") or ""
    lines = ["Available models:"]
    for item in models:
        model_id = item.get("id") or item.get("model") or "?"
        display = item.get("displayName") or ""
        flags = []
        if item.get("isDefault"):
            flags.append("server default")
        if model_id == current or item.get("model") == current:
            flags.append("current")
        suffix = f" ({', '.join(flags)})" if flags else ""
        label = f" — {display}" if display and display != model_id else ""
        lines.append(f"  `{model_id}`{label}{suffix}")
    return "\n".join(lines)


def _cmd_model(model_id: Optional[str]) -> str:
    if not model_id:
        result = _call("codex_models", {"action": "get_default"})
        if not result.get("ok"):
            return f"Failed: {result.get('error', 'unknown error')}"
        return f"Default model is `{result.get('model', '')}`."

    result = _call("codex_models", {"action": "set_default", "model_id": model_id})
    if not result.get("ok"):
        return f"Failed: {result.get('error', 'unknown error')}"
    return f"Default model set to `{result['model']}`."


def _cmd_approve(task_id: str, for_session: bool = False) -> str:
    result = _call("codex_tasks", {"action": "approve", "task_id": task_id, "for_session": for_session})
    if result.get("ok"):
        if for_session:
            return f"Approved task `{task_id}` for session (similar commands won't prompt again)."
        return f"Approved task `{task_id}`."
    return f"Failed: {result.get('error', 'unknown error')}"


def _cmd_deny(task_id: str) -> str:
    result = _call("codex_tasks", {"action": "deny", "task_id": task_id})
    if result.get("ok"):
        return f"Denied task `{task_id}`."
    return f"Failed: {result.get('error', 'unknown error')}"


def _cmd_archive(ns: argparse.Namespace) -> str:
    if ns.all_threads:
        target = "allthreads"
    elif ns.all_tasks:
        target = "all"
    elif ns.task_id:
        target = ns.task_id
    else:
        return "Specify a task_id, --all, or --threads. Usage: `/codex archive [--all | --threads | <task_id>]`"

    result = _call("codex_tasks", {"action": "archive", "target": target})
    scope = result.get("scope")

    if scope == "allthreads":
        removed = result.get("removed", 0)
        errors = result.get("errors") or []
        if result.get("ok"):
            return f"Archived {removed} threads."
        return f"Archived {removed}, failed: {', '.join(errors)}"

    if scope == "all":
        removed = result.get("removed", 0)
        errors = result.get("errors") or []
        if result.get("ok"):
            return f"Archived {removed} tasks."
        return f"Archived {removed}, failed: {', '.join(errors)}"

    if result.get("ok"):
        return f"Task `{target}` archived."
    return f"Failed: {result.get('error', 'unknown error')}"


def _cmd_plan(toggle: Optional[str]) -> str:
    if toggle is None:
        result = _call("codex_session", {"action": "plan_get"})
        if not result.get("ok"):
            return f"Failed: {result.get('error', 'unknown error')}"
        return f"Plan mode is `{result.get('mode', '')}`."
    normalized = toggle.strip().lower()
    if normalized in ("on", "true", "1", "enable", "enabled"):
        enabled = True
    elif normalized in ("off", "false", "0", "disable", "disabled"):
        enabled = False
    else:
        return f"Unknown toggle `{toggle}`. Use `/codex plan on` or `/codex plan off`."

    result = _call("codex_session", {"action": "plan_set", "enabled": enabled})
    if not result.get("ok"):
        return f"Failed: {result.get('error', 'unknown error')}"
    if enabled:
        return "Plan mode `on` — future turns will use collaborationMode=plan."
    return "Plan mode `off` — future turns will use collaborationMode=default."


def _cmd_verbose(level: Optional[str]) -> str:
    if level is None:
        result = _call("codex_session", {"action": "verbose_get"})
        if not result.get("ok"):
            return f"Failed: {result.get('error', 'unknown error')}"
        return f"Verbose level is `{result.get('verbose', 'off')}`. Options: off / mid / on"
    normalized = level.strip().lower()
    if normalized in ("on", "true", "1", "enable", "enabled"):
        normalized = "on"
    elif normalized in ("off", "false", "0", "disable", "disabled"):
        normalized = "off"
    elif normalized != "mid":
        return f"Unknown level `{level}`. Use: `/codex verbose off|mid|on`"

    result = _call("codex_session", {"action": "verbose_set", "level": normalized})
    if not result.get("ok"):
        return f"Failed: {result.get('error', 'unknown error')}"
    descriptions = {
        "off": "last item/completed + turn/completed",
        "mid": "agentMessage + turn/completed",
        "on": "all item/completed notifications",
    }
    return f"Verbose `{normalized}` — {descriptions[normalized]}."


def _cmd_reply(ns: argparse.Namespace) -> str:
    task_id = ns.task_id
    message = " ".join(ns.message).strip() if ns.message else ""
    if not message:
        return "Missing message. Usage: `/codex reply <task_id> <message>`"
    result = _call("codex_tasks", {
        "action": "reply",
        "task_id": task_id,
        "message": message,
    })
    if not result.get("ok"):
        return f"Failed: {result.get('error', 'unknown error')}"
    return f"Message sent to Codex task `{task_id}`, waiting for reply..."


def _cmd_answer(ns: argparse.Namespace) -> str:
    task_id = ns.task_id
    raw = " ".join(ns.answers).strip() if ns.answers else ""
    if not raw:
        return "Missing answer. Usage: `/codex answer <task_id> <answer>` or `/codex answer <task_id> <a1> | <a2> | <a3>`"
    responses = [r.strip() for r in raw.split(" | ")]
    responses = [r for r in responses if r]
    if not responses:
        return "Empty answer."
    result = _call("codex_tasks", {
        "action": "answer",
        "task_id": task_id,
        "responses": responses,
    })
    if not result.get("ok"):
        return f"Failed: {result.get('error', 'unknown error')}"
    n = len(responses)
    return f"Answered {n} question{'s' if n != 1 else ''} for Codex task `{task_id}`."


def _cmd_status() -> str:
    result = _call("codex_session", {"action": "status"})
    if not result.get("ok"):
        return f"Failed to get status: {result.get('error', 'unknown error')}"

    conn = "connected" if result["connected"] else "disconnected"
    return (
        "**CodexSession Status**\n"
        f"• Session key: `{result['session_key']}`\n"
        f"• Connection: {conn}\n"
        f"• Active tasks: {result['active_tasks']}\n"
        f"• Total threads: {result['total_threads']}\n"
        f"• Default model: `{result['model']}`\n"
        f"• Mode: `{result['mode']}`\n"
        f"• Verbose: `{result['verbose']}`"
    )


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------


def handle_slash(raw_args: str) -> str:
    ns = _parse_args(raw_args or "")

    if ns is None or ns.command is None:
        return _cmd_list()

    if ns.command == "__help__":
        return (ns.help_text or _cmd_help()).strip()

    if ns.command == "help":
        return _cmd_help_topic(getattr(ns, "topic", None))

    if ns.command == "list":
        return _cmd_list(show_threads=ns.threads)

    if ns.command == "models":
        return _cmd_models()

    if ns.command == "model":
        return _cmd_model(ns.model_id)

    if ns.command == "approve":
        return _cmd_approve(ns.task_id, for_session=getattr(ns, "for_session", False))

    if ns.command == "deny":
        return _cmd_deny(ns.task_id)

    if ns.command == "archive":
        return _cmd_archive(ns)

    if ns.command == "plan":
        return _cmd_plan(ns.toggle)

    if ns.command == "verbose":
        return _cmd_verbose(ns.level)

    if ns.command == "status":
        return _cmd_status()

    if ns.command == "reply":
        return _cmd_reply(ns)

    if ns.command == "answer":
        return _cmd_answer(ns)

    return f"Unknown subcommand `{ns.command}`. Try `/codex help`."
