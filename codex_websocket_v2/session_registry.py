"""Process-level registry of CodexSession instances, keyed by ``platform:chat_id``.

Lazy creation: ``resolve_current_session()`` reads platform + chat_id from
contextvars or (as a fallback for slash commands) walks the call stack to
find the gateway's ``event``/``source``. One CodexSession per chat.

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
from typing import Any, Dict, List, Optional

from .session import CodexSession
from .state import TaskTarget

logger = logging.getLogger(__name__)

_sessions: Dict[str, CodexSession] = {}
_lock = threading.Lock()

# Maximum number of stack frames to search for the gateway event/source.
_MAX_STACK_DEPTH = 15

# Fallback session_key when neither contextvars nor the stack yields a source
# (e.g. CLI mode, cron jobs, ad-hoc imports).
_CLI_FALLBACK_KEY = "local:cli"


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

    Uses ``platform:chat_id`` as the registry key — one CodexSession per chat.

    Resolution order:
    1. Contextvars (set by the gateway's ``_set_session_env()`` before agent
       dispatch). LLM tool calls reach this path.
    2. Stack inspection — fallback for plugin slash commands, which are
       dispatched *before* contextvars are set. Walks the stack for the
       gateway's ``event``/``source`` local.
    3. Falls back to ``_CLI_FALLBACK_KEY`` if neither path yields a source.
    """
    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "")

    # Stack fallback: contextvars are empty during plugin slash dispatch.
    if not platform or not chat_id:
        source = _resolve_source_from_stack()
        if source is not None:
            platform = platform or _platform_str(source)
            chat_id = chat_id or _attr_str(source, "chat_id")
            thread_id = thread_id or _attr_str(source, "thread_id")

    if not platform or not chat_id:
        session_key = _CLI_FALLBACK_KEY
        chat_id = chat_id or "cli"
    else:
        session_key = f"{platform}:{chat_id}"

    logger.info(
        "v2 resolve_current_session: key=%r platform=%r chat_id=%r thread=%r",
        session_key, platform, chat_id, thread_id,
    )
    target = TaskTarget(platform=platform, chat_id=chat_id, thread_id=thread_id)
    return get_or_create(session_key, target)


def _resolve_source_from_stack() -> Optional[Any]:
    """Walk the call stack for the gateway's ``event``/``source`` local.

    Returns the SessionSource (or whatever object is at ``source`` / ``event.source``)
    if found, else ``None``. Caller is responsible for deriving fields.
    """
    try:
        frames = inspect.stack()[1:]  # skip this frame
    except Exception:
        return None

    for frame_info in frames[:_MAX_STACK_DEPTH]:
        locals_ = frame_info.frame.f_locals

        # ``event`` (MessageEvent) carries .source with full metadata.
        event = locals_.get("event")
        source = getattr(event, "source", None) if event is not None else None

        # Some frames may have ``source`` directly.
        if source is None:
            source = locals_.get("source")

        if source is not None and _attr_str(source, "chat_id"):
            logger.debug(
                "v2 stack fallback: found source in frame %s", frame_info.function,
            )
            return source

    return None


def _platform_str(source: Any) -> str:
    """Normalise ``source.platform`` (Platform enum or str) to its string value."""
    plat = getattr(source, "platform", None)
    if plat is None:
        return ""
    # Platform enum has .value="telegram"; plain strings pass through.
    return getattr(plat, "value", plat) or ""


def _attr_str(obj: Any, name: str, default: str = "") -> str:
    """Read a string attribute, returning *default* on missing/None/error.

    Unlike a naive ``str(getattr(...))``, this does NOT stringify enums (which
    would produce ``"Platform.TELEGRAM"`` instead of the bare ``"telegram"``).
    Use ``_platform_str`` for enum-bearing fields.
    """
    try:
        val = getattr(obj, name, default)
        if val is None:
            return default
        if isinstance(val, str):
            return val
        # Numbers (telegram chat_id is int in some adapters) → str
        return str(val)
    except Exception:
        return default