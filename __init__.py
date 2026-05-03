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
    import asyncio
    from . import schemas
    from . import tools
    from .codex_websocket_v2.surfaces import commands
    from .codex_websocket_v2.surfaces import notify

    # Capture the hermes main event loop so cross-thread sends (codex bridge
    # runs on its own loop; weixin's aiohttp session is bound to the main
    # loop) can be scheduled correctly. In CLI mode there is no async loop
    # at register-time — leave it as None and notify_user falls through to
    # a direct await (single-loop world, no bridging needed).
    try:
        notify.set_main_loop(asyncio.get_running_loop())
    except RuntimeError:
        notify.set_main_loop(None)

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
        name="codex_approval",
        toolset="codex_bridge",
        schema=schemas.CODEX_APPROVAL,
        handler=tools.codex_approval,
        check_fn=_codex_available,
    )
    ctx.register_tool(
        name="codex_action",
        toolset="codex_bridge",
        schema=schemas.CODEX_ACTION,
        handler=tools.codex_action,
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
    from .codex_websocket_v2.core import session_registry
    from .codex_websocket_v2.transport.server_manager import CodexServerManager

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
