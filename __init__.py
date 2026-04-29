"""Codex-WebSocket v2 plugin — per-hermes-session connections.

Each hermes session_key gets its own ``CodexSession`` (independent
WebSocket + event loop). The codex app-server subprocess is shared
across sessions via ``CodexServerManager``.
"""

from __future__ import annotations

import atexit
from shutil import which


def _codex_available() -> bool:
    return which("codex") is not None


def register(ctx) -> None:
    from . import schemas
    from . import tools
    from .codex_websocket_v2.commands import handle_slash

    ctx.register_tool(
        name="codex_task",
        toolset="codex_bridge",
        schema=schemas.CODEX_TASK,
        handler=tools.codex_task,
        check_fn=_codex_available,
    )
    ctx.register_tool(
        name="codex_revive",
        toolset="codex_bridge",
        schema=schemas.CODEX_REVIVE,
        handler=tools.codex_revive,
        check_fn=_codex_available,
    )
    ctx.register_command(
        "codex",
        handler=handle_slash,
        description="Codex task management (per-session, v2)",
    )

    atexit.register(_shutdown_all)


def _shutdown_all() -> None:
    from .codex_websocket_v2 import session_registry
    from .codex_websocket_v2.server_manager import CodexServerManager

    for session in session_registry.all_sessions():
        try:
            session.shutdown()
        except Exception:
            pass
    session_registry.clear()
    # Belt-and-suspenders: force-shutdown the manager in case ref counts drift.
    try:
        CodexServerManager.instance().force_shutdown()
    except Exception:
        pass
