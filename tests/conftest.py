"""Shared pytest fixtures for codex-websocket-v2 tests."""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests.mock_codex_server import MockCodexAppServer
from codex_websocket_v2.core.session import CodexSession
from codex_websocket_v2.core.state import TaskTarget
from codex_websocket_v2.transport.bridge import CodexBridge
from codex_websocket_v2.transport.server_manager import ServerLease


class PortOnlyServer:
    """Drop-in for CodexServerManager that returns a fixed port without spawning a process.

    Used to connect a real ``CodexBridge`` to a ``MockCodexAppServer``.
    """

    def __init__(self, port: int) -> None:
        self.port = port

    def acquire(self):
        return {"ok": True, "port": self.port}

    def acquire_lease(self):
        return {"ok": True, "lease": ServerLease(self, self.port)}

    def release(self) -> None:
        pass


def make_connected_session(
    mock_server: MockCodexAppServer,
    session_key: str = "test-e2e",
) -> CodexSession:
    """Create a CodexSession with a live bridge connected to ``mock_server``."""
    port_server = PortOnlyServer(port=mock_server.port)
    session = CodexSession(session_key, TaskTarget())
    session.bridge = CodexBridge(session=session, server_manager=port_server)
    result = session.ensure_started()
    assert result.get("ok"), f"ensure_started failed: {result}"
    return session


@pytest.fixture(scope="module")
def mock_server():
    """One MockCodexAppServer per test module (avoids repeated port allocation)."""
    server = MockCodexAppServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def connected_session(mock_server):
    """A fresh CodexSession connected to the module-scoped mock server.

    Resets the server's frame log before each test and shuts the session
    down in teardown.
    """
    mock_server.clear_received()
    mock_server.set_response_delay(0.0)
    session = make_connected_session(mock_server)
    yield session
    try:
        session.shutdown()
    except Exception:
        pass
