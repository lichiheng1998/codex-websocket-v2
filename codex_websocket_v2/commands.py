"""Slash command handlers for /codex (WebSocket v2 — per-session).

Subcommands: list [--threads], models, model, reply, approve, deny, archive,
plan, verbose, status, help.

Every entry point starts with ``resolve_current_session()`` to get/create
the CodexSession that matches the current hermes session_key.
"""

from __future__ import annotations

import argparse
import logging
import shlex
from typing import Optional

from .session import CodexSession
from .session_registry import resolve_current_session

logger = logging.getLogger(__name__)

MAX_TASKS_DISPLAY = 20
MAX_PREVIEW_LENGTH = 60


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

    for name in ("approve", "deny"):
        p = sub.add_parser(name, add_help=True, help=f"{name} a pending request")
        p.add_argument("task_id")

    archive_p = sub.add_parser("archive", add_help=True, help="archive tasks or threads")
    archive_p.add_argument("target", help="task_id, 'all', or 'allthreads'")

    plan_p = sub.add_parser("plan", add_help=True, help="show or toggle plan mode")
    plan_p.add_argument("toggle", nargs="?", help="'on' or 'off'; omit to query")

    verbose_p = sub.add_parser("verbose", add_help=True, help="show or toggle verbose mode")
    verbose_p.add_argument("toggle", nargs="?", help="'on' or 'off'; omit to query")

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


def _cmd_help() -> str:
    return (
        "Usage:\n"
        "  `/codex` or `/codex list` — list this session's tasks\n"
        "  `/codex list --threads` — list all threads on the server\n"
        "  `/codex models` — list available models from app-server\n"
        "  `/codex model` — show current default model (this session)\n"
        "  `/codex model <model_id>` — set default model for this session\n"
        "  `/codex reply <task_id> <message>` — send follow-up to Codex\n"
        "  `/codex approve <task_id>` — approve a pending Codex request\n"
        "  `/codex deny <task_id>` — deny a pending Codex request\n"
        "  `/codex archive <task_id>` — archive a task thread\n"
        "  `/codex archive all` — archive all tasks in this session\n"
        "  `/codex archive allthreads` — archive every thread on the server\n"
        "  `/codex plan on|off` — toggle plan mode (this session)\n"
        "  `/codex verbose on|off` — toggle verbose notifications (this session)\n"
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


def _cmd_list(session: CodexSession, show_threads: bool = False) -> str:
    if show_threads:
        return _list_threads(session)
    return _list_tasks(session)


def _cmd_models(session: CodexSession) -> str:
    result = session.list_models()
    if not result.get("ok"):
        return f"Failed to list models: {result.get('error')}"

    models = result.get("data") or []
    if not models:
        return "No models returned by app-server."

    current = session.get_default_model()
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


def _cmd_model(session: CodexSession, model_id: Optional[str]) -> str:
    started = session.ensure_started()
    if not started.get("ok"):
        return f"Failed: {started.get('error')}"
    if not model_id:
        return f"Default model is `{session.get_default_model()}`."

    result = session.set_default_model(model_id)
    if not result.get("ok"):
        return f"Failed: {result.get('error')}"
    return f"Default model set to `{result['model']}`."


def _list_tasks(session: CodexSession) -> str:
    if not session.tasks:
        return "No Codex tasks in this session."
    lines = ["Codex tasks:"]
    for task in list(session.tasks.values())[:MAX_TASKS_DISPLAY]:
        flag = ""
        if task.request_rpc_id is not None:
            flag = f"  ⚠️ pending {task.request_type}"
        lines.append(f"  `{task.task_id}` → `{task.thread_id[:8]}…`{flag}")
    lines.append("\nReply: `/codex reply <task_id> <message>`")
    return "\n".join(lines)


def _list_threads(session: CodexSession) -> str:
    try:
        result = session.list_threads()
        threads = (result or {}).get("data", [])
    except Exception as exc:
        return f"Failed to list threads: {exc}"
    if not threads:
        return "No threads on server."
    total = len(threads)
    lines = [f"Codex threads ({total}):"]
    for t in threads[:MAX_TASKS_DISPLAY]:
        tid = t.get("id", "?")
        cwd = t.get("cwd", "?")
        preview = (t.get("preview") or "").replace("\n", " ")[:MAX_PREVIEW_LENGTH]
        lines.append(f"  `{tid}` — `{cwd}` {preview}")
    if total > MAX_TASKS_DISPLAY:
        lines.append(f"  … and {total - MAX_TASKS_DISPLAY} more")
    return "\n".join(lines)


def _cmd_approve(session: CodexSession, task_id: str) -> str:
    result = session.approve_task(task_id, "accept")
    if result.get("ok"):
        return f"Approved task `{task_id}`."
    return f"Failed: {result.get('error')}"


def _cmd_deny(session: CodexSession, task_id: str) -> str:
    result = session.approve_task(task_id, "decline")
    if result.get("ok"):
        return f"Denied task `{task_id}`."
    return f"Failed: {result.get('error')}"


def _cmd_archive(session: CodexSession, target: str) -> str:
    if target == "allthreads":
        result = session.archive_all_threads()
        if result.get("ok"):
            return f"Archived {result['removed']} threads."
        return f"Archived {result['removed']}, failed: {', '.join(result['errors'])}"
    if target == "all":
        result = session.remove_all_tasks()
        if result.get("ok"):
            return f"Archived {result['removed']} tasks."
        return f"Archived {result['removed']}, failed: {', '.join(result['errors'])}"
    result = session.remove_task(target)
    if result.get("ok"):
        return f"Task `{target}` archived."
    return f"Failed: {result.get('error')}"


def _cmd_plan(session: CodexSession, toggle: Optional[str]) -> str:
    if toggle is None:
        return f"Plan mode is `{session.get_mode()}`."
    normalized = toggle.strip().lower()
    if normalized in ("on", "true", "1", "enable", "enabled"):
        session.set_mode("plan")
        return "Plan mode `on` — future turns will use collaborationMode=plan."
    if normalized in ("off", "false", "0", "disable", "disabled"):
        session.set_mode("default")
        return "Plan mode `off` — future turns will use collaborationMode=default."
    return f"Unknown toggle `{toggle}`. Use `/codex plan on` or `/codex plan off`."


def _cmd_verbose(session: CodexSession, toggle: Optional[str]) -> str:
    if toggle is None:
        state = "on" if session.get_verbose() else "off"
        return f"Verbose mode is `{state}`."
    normalized = toggle.strip().lower()
    if normalized in ("on", "true", "1", "enable", "enabled"):
        session.set_verbose(True)
        return "Verbose mode `on` — item/completed notifications will be shown."
    if normalized in ("off", "false", "0", "disable", "disabled"):
        session.set_verbose(False)
        return "Verbose mode `off` — only turn/completed notifications will be shown."
    return f"Unknown toggle `{toggle}`. Use `/codex verbose on` or `/codex verbose off`."


def _cmd_reply(session: CodexSession, ns: argparse.Namespace) -> str:
    task_id = ns.task_id
    message = " ".join(ns.message).strip() if ns.message else ""
    if not message:
        return "Missing message. Usage: `/codex reply <task_id> <message>`"
    try:
        result = session.send_reply(task_id, message)
    except Exception as exc:
        logger.exception("codex /reply failed")
        return f"Failed to send reply: {exc}"
    if not result.get("ok"):
        return f"Failed: {result.get('error', 'unknown error')}"
    return f"Message sent to Codex task `{task_id}`, waiting for reply..."


def _cmd_status(session: CodexSession) -> str:
    status = session.get_status()
    if not status.get("ok"):
        return f"Failed to get status: {status.get('error')}"

    conn = "connected" if status["connected"] else "disconnected"
    return (
        "**CodexSession Status**\n"
        f"• Session key: `{session.session_key}`\n"
        f"• Connection: {conn}\n"
        f"• Active tasks: {status['active_tasks']}\n"
        f"• Total threads: {status['total_threads']}\n"
        f"• Default model: `{status['model']}`\n"
        f"• Mode: `{status['mode']}`\n"
        f"• Verbose: {'on' if status['verbose'] else 'off'}"
    )


def handle_slash(raw_args: str) -> str:
    session = resolve_current_session()
    ns = _parse_args(raw_args or "")

    if ns is None or ns.command is None:
        return _cmd_list(session)

    if ns.command == "__help__":
        return (ns.help_text or _cmd_help()).strip()

    if ns.command == "help":
        return _cmd_help_topic(getattr(ns, "topic", None))

    if ns.command == "list":
        return _cmd_list(session, show_threads=ns.threads)

    if ns.command == "models":
        return _cmd_models(session)

    if ns.command == "model":
        return _cmd_model(session, ns.model_id)

    if ns.command == "approve":
        return _cmd_approve(session, ns.task_id)

    if ns.command == "deny":
        return _cmd_deny(session, ns.task_id)

    if ns.command == "archive":
        return _cmd_archive(session, ns.target)

    if ns.command == "plan":
        return _cmd_plan(session, ns.toggle)

    if ns.command == "verbose":
        return _cmd_verbose(session, ns.toggle)

    if ns.command == "status":
        return _cmd_status(session)

    if ns.command == "reply":
        return _cmd_reply(session, ns)

    return f"Unknown subcommand `{ns.command}`. Try `/codex help`."
