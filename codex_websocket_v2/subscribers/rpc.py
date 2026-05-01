"""Subscribers for JSON-RPC responses from app-server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..events import RpcErrorEvent, RpcResponseEvent

if TYPE_CHECKING:
    from ..session import CodexSession


class RpcResponseSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    async def __call__(self, event: RpcResponseEvent) -> bool:
        fut = self._pending_future(event.rpc_id)
        if fut is not None and not fut.done():
            fut.set_result(event.result)
        return True

    def _pending_future(self, rpc_id: Any):
        pending = self.session.bridge._pending_rpc
        fut = pending.get(rpc_id)
        if fut is None:
            if isinstance(rpc_id, str):
                try:
                    fut = pending.get(int(rpc_id))
                except ValueError:
                    pass
            elif isinstance(rpc_id, int):
                fut = pending.get(str(rpc_id))
        return fut


class RpcErrorSubscriber(RpcResponseSubscriber):
    async def __call__(self, event: RpcErrorEvent) -> bool:
        fut = self._pending_future(event.rpc_id)
        if fut is not None and not fut.done():
            fut.set_exception(RuntimeError(f"{event.error.code}: {event.error.message}"))
        return True
