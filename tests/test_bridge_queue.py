from __future__ import annotations

import asyncio

from codex_websocket_v2.core.session import CodexSession
from codex_websocket_v2.core.state import TaskTarget
from codex_websocket_v2.transport.bridge import CodexBridge


class QueueBridge(CodexBridge):
    def __init__(self, session: CodexSession) -> None:
        super().__init__(session=session)
        self.dispatches: list[dict] = []

    def run_sync(self, awaitable, timeout=None):
        try:
            asyncio.run(awaitable)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}


class QueueSession(CodexSession):
    def __init__(self) -> None:
        super().__init__("test", TaskTarget())
        self.bridge = QueueBridge(self)


async def _wait_for(cond, *, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


class Handler:
    def __init__(self, bridge: QueueBridge, delay: float = 0.0) -> None:
        self.bridge = bridge
        self.delay = delay

    async def dispatch(self, msg):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.bridge.dispatches.append(msg)


def test_reader_queues_events_in_order() -> None:
    session = QueueSession()
    bridge = session.bridge
    bridge._handler = Handler(bridge)

    async def main():
        bridge._event_queue = asyncio.Queue(maxsize=4)
        consumer = asyncio.create_task(bridge._consumer_loop())
        await bridge._event_queue.put({"id": 1})
        await bridge._event_queue.put({"id": 2})
        await bridge._event_queue.put(None)
        await consumer

    asyncio.run(main())
    assert bridge.dispatches == [{"id": 1}, {"id": 2}]


def test_reader_blocks_when_queue_full() -> None:
    session = QueueSession()
    bridge = session.bridge
    bridge._handler = Handler(bridge, delay=0.05)

    async def main():
        bridge._event_queue = asyncio.Queue(maxsize=1)
        await bridge._event_queue.put({"id": 1})
        put_task = asyncio.create_task(bridge._event_queue.put({"id": 2}))
        await asyncio.sleep(0.01)
        assert not put_task.done()
        consumer = asyncio.create_task(bridge._consumer_loop())
        await _wait_for(lambda: put_task.done())
        await bridge._event_queue.put(None)
        await consumer

    asyncio.run(main())
    assert bridge.dispatches == [{"id": 1}, {"id": 2}]
