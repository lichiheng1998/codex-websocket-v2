"""Queue-based action event bus with serial consumer.

Action events (from tools.py) are submitted via ``submit()`` from any thread.
A consumer coroutine on the bridge loop drains the queue and publishes each
event on the session's EventBus, where typed subscribers handle them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .bus import EventBus

logger = logging.getLogger(__name__)


class ActionEventBus:
    """Async queue + consumer that dispatches action events via EventBus."""

    def __init__(self, event_bus: "EventBus") -> None:
        self._event_bus = event_bus
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._consumer: Optional[asyncio.Task] = None

    async def start_consumer(self) -> None:
        """Start the drain loop. Call from the bridge loop after it's running."""
        if self._consumer is not None and not self._consumer.done():
            return
        self._consumer = asyncio.create_task(
            self._consume_loop(),
            name="action-bus-consumer",
        )

    async def _consume_loop(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            try:
                handled = await self._event_bus.publish(event)
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

    def submit(self, event) -> None:
        """Submit an action event from any thread (sync-safe).

        Uses ``call_soon_threadsafe`` to safely enqueue from the tools.py
        sync thread onto the bridge loop's queue.
        """
        loop = event.session.bridge.loop
        if loop is None or not loop.is_running():
            logger.error("action bus: bridge loop not running; cannot submit")
            if not event.result_future.done():
                event.result_future.set_result(
                    {"ok": False, "error": "bridge event loop is not running"}
                )
            return
        loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def shutdown(self) -> None:
        """Signal the consumer to stop and wait for it."""
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
