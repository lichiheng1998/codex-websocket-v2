"""Pure connection layer: WebSocket + event-loop thread + RPC plumbing.

Each ``CodexSession`` owns one ``CodexBridge``. The bridge is responsible
only for:
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
from ..core.state import Result, err, ok

if TYPE_CHECKING:
    from ..core.session import CodexSession

logger = logging.getLogger(__name__)


class CodexBridge:
    def __init__(self, port: int, session: "CodexSession") -> None:
        self.port = port
        self.session = session
        self.ws = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_thread: Optional[threading.Thread] = None

        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending_rpc: Dict[int, asyncio.Future] = {}
        self._handler = None  # set in connect()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def connect(self) -> Result:
        """Start loop thread, open WS, run initialize handshake.

        Returns a Result; on failure leaves the bridge in a clean state
        (no leaked WS, loop thread still running so retries are cheap).
        """
        loop_result = self._start_loop_thread()
        if not loop_result["ok"]:
            return loop_result

        return self.run_sync(self._connect_and_initialize(), timeout=LOOP_READY_TIMEOUT * 4)

    def disconnect(self) -> None:
        """Close WS, stop loop. Does not touch CodexServerManager."""
        if self.loop and self.loop.is_running():
            self.run_sync(self._close_ws(), timeout=SHUTDOWN_TIMEOUT)
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.loop_thread is not None:
            self.loop_thread.join(timeout=SHUTDOWN_TIMEOUT)

    # ── Public RPC API (called by CodexSession) ──────────────────────────────

    def run_sync(self, coro, timeout: float = 12.0) -> Result:
        """Schedule ``coro`` on the bridge loop from a sync caller."""
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
        try:
            await self.ws.send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id,
                "method": method, "params": wire.serialize(params),
            }))
            result = await asyncio.wait_for(fut, timeout=timeout)
            return ok(result=result)
        except asyncio.TimeoutError:
            return err(f"{method}: timeout after {timeout}s")
        except Exception as exc:
            return err(f"{method}: {exc}")
        finally:
            self._pending_rpc.pop(rpc_id, None)

    async def ws_send(self, payload: str) -> Result:
        try:
            await self.ws.send(payload)
            return ok()
        except Exception as exc:
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
            return err("bridge event loop failed to start within timeout")
        return ok()

    async def _connect_and_initialize(self) -> None:
        import websockets
        from .handlers import MessageHandler

        url = f"ws://127.0.0.1:{self.port}"
        self.ws = await websockets.connect(url, max_size=None, ping_interval=20)
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
            logger.warning("codex bridge reader exited: %s", exc)
            for fut in list(self._pending_rpc.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError(f"websocket closed: {exc}"))

    # ── Status helpers (used by CodexSession.get_status) ─────────────────────

    def is_connected(self) -> bool:
        try:
            from websockets.protocol import State as WsState
            return self.ws is not None and self.ws.state == WsState.OPEN
        except Exception:
            return False
