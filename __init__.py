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
    from .codex_websocket_v2 import commands

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
    ctx.register_tool(
        name="codex_tasks",
        toolset="codex_bridge",
        schema=schemas.CODEX_TASKS,
        handler=tools.codex_tasks,
        check_fn=_codex_available,
    )
    ctx.register_tool(
        name="codex_models",
        toolset="codex_bridge",
        schema=schemas.CODEX_MODELS,
        handler=tools.codex_models,
        check_fn=_codex_available,
    )
    ctx.register_tool(
        name="codex_session",
        toolset="codex_bridge",
        schema=schemas.CODEX_SESSION,
        handler=tools.codex_session,
        check_fn=_codex_available,
    )

    commands.set_dispatch(ctx.dispatch_tool)

    ctx.register_command(
        "codex",
        handler=commands.handle_slash,
        description="Codex task management (per-session, v2)",
        args_hint="<subcommand> [args]",
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
