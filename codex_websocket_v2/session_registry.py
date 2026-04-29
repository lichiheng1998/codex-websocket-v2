"""Process-level registry of CodexSession instances, keyed by chat_id.

Lazy creation: ``resolve_current_session()`` reads the chat_id from
contextvars or (as a fallback for slash commands) from the gateway's
call-stack frame, then returns (creating if needed) the matching
CodexSession.

**Slash-command fallback**: Plugin slash commands (``/codex``) are dispatched
by the gateway *before* ``_set_session_env()`` populates contextvars, so the
normal contextvar path returns empty values.  As a workaround we walk the call
stack to find the ``event`` or ``source`` local variable from the gateway's
``_handle_message`` frame, which is already populated at dispatch time.
"""

from __future__ import annotations

import inspect
import logging
import threading
from typing import Dict, List, Optional, Tuple

from .session import CodexSession
from .state import TaskTarget

logger = logging.getLogger(__name__)

_sessions: Dict[str, CodexSession] = {}
_lock = threading.Lock()

# Maximum number of stack frames to search for the gateway event/source.
_MAX_STACK_DEPTH = 15


def get(session_key: str) -> Optional[CodexSession]:
    with _lock:
        return _sessions.get(session_key)


def get_or_create(session_key: str, target: TaskTarget) -> CodexSession:
    with _lock:
        session = _sessions.get(session_key)
        if session is None:
            session = CodexSession(session_key=session_key, target=target)
            _sessions[session_key] = session
        else:
            # Refresh target — chat metadata may have changed (e.g. thread_id).
            session.target = target
        return session


def remove(session_key: str) -> Optional[CodexSession]:
    with _lock:
        return _sessions.pop(session_key, None)


def all_sessions() -> List[CodexSession]:
    with _lock:
        return list(_sessions.values())


def clear() -> None:
    with _lock:
        _sessions.clear()


def resolve_current_session() -> CodexSession:
    """Resolve the CodexSession for the *current* hermes call context.

    Uses ``chat_id`` as the session key — one CodexSession per chat.

    Resolution order:
    1. Contextvars (``gateway.session_context``) — works for tool calls and
       any code that runs *after* the gateway's ``_set_session_env()``.
    2. Stack inspection — fallback for slash commands, which are dispatched
       *before* contextvars are set.  Walks up the call stack to find the
       ``event`` or ``source`` local variable from ``_handle_message``.
    3. Falls back to ``"cli"`` if neither path yields a chat_id.
    """
    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "")

    # If contextvars are empty (slash command dispatch), try stack inspection.
    if not chat_id:
        pl, ci, ti = _resolve_from_stack()
        platform = platform or pl
        chat_id = ci
        thread_id = thread_id or ti

    session_key = chat_id or "cli"
    logger.info(
        "v2 resolve_current_session: key=%r platform=%r chat_id=%r thread=%r",
        session_key, platform, chat_id, thread_id,
    )
    target = TaskTarget(platform=platform, chat_id=chat_id, thread_id=thread_id)
    return get_or_create(session_key, target)


def _resolve_from_stack() -> Tuple[str, str, str]:
    """Walk the call stack to extract session info from the gateway's ``_handle_message``.

    Returns ``(platform, chat_id, thread_id)`` — any field may be empty if the
    frame or its locals are not found.
    """
    try:
        frames = inspect.stack()[1:]  # skip this frame
    except Exception:
        return ("", "", "")

    for frame_info in frames[:_MAX_STACK_DEPTH]:
        locals_ = frame_info.frame.f_locals

        # Look for ``event`` (MessageEvent) — has .source with full metadata.
        event = locals_.get("event")
        source = getattr(event, "source", None) if event is not None else None

        # Some frames may only have ``source`` directly.
        if source is None:
            source = locals_.get("source")

        if source is not None:
            platform = _get_attr(source, "platform")
            # platform may be an enum; normalise to string.
            platform = getattr(platform, "value", platform) or ""
            chat_id = _get_attr(source, "chat_id") or ""
            thread_id = _get_attr(source, "thread_id") or ""
            if chat_id:
                logger.debug(
                    "v2 stack fallback: platform=%r chat_id=%r thread=%r (frame=%s)",
                    platform, chat_id, thread_id, frame_info.function,
                )
                return (platform, chat_id, str(thread_id))

    return ("", "", "")


def _get_attr(obj: object, name: str, default: str = "") -> str:
    """Read an attribute, coercing to str; returns *default* on any failure."""
    try:
        val = getattr(obj, name, default)
        return str(val) if val is not None else default
    except Exception:
        return default
