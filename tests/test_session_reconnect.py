from __future__ import annotations

from codex_websocket_v2.core.session import CodexSession
from codex_websocket_v2.core.state import TaskTarget
from codex_websocket_v2.transport.bridge import CodexBridge
from codex_websocket_v2.transport.server_manager import ServerLease


class FakeServer:
    def __init__(self) -> None:
        self.port = 31337
        self.acquire_count = 0
        self.release_count = 0

    def acquire(self):
        self.acquire_count += 1
        return {"ok": True, "port": self.port}

    def acquire_lease(self):
        acquired = self.acquire()
        if not acquired["ok"]:
            return acquired
        return {"ok": True, "lease": ServerLease(self, acquired["port"])}

    def release(self) -> None:
        self.release_count += 1


class FakeBridge(CodexBridge):
    def __init__(self, session: CodexSession, server: FakeServer) -> None:
        super().__init__(session=session, server_manager=server)
        self.connect_count = 0
        self.disconnect_count = 0
        self.closed = False

    def connect(self):
        self.connect_count += 1
        self._closed.clear()
        self.ws = object()
        self.closed = False
        return {"ok": True}

    def disconnect(self) -> None:
        self.disconnect_count += 1
        self._mark_closed("disconnect requested")
        self.ws = None
        self.closed = True

    def is_connected(self) -> bool:
        return self.ws is not None and not self._closed.is_set()

    def run_sync(self, awaitable, timeout=None):
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        return {"ok": True, "model": "gpt-test"}


class FailingBridge(FakeBridge):
    def connect(self):
        self.connect_count += 1
        return {"ok": False, "error": "connect failed"}


def make_session() -> tuple[CodexSession, FakeBridge, FakeServer]:
    server = FakeServer()
    session = CodexSession("test", TaskTarget())
    bridge = FakeBridge(session, server)
    session.bridge = bridge
    return session, bridge, server


def test_ensure_started_acquires_connects_and_syncs_config() -> None:
    session, bridge, server = make_session()

    result = session.ensure_started()

    assert result == {"ok": True}
    assert server.acquire_count == 1
    assert server.release_count == 0
    assert bridge.connect_count == 1
    assert bridge._lease is not None
    assert bridge._lease.port == server.port
    assert session.default_model == "gpt-test"


def test_ensure_started_reuses_connected_bridge_without_reacquiring() -> None:
    session, bridge, server = make_session()
    assert session.ensure_started() == {"ok": True}

    result = session.ensure_started()

    assert result == {"ok": True}
    assert server.acquire_count == 1
    assert bridge.connect_count == 1


def test_ensure_started_reconnects_disconnected_bridge_without_reacquiring() -> None:
    session, bridge, server = make_session()
    assert session.ensure_started() == {"ok": True}
    bridge.disconnect()

    result = session.ensure_started()

    assert result == {"ok": True}
    assert server.acquire_count == 1
    assert server.release_count == 0
    assert bridge.disconnect_count == 1
    assert bridge.connect_count == 2
    assert bridge.is_connected()


def test_ensure_started_reacquires_after_bridge_close() -> None:
    session, bridge, server = make_session()
    assert session.ensure_started() == {"ok": True}
    bridge.close()

    result = session.ensure_started()

    assert result == {"ok": True}
    assert server.acquire_count == 2
    assert server.release_count == 1
    assert bridge.connect_count == 2
    assert bridge.is_connected()


def test_ensure_started_releases_server_when_connect_fails() -> None:
    server = FakeServer()
    session = CodexSession("test", TaskTarget())
    bridge = FailingBridge(session, server)
    session.bridge = bridge

    result = session.ensure_started()

    assert result == {"ok": False, "error": "connect failed"}
    assert server.acquire_count == 1
    assert server.release_count == 1
    assert bridge._lease is None


def test_session_shutdown_releases_server_exactly_once() -> None:
    session, bridge, server = make_session()
    assert session.ensure_started() == {"ok": True}

    session.shutdown()
    session.shutdown()

    assert bridge.disconnect_count == 2
    assert server.release_count == 1
    assert bridge._lease is None


def test_bridge_disconnect_keeps_server_lease_and_close_releases_it() -> None:
    session, bridge, server = make_session()
    assert session.ensure_started() == {"ok": True}

    bridge.disconnect()

    assert server.release_count == 0
    assert bridge._lease is not None

    bridge.close()

    assert server.release_count == 1
    assert bridge._lease is None
