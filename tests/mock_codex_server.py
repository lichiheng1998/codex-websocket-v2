"""Minimal WebSocket server that mimics the codex app-server for testing.

Run in a daemon thread with its own asyncio event loop so tests can use
ordinary synchronous fixtures alongside the bridge's own loop thread.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Callable, Dict, List, Optional, Set


class MockCodexAppServer:
    """A stand-in for the real ``codex app-server`` WebSocket process.

    Default RPC responses (override with ``set_rpc_handler``):
      initialize   → ``{"serverInfo": {"name": "mock"}}``
      config/read  → ``{"config": {"model": "mock-model"}}``
      model/list   → ``{"data": [{id/model "mock-model", isDefault True}], nextCursor None}``
      <anything>   → ``{"ok": True}``

    Frames with a ``result`` key but no ``method`` (i.e. JSON-RPC responses
    sent by the bridge back to the server) are recorded but not replied to.
    """

    def __init__(self) -> None:
        self.port: int = 0
        self.received: List[dict] = []
        self._connections: Set[Any] = set()
        self._rpc_handlers: Dict[str, Callable[[dict], Any]] = {}
        self._response_delay: float = 0.0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Any = None
        self._ready = threading.Event()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> int:
        """Start the server in a daemon thread; return the bound port."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mock-codex-server"
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("mock server did not become ready within 5 s")
        return self.port

    def stop(self) -> None:
        """Shut down the server and join its thread."""
        if self._server is not None:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ── Push / control (thread-safe) ───────────────────────────────────────────

    def push(self, payload: dict) -> None:
        """Broadcast a JSON frame to all current connections (non-blocking)."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._push_async(payload), self._loop)

    def push_and_wait(self, payload: dict, timeout: float = 3.0) -> None:
        """Broadcast a JSON frame and block until it has been sent."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._push_async(payload), self._loop
            ).result(timeout=timeout)

    def disconnect_all(self, *, wait: bool = True, timeout: float = 3.0) -> None:
        """Force-close every active WebSocket connection."""
        if self._loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._disconnect_all_async(), self._loop
            )
            if wait:
                fut.result(timeout=timeout)

    def set_response_delay(self, seconds: float) -> None:
        """Add an artificial delay before the server replies to each RPC."""
        self._response_delay = seconds

    # ── Configuration ──────────────────────────────────────────────────────────

    def set_rpc_handler(self, method: str, fn: Callable[[dict], Any]) -> None:
        """Override the response for a specific JSON-RPC method.

        ``fn`` receives the parsed request frame and must return a
        JSON-serialisable result dict.
        """
        self._rpc_handlers[method] = fn

    def clear_received(self) -> None:
        """Reset the frame log (call between tests)."""
        self.received.clear()

    def received_methods(self) -> List[str]:
        return [m.get("method", "") for m in self.received if "method" in m]

    def received_by_method(self, method: str) -> List[dict]:
        return [m for m in self.received if m.get("method") == method]

    def received_responses(self) -> List[dict]:
        """Frames that are JSON-RPC responses (have ``result`` but no ``method``)."""
        return [m for m in self.received if "result" in m and "method" not in m]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception:
            pass

    async def _serve(self) -> None:
        import websockets

        self._server = await websockets.serve(
            self._handle_connection,
            "127.0.0.1",
            0,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        try:
            await asyncio.Future()
        except (asyncio.CancelledError, Exception):
            pass

    async def _handle_connection(self, ws) -> None:
        self._connections.add(ws)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                self.received.append(msg)

                rpc_id = msg.get("id")
                method = msg.get("method")
                # Only requests (have both method and id) get a response.
                # Plain responses sent by the bridge (have result but no method)
                # are recorded silently.
                if method is not None and rpc_id is not None:
                    if self._response_delay > 0:
                        await asyncio.sleep(self._response_delay)
                    result = self._dispatch_rpc(method, msg)
                    response = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
                    await ws.send(json.dumps(response))
        except Exception:
            pass
        finally:
            self._connections.discard(ws)

    def _dispatch_rpc(self, method: str, msg: dict) -> Any:
        if method in self._rpc_handlers:
            return self._rpc_handlers[method](msg)
        if method == "config/read":
            return {"config": {"model": "mock-model"}}
        if method == "model/list":
            return {
                "data": [
                    {
                        "id": "mock-model",
                        "model": "mock-model",
                        "displayName": "Mock Model",
                        "isDefault": True,
                    }
                ],
                "nextCursor": None,
            }
        return {"ok": True}

    async def _push_async(self, payload: dict) -> None:
        raw = json.dumps(payload)
        for ws in list(self._connections):
            try:
                await ws.send(raw)
            except Exception:
                pass

    async def _disconnect_all_async(self) -> None:
        for ws in list(self._connections):
            try:
                await ws.close()
            except Exception:
                pass
