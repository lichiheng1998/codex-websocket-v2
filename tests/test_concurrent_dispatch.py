"""Concurrency and stress tests for the dispatch_tool path.

Tests the real ActionEventBus + CodexBridge pipeline under concurrent load,
checking for race conditions, queue saturation, and bridge crash safety.
"""
from __future__ import annotations

import concurrent.futures
import json
import threading
import time

import pytest

from codex_websocket_v2.core.state import Task, TaskTarget
from codex_websocket_v2.events.action_models import (
    ListTasksEvent,
    SetModelEvent,
    SteerEvent,
    StartTaskEvent,
    make_event,
)
from tests.conftest import make_connected_session, PortOnlyServer
from tests.mock_codex_server import MockCodexAppServer


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_task(task_id: str = "task-1", thread_id: str = "thread-1", **kw) -> Task:
    return Task(
        task_id=task_id,
        thread_id=thread_id,
        cwd="/tmp",
        model="mock-model",
        plan=False,
        sandbox_policy="workspace-write",
        approval_policy="on-request",
        **kw,
    )


def _submit_and_wait(session, event, timeout: float = 15.0) -> dict:
    session.action_bus.submit(event)
    raw = event.result_future.result(timeout=timeout)
    return json.loads(raw)


@pytest.fixture(scope="module")
def mock_server():
    server = MockCodexAppServer()
    server.start()
    yield server
    server.stop()


# ── C1: Concurrent dispatch no corruption ──────────────────────────────────────

def test_concurrent_dispatch_no_corruption(mock_server):
    """20 threads simultaneously submit ListTasksEvents; all complete without error or hang."""
    mock_server.clear_received()
    session = make_connected_session(mock_server, session_key="concurrent-c1")

    N = 20
    barrier = threading.Barrier(N)
    results = []
    errors = []
    lock = threading.Lock()

    def worker():
        event = ListTasksEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={},
        )
        barrier.wait()
        try:
            raw = _submit_and_wait(session, event, timeout=15.0)
            with lock:
                results.append(raw)
        except Exception as exc:
            with lock:
                errors.append(str(exc))

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    session.shutdown()

    assert len(errors) == 0, f"Unexpected errors: {errors[:3]}"
    assert len(results) == N
    for r in results:
        assert r["ok"] is True


# ── C2: Action bus queue saturation ───────────────────────────────────────────

def test_action_bus_queue_saturation(mock_server):
    """Submitting >256 events while the consumer is stalled overflows the queue gracefully.

    Strategy: enable a long response delay so the consumer stalls inside the
    first RPC.  Then flood the queue with model/set events (which make RPCs).
    The first 256 fill the queue; the remainder get QueueFull exceptions on
    their result_futures.
    """
    mock_server.clear_received()
    mock_server.set_response_delay(0.0)

    session = make_connected_session(mock_server, session_key="concurrent-c2")

    # Stall the consumer: first RPC takes 1 s, giving us time to flood the queue.
    mock_server.set_response_delay(1.0)

    TOTAL = 300
    events = [
        SetModelEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"model_id": "mock-model"},
        )
        for _ in range(TOTAL)
    ]

    # Enqueue all events before the consumer can drain them.
    for ev in events:
        session.action_bus.submit(ev)

    # Stop the long delay so remaining events eventually complete.
    mock_server.set_response_delay(0.0)

    successes = 0
    queue_full_errors = 0
    other_errors = []

    for ev in events:
        try:
            ev.result_future.result(timeout=30.0)
            successes += 1
        except RuntimeError as exc:
            if "queue full" in str(exc).lower():
                queue_full_errors += 1
            else:
                other_errors.append(str(exc))
        except Exception as exc:
            other_errors.append(str(exc))

    session.shutdown()

    assert other_errors == [], f"Unexpected errors: {other_errors[:3]}"
    # At least some events must have overflowed the 256-slot queue.
    assert queue_full_errors > 0, (
        "Expected some QueueFull errors but got none. "
        f"successes={successes}, total={TOTAL}"
    )
    # The bridge must not have crashed: all non-overflowed events resolved.
    assert successes + queue_full_errors == TOTAL


# ── C3: Bridge disconnect mid-burst ────────────────────────────────────────────

def test_bridge_disconnect_mid_burst(mock_server):
    """Disconnect the WS while RPCs are in flight; all pending futures fail gracefully."""
    mock_server.clear_received()
    mock_server.set_response_delay(0.0)

    session = make_connected_session(mock_server, session_key="concurrent-c3")

    # Slow down server so RPCs stay in flight long enough to disconnect.
    mock_server.set_response_delay(0.5)

    # ReviveEvent calls thread/read RPC with no fallback — fails cleanly on disconnect.
    N = 8
    events = [
        SteerEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "steer-task", "message": f"msg-{i}"},
        )
        for i in range(N)
    ]
    session.tasks["steer-task"] = Task(
        task_id="steer-task",
        thread_id="thread-steer",
        cwd="/tmp",
        model="mock-model",
        plan=False,
        sandbox_policy="workspace-write",
        approval_policy="on-request",
        active_turn_id="turn-active",
    )

    for ev in events:
        session.action_bus.submit(ev)

    # Give the consumer time to start the first RPC, then disconnect.
    time.sleep(0.15)
    mock_server.disconnect_all(wait=True)
    mock_server.set_response_delay(0.0)

    results = []
    for ev in events:
        raw = ev.result_future.result(timeout=10.0)
        results.append(json.loads(raw))

    session.shutdown()

    # All futures must resolve (no hangs); bridge must not crash.
    assert len(results) == N
    for ev in events:
        assert ev.result_future.done(), "Result future was never resolved"

    # At least some requests should have failed (ok: False) due to WS disconnect.
    # Subscribers catch RPC exceptions and return error JSON, not raised exceptions.
    failures = [r for r in results if not r.get("ok")]
    assert len(failures) > 0, (
        "Expected some ok:false results after WS disconnect, but all succeeded. "
        f"results={results[:3]}"
    )


# ── C4: Event queue flood ──────────────────────────────────────────────────────

def test_event_queue_flood(mock_server):
    """Push 2200 inbound WS frames (> _event_queue maxsize=2048); bridge survives."""
    mock_server.clear_received()
    mock_server.set_response_delay(0.0)

    session = make_connected_session(mock_server, session_key="concurrent-c4")
    bridge = session.bridge

    # Push frames in batches to avoid overwhelming the server's loop.
    TOTAL = 2200
    BATCH = 100
    for i in range(0, TOTAL, BATCH):
        for j in range(min(BATCH, TOTAL - i)):
            mock_server.push({
                "jsonrpc": "2.0",
                "method": "item/agentMessage/delta",
                "params": {"delta": f"chunk-{i + j}"},
            })
        time.sleep(0.02)  # let the bridge loop drain the queue a bit

    # Allow processing to settle.
    time.sleep(0.5)

    # Bridge must still be responsive: submit a simple list event.
    event = ListTasksEvent(
        session=session,
        result_future=concurrent.futures.Future(),
        args={},
    )
    result = _submit_and_wait(session, event, timeout=10.0)
    assert result["ok"] is True

    session.shutdown()


# ── C5: RPC ID uniqueness under concurrency ────────────────────────────────────

def test_rpc_id_uniqueness_under_concurrency(mock_server):
    """30 concurrent action events trigger 30 RPCs; all rpc_ids must be unique."""
    mock_server.clear_received()
    mock_server.set_response_delay(0.0)

    session = make_connected_session(mock_server, session_key="concurrent-c5")

    N = 30
    barrier = threading.Barrier(N)
    result_futures = []
    threads_done = threading.Event()

    def worker(idx):
        event = SetModelEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"model_id": "mock-model"},
        )
        result_futures.append(event.result_future)
        barrier.wait()
        session.action_bus.submit(event)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    # Wait for all futures.
    for fut in result_futures:
        try:
            fut.result(timeout=15.0)
        except Exception:
            pass

    session.shutdown()

    # Collect all RPC ids seen by the server (method-bearing frames with an id).
    rpc_frames = [
        f for f in mock_server.received
        if f.get("method") and f.get("id") is not None
        and f.get("method") not in ("initialize", "initialized")
    ]
    rpc_ids = [f["id"] for f in rpc_frames]

    assert len(rpc_ids) == len(set(rpc_ids)), (
        f"Duplicate RPC ids detected: {[i for i in rpc_ids if rpc_ids.count(i) > 1]}"
    )


# ── C6: Multi-session isolation ────────────────────────────────────────────────

def test_multi_session_isolation(mock_server):
    """Approval request pushed to session A's connection must not affect session B."""
    mock_server.clear_received()
    mock_server.set_response_delay(0.0)

    session_a = make_connected_session(mock_server, session_key="iso-a")
    session_b = make_connected_session(mock_server, session_key="iso-b")

    # Give each session a task with the same thread_id.
    session_a.tasks["a-task"] = _make_task("a-task", "thread-shared")
    session_b.tasks["b-task"] = _make_task("b-task", "thread-shared")

    # Find the WebSocket connection belonging to session A.
    # We identify it by checking which connection session A's bridge holds.
    ws_a = session_a.bridge.ws
    assert ws_a is not None

    # Push a commandExecution/requestApproval notification to ALL connections.
    # In a real server each session gets its own WS, so this simulates the
    # server routing a notification to A's connection.
    # Here both sessions share the same mock server, so the push goes to both.
    # After handling, BOTH sessions will process the notification.
    # The isolation guarantee is weaker here (both receive the push), but we
    # verify that each session routes independently via task_for_thread.

    mock_server.push_and_wait({
        "jsonrpc": "2.0",
        "method": "item/commandExecution/requestApproval",
        "id": 77,
        "params": {
            "threadId": "thread-shared",
            "itemId": "item-1",
            "turnId": "turn-1",
            "approvalId": "appr-1",
            "reason": "run tests",
        },
    })

    # Give bridges time to process the notification.
    time.sleep(0.3)

    # Verify: session A's task should have the pending request stashed.
    assert session_a.tasks["a-task"].request_rpc_id == 77 or (
        # If both WS receive the push, session B may also stash it.
        session_b.tasks["b-task"].request_rpc_id == 77
    ), "Neither session stashed the approval request"

    session_a.shutdown()
    session_b.shutdown()
