"""Server lease + WebSocket transport for a CodexSession.

Each ``CodexSession`` owns one long-lived ``CodexBridge``. The bridge is
responsible for:
  * acquiring/releasing this session's app-server lease
  * starting/stopping a private asyncio loop on a dedicated thread
  * holding a single WebSocket connection to the app-server
  * pairing JSON-RPC requests with their responses (``_pending_rpc``)
  * routing inbound frames to ``MessageHandler.dispatch``

All business logic (task lookups, notification formatting, approval
stashing) lives on ``CodexSession`` — the bridge does not touch task state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from pydantic import BaseModel

from . import wire
from ..core.policies import LOOP_READY_TIMEOUT, SHUTDOWN_TIMEOUT
from .server_manager import CodexServerManager, ServerLease
from ..core.state import Result, err, ok

if TYPE_CHECKING:
    from ..core.session import CodexSession
    from .server_manager import CodexServerManager as ServerManager

logger = logging.getLogger(__name__)


class CodexBridge:
    def __init__(
        self,
        session: "CodexSession",
        server_manager: "ServerManager | None" = None,
    ) -> None:
        self.session = session
        self._server = server_manager or CodexServerManager.instance()
        self._lease: Optional[ServerLease] = None
        self.ws = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_thread: Optional[threading.Thread] = None

        self._next_id = 1
        self._id_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._pending_rpc: Dict[int, asyncio.Future] = {}
        self._handler = None  # set in connect()
        self._closed = threading.Event()
        self._closed_reason = ""

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def ensure_connected(self) -> Result:
        """Ensure this bridge owns a server lease and has an open WebSocket."""
        if self.is_connected():
            return ok(connected=False)
        with self._connect_lock:
            if self.is_connected():
                return ok(connected=False)

            if self._lease is None:
                acquired = self._server.acquire_lease()
                if not acquired["ok"]:
                    return acquired
                self._lease = acquired["lease"]

            if self.ws is not None or self.loop is not None:
                logger.warning(
                    "codex bridge disconnected; reconnecting session %s",
                    self.session.session_key,
                )
                self.disconnect()

            connected = self.connect()
            if connected["ok"]:
                return ok(connected=True)

            self._release_lease()
            return connected

    def connect(self) -> Result:
        """Start loop thread, open WS, run initialize handshake.

        Returns a Result; on failure leaves the bridge in a clean state
        (no leaked WS, loop thread still running so retries are cheap).
        """
        if self._lease is None:
            return err("codex bridge has no app-server port")
        loop_result = self._start_loop_thread()
        if not loop_result["ok"]:
            return loop_result

        return self.run_sync(self._connect_and_initialize(), timeout=LOOP_READY_TIMEOUT * 4)

    def disconnect(self) -> None:
        """Close WS, stop loop. Does not touch CodexServerManager."""
        self._mark_closed("disconnect requested")
        self._fail_pending_rpcs("websocket disconnected")
        if self.loop and self.loop.is_running():
            self.run_sync(self._close_ws(), timeout=SHUTDOWN_TIMEOUT)
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.loop_thread is not None:
            self.loop_thread.join(timeout=SHUTDOWN_TIMEOUT)
        self.ws = None
        self.loop = None
        self.loop_thread = None
        self._handler = None

    def close(self) -> None:
        """Close WS resources and release this bridge's app-server lease."""
        self.disconnect()
        self._release_lease()

    # ── Public RPC API (called by CodexSession) ──────────────────────────────

    def run_sync(self, coro, timeout: float = 12.0) -> Result:
        """Schedule ``coro`` on the bridge loop from a sync caller."""
        if self.loop is None:
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            return err("bridge event loop is not running")
        try:
            value = asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)
        except Exception as exc:
            return err(f"bridge loop call failed: {exc}")
        if isinstance(value, dict) and "ok" in value:
            return value
        return ok(result=value)

    async def rpc(
        self,
        method: str,
        params: Union[BaseModel, dict, None] = None,
        timeout: float = 30.0,
    ) -> Result:
        rpc_id = self._next_rpc_id()
        fut: asyncio.Future = self.loop.create_future()
        self._pending_rpc[rpc_id] = fut
        payload = json.dumps({
            "jsonrpc": "2.0", "id": rpc_id,
            "method": method, "params": wire.serialize(params),
        })
        try:
            await self.ws.send(payload)
        except Exception as exc:
            if _is_websocket_closed(exc):
                self._mark_closed(str(exc))
                return err(f"{method} send failed: websocket closed: {exc}")
            return err(f"{method} send failed: {exc}")

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            return ok(result=result)
        except asyncio.TimeoutError:
            return err(f"{method}: timeout after {timeout}s")
        except Exception as exc:
            if _is_websocket_closed(exc):
                self._mark_closed(str(exc))
                return err(f"{method} response failed: websocket closed: {exc}")
            return err(f"{method} response failed: {exc}")
        finally:
            self._pending_rpc.pop(rpc_id, None)

    async def ws_send(self, payload: str) -> Result:
        try:
            await self.ws.send(payload)
            return ok()
        except Exception as exc:
            if _is_websocket_closed(exc):
                self._mark_closed(str(exc))
                return err(f"ws send failed: websocket closed: {exc}")
            return err(f"ws send failed: {exc}")

    # ── Internal ─────────────────────────────────────────────────────────────

    def _next_rpc_id(self) -> int:
        with self._id_lock:
            rpc_id = self._next_id
            self._next_id += 1
            return rpc_id

    def _start_loop_thread(self) -> Result:
        if self.loop is not None:
            return ok()
        loop_ready = threading.Event()

        def _run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            loop_ready.set()
            self.loop.run_forever()

        self.loop_thread = threading.Thread(
            target=_run, name=f"codex-ws-{self.session.session_key}", daemon=True,
        )
        self.loop_thread.start()
        if not loop_ready.wait(timeout=LOOP_READY_TIMEOUT):
            self.loop = None
            self.loop_thread = None
            return err("bridge event loop failed to start within timeout")
        return ok()

    async def _connect_and_initialize(self) -> None:
        import websockets
        from .handlers import MessageHandler

        url = f"ws://127.0.0.1:{self._lease.port}"
        self.ws = await websockets.connect(url, max_size=None, ping_interval=20)
        self._closed.clear()
        self._closed_reason = ""
        self._handler = MessageHandler(self.session)
        try:
            asyncio.create_task(self._reader_loop())
            init = await self.rpc(
                "initialize",
                wire.InitializeParams(
                    clientInfo={"name": "hermes-codex-ws-bridge-v2", "version": "0.1"},
                    capabilities=wire.InitializeCapabilities(experimentalApi=True),
                ),
            )
            if not init["ok"]:
                raise RuntimeError(f"initialize failed: {init['error']}")
            notified = await self.ws_send(json.dumps({"jsonrpc": "2.0", "method": "initialized"}))
            if not notified["ok"]:
                raise RuntimeError(f"initialized notification failed: {notified['error']}")
        except Exception:
            await self._close_ws()
            self.ws = None
            raise

    async def _close_ws(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def _reader_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("codex bridge: non-JSON frame dropped")
                    continue
                logger.debug("codex ws ← %s", json.dumps(msg, ensure_ascii=False)[:500])
                try:
                    await self._handler.dispatch(msg)
                except Exception as exc:
                    logger.exception("codex handler failed on frame: %s", exc)
        except Exception as exc:
            if _is_websocket_closed(exc):
                logger.warning("codex bridge reader exited: websocket closed: %s", exc)
                self._mark_closed(str(exc))
                self._fail_pending_rpcs(f"websocket closed: {exc}")
                return
            logger.warning("codex bridge reader exited: %s", exc)
            self._mark_closed(str(exc))
            self._fail_pending_rpcs(f"websocket reader exited: {exc}")
        else:
            self._mark_closed("websocket reader ended")
            self._fail_pending_rpcs("websocket reader ended")

    def _mark_closed(self, reason: str) -> None:
        self._closed_reason = reason
        self._closed.set()

    def _fail_pending_rpcs(self, reason: str) -> None:
        for fut in list(self._pending_rpc.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(reason))

    def _release_lease(self) -> None:
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    # ── Status helpers (used by CodexSession.get_status) ─────────────────────

    def is_connected(self) -> bool:
        if self._closed.is_set():
            return False
        try:
            from websockets.protocol import State as WsState
            return self.ws is not None and self.ws.state == WsState.OPEN
        except Exception:
            return False


def _is_websocket_closed(exc: BaseException) -> bool:
    try:
        from websockets.exceptions import ConnectionClosed

        return isinstance(exc, ConnectionClosed)
    except Exception:
        return (
            exc.__class__.__name__.startswith("ConnectionClosed")
            and "websockets" in exc.__class__.__module__
        )
