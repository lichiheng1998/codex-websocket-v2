"""Global serial action event bus.

All tool calls across all sessions go through a single queue.  The bus owns
a dedicated loop thread; consumers dispatch each event to its session's
``EventBus`` where typed subscribers handle the actual work.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_LOOP_READY_TIMEOUT = 5.0


class ActionEventBus:
    """Single queue + consumer shared by all sessions."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._consumer: Optional[asyncio.Task] = None
        self._started = threading.Event()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the loop thread and consumer.  Idempotent."""
        if self._started.is_set():
            return
        loop_ready = threading.Event()

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._consumer = self._loop.create_task(
                self._consume_loop(),
                name="action-bus-consumer",
            )
            loop_ready.set()
            self._loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_run, name="action-bus-loop", daemon=True,
        )
        self._loop_thread.start()
        if not loop_ready.wait(timeout=_LOOP_READY_TIMEOUT):
            raise RuntimeError("action bus loop failed to start")
        self._started.set()

    # ── Submit ───────────────────────────────────────────────────────────

    def submit(self, event) -> None:
        """Submit from any thread (sync-safe)."""
        if not self._started.is_set():
            self.start()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    # ── Consumer ─────────────────────────────────────────────────────────

    async def _consume_loop(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            try:
                handled = await event.session.event_bus.publish(event)
                if not handled:
                    logger.warning(
                        "action bus: no subscriber handled %r",
                        type(event).__name__,
                    )
                    if not event.result_future.done():
                        event.result_future.set_result(
                            {"ok": False, "error": f"unhandled action: {type(event).__name__}"}
                        )
            except Exception as exc:
                logger.exception("action bus: publish failed: %s", exc)
                if not event.result_future.done():
                    event.result_future.set_exception(exc)

    # ── Shutdown ─────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Signal the consumer to stop and tear down the loop."""
        if self._consumer is not None:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            try:
                await asyncio.wait_for(self._consumer, timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                self._consumer.cancel()
            self._consumer = None
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None
        self._started.clear()


# ── Module-level singleton ───────────────────────────────────────────────

_bus: Optional[ActionEventBus] = None


def get_action_bus() -> ActionEventBus:
    global _bus
    if _bus is None:
        _bus = ActionEventBus()
    return _bus
