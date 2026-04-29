"""Small pure utilities — no hermes runtime imports."""

from __future__ import annotations

import secrets
import socket
from typing import Any


def extract_thread_id(obj: Any) -> str:
    """Best-effort scrape of a thread/conversation id from a server payload.

    Codex's response shapes have varied between revisions: top-level
    ``threadId`` / ``conversationId`` / ``thread_id``, a nested ``thread``
    dict with ``id`` or ``threadId``, or a top-level ``id`` that happens
    to look like a UUID. Empty string when nothing matches.
    """
    if not isinstance(obj, dict):
        return ""
    for key in ("threadId", "conversationId", "thread_id"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    thread = obj.get("thread")
    if isinstance(thread, dict):
        tid = thread.get("id") or thread.get("threadId")
        if isinstance(tid, str) and tid:
            return tid
    tid = obj.get("id")
    if isinstance(tid, str) and len(tid) >= 8 and "-" in tid:
        return tid
    return ""


def new_task_id() -> str:
    """8-hex-char id used as the bridge-internal handle for a task."""
    return secrets.token_hex(4)


def pick_free_port() -> int:
    """Bind ephemeral, return the kernel's chosen port. Caller races to
    use it before something else grabs it — fine for a localhost spawn."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
