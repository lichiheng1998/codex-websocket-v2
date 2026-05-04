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
the hermes tool/gateway loop — awaiting their ``send`` from the bridge loop
raises ``Timeout context manager should be used inside a task``. To fix this,
the plugin captures the loop before codex tools execute; ``notify_user`` then
schedules sends via ``run_coroutine_threadsafe`` when the calling loop differs.

``report_failure`` is the bridge's standard "task failed at stage X with
detail Y" helper — kept here because its only side effect is one
notify call.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Optional

from ..core.state import TaskTarget

logger = logging.getLogger(__name__)

# Captured before codex tools execute when hermes is running async (gateway mode).
# Used to route platform sends to the loop the live adapters are bound to.
# Stays None in CLI mode — there's no separate "main loop" to bridge to.
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _debug_stack(limit: int = 8) -> str:
    frames = inspect.stack()[2 : 2 + limit]
    parts = []
    for frame in frames:
        parts.append(f"{frame.function}@{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}")
    return " <- ".join(parts)


def set_main_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Record the hermes tool/gateway event loop."""
    global _MAIN_LOOP
    if loop is None:
        return
    _MAIN_LOOP = loop


def capture_current_loop(reason: str = "") -> None:
    """Capture the current event loop before codex enters its bridge loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _MAIN_LOOP is not None:
        return
    set_main_loop(loop)
    logger.info(
        "codex notify: captured tool loop reason=%s loop_id=%s running=%s",
        reason,
        id(loop),
        loop.is_running(),
    )


async def _send_via_main_loop(coro_factory) -> None:
    """Schedule ``coro_factory()`` on the hermes main loop and await its result."""
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None

    if _MAIN_LOOP is not None and current is not _MAIN_LOOP and _MAIN_LOOP.is_running():
        logger.info("codex notify: dispatching to main loop via run_coroutine_threadsafe")
        future = asyncio.run_coroutine_threadsafe(coro_factory(), _MAIN_LOOP)
        return await asyncio.wrap_future(future)

    logger.info(
        "codex notify: direct send "
        "(main_loop_set=%s, on_main_loop=%s, main_loop_running=%s)",
        _MAIN_LOOP is not None,
        current is _MAIN_LOOP if _MAIN_LOOP is not None else None,
        _MAIN_LOOP.is_running() if _MAIN_LOOP is not None else None,
    )
    return await coro_factory()


async def _send_telegram_direct(pconfig, chat_id: str, message: str, thread_id: Optional[str]) -> None:
    """Send Telegram notifications without live adapter or _send_telegram HTML detection."""
    try:
        from gateway.platforms.base import utf16_len
        from gateway.platforms.telegram import TelegramAdapter, _strip_mdv2
        from telegram import Bot
        from telegram.constants import ParseMode

        token = getattr(pconfig, "token", None)
        if not token:
            logger.warning("codex notify: Telegram token missing")
            return

        adapter = TelegramAdapter.__new__(TelegramAdapter)
        formatted = adapter.format_message(message)
        chunks = adapter.truncate_message(
            formatted,
            getattr(TelegramAdapter, "MAX_MESSAGE_LENGTH", 4096),
            len_fn=utf16_len,
        )
        if len(chunks) > 1:
            chunks = [
                re.sub(r" \((\d+)/(\d+)\)$", r" \\(\1/\2\\)", chunk)
                for chunk in chunks
            ]

        bot = Bot(token=token)
        effective_thread_id = int(thread_id) if thread_id else None
        for chunk in chunks:
            try:
                await bot.send_message(
                    chat_id=int(chat_id),
                    text=chunk,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    message_thread_id=effective_thread_id,
                )
            except Exception as md_error:
                if "parse" not in str(md_error).lower() and "markdown" not in str(md_error).lower():
                    raise
                logger.warning(
                    "codex notify: Telegram MarkdownV2 parse failed, falling back to plain text: %s",
                    md_error,
                )
                await bot.send_message(
                    chat_id=int(chat_id),
                    text=_strip_mdv2(chunk),
                    parse_mode=None,
                    message_thread_id=effective_thread_id,
                )
    except Exception as exc:
        logger.warning("codex notify: direct Telegram send failed: %s", exc)


async def _deliver_notify_on_gateway(target: TaskTarget, message: str) -> None:
    try:
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

        thread_id = target.thread_id or None
        if platform == Platform.TELEGRAM:
            await _send_telegram_direct(pconfig, target.chat_id, message, thread_id)
        else:
            await _send_to_platform(
                platform, pconfig, target.chat_id, message, thread_id=thread_id,
            )

        try:
            from gateway.mirror import mirror_to_session
            mirror_to_session(
                platform=target.platform,
                chat_id=str(target.chat_id),
                message_text=message,
                source_label="codex",
                thread_id=thread_id,
            )
        except Exception as mirror_exc:
            logger.debug("codex notify: mirror skipped: %s", mirror_exc)
    except Exception as exc:
        logger.warning("codex notify failed: %s", exc)


async def notify_user(target: Optional[TaskTarget], message: str) -> None:
    """Best-effort push to the chat platform identified by ``target``.

    Returns silently on every failure path; callers are background driver
    coroutines that have nowhere to surface the error to anyway.
    """
    if target is None or not target.platform or not target.chat_id:
        logger.info("codex notify (no target): %s", message[:200])
        return
    try:
        logger.warning(
            "codex notify debug: enter platform=%r chat_id=%r thread_id=%r "
            "message_id=%s len=%s preview=%r stack=%s",
            target.platform,
            target.chat_id,
            target.thread_id,
            id(message),
            len(message) if message is not None else None,
            (message or "")[:200],
            _debug_stack(),
        )
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
        if platform == Platform.TELEGRAM:
            await _send_via_main_loop(
                lambda: _send_telegram_direct(pconfig, chat_id, message, thread_id)
            )
        else:
            await _send_via_main_loop(
                lambda: _send_to_platform(
                    platform, pconfig, chat_id, message, thread_id=thread_id,
                )
            )

        # Mirror the same text into the hermes session transcript so the
        # agent's conversation log reflects what was pushed to the platform.
        try:
            from gateway.mirror import mirror_to_session
            mirror_to_session(
                platform=target.platform,
                chat_id=str(target.chat_id),
                message_text=message,
                source_label="codex",
                thread_id=thread_id,
            )
        except Exception as mirror_exc:
            logger.debug("codex notify: mirror skipped: %s", mirror_exc)
    except Exception as exc:
        logger.warning("codex notify failed: %s", exc)


async def report_failure(
    target: Optional[TaskTarget], task_id: str, stage: str, detail: str,
) -> None:
    """Fire-and-forget error reporter for background driver coroutines."""
    logger.warning("codex task %s failed at %s: %s", task_id, stage, detail)
    await notify_user(target, f"❌ Codex task `{task_id}` {stage}: {detail}")
