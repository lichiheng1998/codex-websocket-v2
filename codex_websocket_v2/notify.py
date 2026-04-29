"""Push user-facing messages to whichever chat platform the task is bound to.

The bridge routes every progress/approval/completion message through
``notify_user``. This module owns the platform-name → ``Platform`` enum
mapping and the dynamic ``gateway`` / ``tools.send_message_tool`` imports
(those modules are part of the hermes runtime and may be absent in unit
tests / standalone use, so failures are caught and downgraded to a log
warning).

**Cross-loop dispatch**: codex bridges run on their own dedicated event
loop (``codex-ws-{session_key}`` thread). Some platform adapters (e.g.
weixin's live adapter) hold an aiohttp ``ClientSession`` that is bound to
the hermes main loop — awaiting their ``send`` from the bridge loop raises
``Timeout context manager should be used inside a task``. To fix this,
``set_main_loop()`` records the hermes main loop at plugin register-time;
``notify_user`` then schedules the send via ``run_coroutine_threadsafe``
when the calling loop differs from the main loop.

``report_failure`` is the bridge's standard "task failed at stage X with
detail Y" helper — kept here because its only side effect is one
notify call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .state import TaskTarget

logger = logging.getLogger(__name__)

# Captured at plugin register-time when hermes is running async (gateway mode).
# Used to route platform sends to the loop the live adapters are bound to.
# Stays None in CLI mode — there's no separate "main loop" to bridge to.
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Record the hermes main event loop. Called by the plugin's ``register()``."""
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def _try_capture_main_loop() -> None:
    """Lazy capture of the gateway event loop if ``set_main_loop`` wasn't called.

    This covers edge cases where ``register()`` ran outside the async context
    (e.g. early import via ``model_tools``) and ``_MAIN_LOOP`` stayed None.
    """
    global _MAIN_LOOP
    if _MAIN_LOOP is not None:
        return
    try:
        _MAIN_LOOP = asyncio.get_running_loop()
    except RuntimeError:
        pass


async def _send_via_main_loop(coro_factory) -> None:
    """Schedule ``coro_factory()`` on the hermes main loop and await its result.

    If the current loop *is* the main loop (or no main loop was captured),
    falls through to a direct ``await coro_factory()``.
    """
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None

    if _MAIN_LOOP is None or current is _MAIN_LOOP or not _MAIN_LOOP.is_running():
        logger.info(
            "codex notify: direct send "
            "(main_loop_set=%s, on_main_loop=%s, main_loop_running=%s)",
            _MAIN_LOOP is not None,
            current is _MAIN_LOOP if _MAIN_LOOP is not None else None,
            _MAIN_LOOP.is_running() if _MAIN_LOOP is not None else None,
        )
        await coro_factory()
        return

    logger.info("codex notify: dispatching to main loop via run_coroutine_threadsafe")
    future = asyncio.run_coroutine_threadsafe(coro_factory(), _MAIN_LOOP)
    # wrap_future bridges the concurrent.futures.Future to the calling loop.
    await asyncio.wrap_future(future)


async def notify_user(target: Optional[TaskTarget], message: str) -> None:
    """Best-effort push to the chat platform identified by ``target``.

    Returns silently on every failure path; callers are background driver
    coroutines that have nowhere to surface the error to anyway.
    """
    if target is None or not target.platform or not target.chat_id:
        logger.info("codex notify (no target): %s", message[:200])
        return
    try:
        _try_capture_main_loop()

        from gateway.config import load_gateway_config, Platform
        from tools.send_message_tool import _send_to_platform

        platform_map = {
            "telegram": Platform.TELEGRAM, "discord": Platform.DISCORD,
            "slack": Platform.SLACK, "whatsapp": Platform.WHATSAPP,
            "signal": Platform.SIGNAL, "bluebubbles": Platform.BLUEBUBBLES,
            "qqbot": Platform.QQBOT, "matrix": Platform.MATRIX,
            "mattermost": Platform.MATTERMOST,
            "homeassistant": Platform.HOMEASSISTANT,
            "dingtalk": Platform.DINGTALK, "feishu": Platform.FEISHU,
            "wecom": Platform.WECOM, "weixin": Platform.WEIXIN,
            "email": Platform.EMAIL, "sms": Platform.SMS,
        }
        platform = platform_map.get(target.platform.lower())
        if platform is None:
            logger.warning("codex notify: unknown platform %r", target.platform)
            return

        cfg = load_gateway_config()
        pconfig = cfg.platforms.get(platform)
        if pconfig is None:
            logger.warning("codex notify: platform %s not configured", platform)
            return

        chat_id = target.chat_id
        thread_id = target.thread_id or None

        await _send_via_main_loop(
            lambda: _send_to_platform(
                platform, pconfig, chat_id, message, thread_id=thread_id,
            )
        )
    except Exception as exc:
        logger.warning("codex notify failed: %s", exc)


async def report_failure(
    target: Optional[TaskTarget], task_id: str, stage: str, detail: str,
) -> None:
    """Fire-and-forget error reporter for background driver coroutines."""
    logger.warning("codex task %s failed at %s: %s", task_id, stage, detail)
    await notify_user(target, f"❌ Codex task `{task_id}` {stage}: {detail}")
