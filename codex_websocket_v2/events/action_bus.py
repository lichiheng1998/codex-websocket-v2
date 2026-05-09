"""Action event bus — serialises all outbound tool calls onto the bridge loop.

``ActionEventBus`` wraps an ``asyncio.Queue`` whose consumer runs as a
single coroutine on the bridge event loop. Every tool call in ``tools.py``
submits one ``BaseActionEvent`` via ``submit()`` (thread-safe), then blocks
on ``event.result_future.result(timeout)`` until the consumer sets the
result.

Because the consumer is a single coroutine, all mutations to
``CodexSession.tasks`` happen in a strictly serial order — this eliminates
the TOCTOU race present when two concurrent tool calls both tried to
revive/remove the same task.
"""

from __future__ import annotations

import asyncio
import logging

from .bus import EventBus
from .action_models import BaseActionEvent

logger = logging.getLogger(__name__)


class ActionEventBus:
    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._queue: asyncio.Queue[BaseActionEvent | None] = asyncio.Queue(maxsize=256)
        self._consumer: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start_consumer(self) -> None:
        """Start the consumer task on the current (bridge) event loop."""
        if self._consumer is not None and not self._consumer.done():
            return
        self._consumer = asyncio.create_task(
            self._consume_loop(),
            name="codex-action-consumer",
        )

    async def shutdown(self) -> None:
        """Signal the consumer to stop and wait for it to finish."""
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._consumer is not None and not self._consumer.done():
            try:
                await asyncio.wait_for(self._consumer, timeout=5.0)
            except Exception:
                pass

    # ── Submit (called from any thread) ───────────────────────────────────────

    def submit(self, event: BaseActionEvent) -> None:
        """Enqueue an action event from any thread (thread-safe).

        Uses ``call_soon_threadsafe`` so the queue operation runs on the
        bridge loop, which owns the queue.
        """
        loop = event.session.bridge.loop
        loop.call_soon_threadsafe(self._enqueue, event)

    def _enqueue(self, event: BaseActionEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            event.result_future.set_exception(
                RuntimeError("action bus queue full; try again later")
            )

    # ── Consumer loop (bridge loop) ───────────────────────────────────────────

    async def _consume_loop(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            try:
                await self._event_bus.publish(event)
            except Exception as exc:
                logger.exception("action bus: unhandled error dispatching %s", type(event).__name__)
                if not event.result_future.done():
                    event.result_future.set_exception(exc)
