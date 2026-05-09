"""End-to-end tool tests: real ActionEventBus + real CodexBridge + MockCodexAppServer.

Every test goes through the full production dispatch path:
  tools.py → make_event → action_bus.submit → bridge loop →
  subscriber → session method → bridge.rpc / bridge.ws_send →
  MockCodexAppServer → response → RpcResponseSubscriber → result_future

Fixtures ``mock_server`` and ``connected_session`` are defined in conftest.py.
"""
from __future__ import annotations

import concurrent.futures
import json
import time

import pytest

from codex_websocket_v2.core.state import Task, TaskTarget
from codex_websocket_v2.events.action_models import (
    ApproveEvent,
    DenyEvent,
    GetApprovalPolicyEvent,
    GetModelEvent,
    GetPlanEvent,
    GetSandboxEvent,
    ListTasksEvent,
    RemoveEvent,
    RespondEvent,
    ReviveEvent,
    SetApprovalPolicyEvent,
    SetModelEvent,
    SetPlanEvent,
    SetSandboxEvent,
    ShowPendingEvent,
    StartTaskEvent,
    SteerEvent,
    StopEvent,
    make_event,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _submit(session, event, timeout: float = 10.0) -> dict:
    """Submit an event to the action bus and return the parsed JSON result."""
    session.action_bus.submit(event)
    raw = event.result_future.result(timeout=timeout)
    return json.loads(raw)


def _call(map_name: str, action: str, session, args: dict, timeout: float = 10.0) -> dict:
    event = make_event(map_name, action, session, args)
    return _submit(session, event, timeout=timeout)


def _make_task(
    task_id: str = "task-1",
    thread_id: str = "thread-1",
    *,
    model: str = "mock-model",
    plan: bool = False,
    sandbox_policy: str = "workspace-write",
    approval_policy: str = "on-request",
    active_turn_id: str = "",
    request_rpc_id=None,
    request_type: str | None = None,
    request_payload: dict | None = None,
    request_schema: dict | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        thread_id=thread_id,
        cwd="/tmp",
        model=model,
        plan=plan,
        sandbox_policy=sandbox_policy,
        approval_policy=approval_policy,
        active_turn_id=active_turn_id,
        request_rpc_id=request_rpc_id,
        request_type=request_type,
        request_payload=request_payload,
        request_schema=request_schema,
    )


def _wait_for_received(server, method: str, *, count: int = 1, timeout: float = 2.0):
    """Poll until the mock server has at least ``count`` frames for ``method``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frames = server.received_by_method(method)
        if len(frames) >= count:
            return frames
        time.sleep(0.01)
    frames = server.received_by_method(method)
    raise AssertionError(
        f"Expected {count} frame(s) for method={method!r}, got {len(frames)}"
    )


def _wait_for_response(server, rpc_id, *, timeout: float = 2.0) -> dict:
    """Poll until the mock server has received a response frame with the given id."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in server.received_responses():
            if frame.get("id") == rpc_id:
                return frame
        time.sleep(0.01)
    raise AssertionError(f"No response frame with id={rpc_id!r} received")


# ── codex_task ─────────────────────────────────────────────────────────────────

class TestCodexTask:
    def test_invalid_plan_returns_error(self, connected_session):
        """StartTaskSubscriber rejects invalid plan values."""
        session = connected_session
        event = StartTaskEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"cwd": "/tmp", "prompt": "hi", "plan": "maybe"},
        )
        result = _submit(session, event)
        assert result["ok"] is False
        assert "plan" in result["error"].lower()

    def test_start_task_returns_task_id(self, connected_session):
        """StartTaskEvent through action bus returns ok with task_id."""
        session = connected_session
        event = StartTaskEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"cwd": "/tmp", "prompt": "write a hello world"},
        )
        result = _submit(session, event)
        assert result["ok"] is True
        assert result["task_id"]
        assert result["model"] == "mock-model"


# ── codex_revive ───────────────────────────────────────────────────────────────

class TestCodexRevive:
    def test_missing_thread_id_returns_error(self, connected_session):
        event = ReviveEvent(
            session=connected_session,
            result_future=concurrent.futures.Future(),
            args={"thread_id": ""},
        )
        result = _submit(connected_session, event)
        assert result["ok"] is False
        assert "thread_id" in result["error"].lower()

    def test_revive_binds_task(self, connected_session, mock_server):
        """ReviveEvent calls thread/read, then binds the thread as a new task."""
        mock_server.set_rpc_handler(
            "thread/read",
            lambda msg: {
                "thread": {
                    "id": "thread-abc-123",
                    "cwd": "/tmp",
                    "status": {"type": "loaded"},
                }
            },
        )
        session = connected_session
        event = ReviveEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"thread_id": "thread-abc-123"},
        )
        result = _submit(session, event)
        assert result["ok"] is True
        assert result["thread_id"] == "thread-abc-123"
        assert result["task_id"]
        assert result["task_id"] in session.tasks


# ── codex_tasks ────────────────────────────────────────────────────────────────

class TestCodexTasks:
    def test_list_empty(self, connected_session):
        result = _call("task", "list", connected_session, {})
        assert result["ok"] is True
        assert result["tasks"] == []

    def test_list_with_tasks(self, connected_session):
        session = connected_session
        session.tasks["task-1"] = _make_task()
        result = _call("task", "list", session, {})
        assert result["ok"] is True
        assert len(result["tasks"]) == 1
        t = result["tasks"][0]
        assert t["task_id"] == "task-1"
        assert t["thread_id"] == "thread-1"
        assert t["model"] == "mock-model"
        assert t["plan"] == "off"
        assert t["pending"] is None

    def test_list_task_with_pending_shows_type(self, connected_session):
        session = connected_session
        session.tasks["task-2"] = _make_task(
            "task-2", "thread-2",
            request_rpc_id=42,
            request_type="command",
            request_payload={"preview": "rm -rf /tmp/foo"},
        )
        result = _call("task", "list", session, {})
        assert result["ok"] is True
        t = next(t for t in result["tasks"] if t["task_id"] == "task-2")
        assert t["pending"] == {"type": "command"}

    def test_show_pending_no_request(self, connected_session):
        session = connected_session
        session.tasks["task-1"] = _make_task()
        result = _call("task", "show_pending", session, {"task_id": "task-1"})
        assert result["ok"] is True
        assert result["pending"] is None

    def test_show_pending_with_command_approval(self, connected_session):
        session = connected_session
        session.tasks["task-1"] = _make_task(
            request_rpc_id=99,
            request_type="command",
            request_payload={"preview": "git push --force"},
        )
        result = _call("task", "show_pending", session, {"task_id": "task-1"})
        assert result["ok"] is True
        assert result["pending"]["type"] == "command"
        assert result["pending"]["rpc_id"] == 99
        assert "git push" in result["pending"]["message"]

    def test_show_pending_missing_task_id_errors(self, connected_session):
        result = _call("task", "show_pending", connected_session, {})
        assert result["ok"] is False
        assert "task_id" in result["error"].lower()

    def test_show_pending_unknown_task_id_errors(self, connected_session):
        result = _call("task", "show_pending", connected_session, {"task_id": "nope"})
        assert result["ok"] is False
        assert "nope" in result["error"]

    def test_unknown_action_errors(self, connected_session):
        with pytest.raises(KeyError):
            make_event("task", "fly", connected_session, {})

    def test_archive_unbound_thread(self, connected_session, mock_server):
        session = connected_session
        result = _call("task", "archive", session, {"target": "thread-orphan"})
        assert result["ok"] is True
        _wait_for_received(mock_server, "thread/archive")


# ── codex_remove ───────────────────────────────────────────────────────────────

class TestCodexRemove:
    def test_remove_single_task(self, connected_session):
        session = connected_session
        session.tasks["task-1"] = _make_task()
        event = RemoveEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "task-1"},
        )
        result = _submit(session, event)
        assert result["ok"] is True
        assert result["scope"] == "task"
        assert result["task_id"] == "task-1"
        assert result["thread_id"] == "thread-1"
        assert "task-1" not in session.tasks

    def test_remove_all_tasks(self, connected_session):
        session = connected_session
        session.tasks["t1"] = _make_task("t1", "th1")
        session.tasks["t2"] = _make_task("t2", "th2")
        event = RemoveEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"all": True},
        )
        result = _submit(session, event)
        assert result["ok"] is True
        assert result["scope"] == "all"
        assert result["removed"] == 2
        assert session.tasks == {}

    def test_remove_unknown_task_errors(self, connected_session):
        event = RemoveEvent(
            session=connected_session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "ghost"},
        )
        result = _submit(connected_session, event)
        assert result["ok"] is False
        assert "ghost" in result["error"]

    def test_remove_no_args_errors(self, connected_session):
        event = RemoveEvent(
            session=connected_session,
            result_future=concurrent.futures.Future(),
            args={},
        )
        result = _submit(connected_session, event)
        assert result["ok"] is False
        assert "task_id" in result["error"].lower()


# ── codex_approval ─────────────────────────────────────────────────────────────

class TestCodexApproval:
    def test_approve_command_sends_accept_decision(self, connected_session, mock_server):
        """approve sends {"decision":"accept"} to the server as a WS response."""
        session = connected_session
        session.tasks["task-1"] = _make_task(
            request_rpc_id=42,
            request_type="command",
            request_payload={
                "preview": "rm -rf /tmp/foo",
                "approval_kind": "commandExecution",
            },
        )
        event = ApproveEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "task-1"},
        )
        result = _submit(session, event)
        assert result["ok"] is True

        frame = _wait_for_response(mock_server, rpc_id=42)
        assert frame["result"]["decision"] == "accept"

    def test_deny_command_sends_decline_decision(self, connected_session, mock_server):
        session = connected_session
        session.tasks["task-1"] = _make_task(
            request_rpc_id=43,
            request_type="command",
            request_payload={"preview": "curl evil.com", "approval_kind": "commandExecution"},
        )
        event = DenyEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "task-1"},
        )
        result = _submit(session, event)
        assert result["ok"] is True

        frame = _wait_for_response(mock_server, rpc_id=43)
        assert frame["result"]["decision"] == "decline"

    def test_approve_elicitation_sends_accept_action(self, connected_session, mock_server):
        session = connected_session
        session.tasks["task-1"] = _make_task(
            request_rpc_id=44,
            request_type="elicitation",
            request_payload={"preview": "City?"},
            request_schema={"type": "object", "properties": {"city": {"type": "string"}}},
        )
        event = ApproveEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "task-1"},
        )
        result = _submit(session, event)
        assert result["ok"] is True

        frame = _wait_for_response(mock_server, rpc_id=44)
        assert frame["result"]["action"] == "accept"
        assert frame["result"]["content"] == {}

    def test_respond_elicitation_sends_content(self, connected_session, mock_server):
        session = connected_session
        session.tasks["task-1"] = _make_task(
            request_rpc_id=45,
            request_type="elicitation",
            request_payload={"preview": "City?"},
            request_schema={"type": "object", "properties": {"city": {"type": "string"}}},
        )
        event = RespondEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "task-1", "content": {"city": "Shanghai"}},
        )
        result = _submit(session, event)
        assert result["ok"] is True

        frame = _wait_for_response(mock_server, rpc_id=45)
        assert frame["result"]["action"] == "accept"
        assert frame["result"]["content"]["city"] == "Shanghai"

    def test_approve_missing_task_id_errors(self, connected_session):
        event = ApproveEvent(
            session=connected_session,
            result_future=concurrent.futures.Future(),
            args={},
        )
        result = _submit(connected_session, event)
        assert result["ok"] is False
        assert "task_id" in result["error"].lower()

    def test_approve_no_pending_errors(self, connected_session):
        session = connected_session
        session.tasks["task-1"] = _make_task()  # no pending request
        event = ApproveEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "task-1"},
        )
        result = _submit(session, event)
        assert result["ok"] is False
        assert "pending" in result["error"].lower()


# ── codex_action ───────────────────────────────────────────────────────────────

class TestCodexAction:
    def test_steer_active_turn_sends_rpc(self, connected_session, mock_server):
        session = connected_session
        session.tasks["task-1"] = _make_task(active_turn_id="turn-99")
        event = SteerEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "task-1", "message": "focus on tests"},
        )
        result = _submit(session, event)
        assert result["ok"] is True

        frames = _wait_for_received(mock_server, "turn/steer")
        params = frames[0].get("params", {})
        assert params.get("expectedTurnId") == "turn-99"
        assert any(
            item.get("text") == "focus on tests"
            for item in (params.get("input") or [])
        )

    def test_steer_no_active_turn_errors(self, connected_session):
        session = connected_session
        session.tasks["task-1"] = _make_task(active_turn_id="")
        event = SteerEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "task-1", "message": "steer me"},
        )
        result = _submit(session, event)
        assert result["ok"] is False
        assert "no active turn" in result["error"]

    def test_steer_missing_message_errors(self, connected_session):
        session = connected_session
        session.tasks["task-1"] = _make_task(active_turn_id="turn-1")
        event = SteerEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "task-1", "message": ""},
        )
        result = _submit(session, event)
        assert result["ok"] is False
        assert "message" in result["error"].lower()

    def test_stop_active_turn_sends_rpc(self, connected_session, mock_server):
        session = connected_session
        session.tasks["task-1"] = _make_task(active_turn_id="turn-77")
        event = StopEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "task-1"},
        )
        result = _submit(session, event)
        assert result["ok"] is True

        frames = _wait_for_received(mock_server, "turn/interrupt")
        params = frames[0].get("params", {})
        assert params.get("turnId") == "turn-77"

    def test_stop_no_active_turn_errors(self, connected_session):
        session = connected_session
        session.tasks["task-1"] = _make_task(active_turn_id="")
        event = StopEvent(
            session=session,
            result_future=concurrent.futures.Future(),
            args={"task_id": "task-1"},
        )
        result = _submit(session, event)
        assert result["ok"] is False
        assert "no active turn" in result["error"]

    def test_unknown_action_raises(self, connected_session):
        with pytest.raises(KeyError):
            make_event("action", "fly", connected_session, {})


# ── codex_models ───────────────────────────────────────────────────────────────

class TestCodexModels:
    def test_get_session_model(self, connected_session):
        result = _call("model", "get", connected_session, {})
        assert result["ok"] is True
        assert result["model"] == "mock-model"
        assert result["scope"] == "default"

    def test_get_task_model(self, connected_session):
        session = connected_session
        session.tasks["task-1"] = _make_task(model="special-model")
        result = _call("model", "get", session, {"task_id": "task-1"})
        assert result["ok"] is True
        assert result["model"] == "special-model"
        assert result["scope"] == "task"

    def test_set_session_model(self, connected_session):
        result = _call("model", "set", connected_session, {"model_id": "mock-model"})
        assert result["ok"] is True
        assert connected_session.default_model == "mock-model"
        assert result["scope"] == "default"

    def test_set_task_model(self, connected_session):
        session = connected_session
        session.tasks["task-1"] = _make_task(model="old-model")
        result = _call("model", "set", session, {"task_id": "task-1", "model_id": "mock-model"})
        assert result["ok"] is True
        assert session.tasks["task-1"].model == "mock-model"
        assert result["scope"] == "task"

    def test_set_missing_model_id_errors(self, connected_session):
        result = _call("model", "set", connected_session, {})
        assert result["ok"] is False
        assert "model_id" in result["error"].lower()

    def test_list_models(self, connected_session):
        result = _call("model", "list", connected_session, {})
        assert result["ok"] is True
        assert any(m.get("model") == "mock-model" for m in result.get("models", []))


# ── codex_session ──────────────────────────────────────────────────────────────

class TestCodexSession:
    def test_plan_get_returns_current(self, connected_session):
        result = _call("session", "plan_get", connected_session, {})
        assert result["ok"] is True
        assert result["plan"] in ("on", "off")

    def test_plan_set_and_get(self, connected_session):
        _call("session", "plan_set", connected_session, {"plan": "on"})
        result = _call("session", "plan_get", connected_session, {})
        assert result["ok"] is True
        assert result["plan"] == "on"

        _call("session", "plan_set", connected_session, {"plan": "off"})
        result = _call("session", "plan_get", connected_session, {})
        assert result["plan"] == "off"

    def test_plan_set_invalid_errors(self, connected_session):
        result = _call("session", "plan_set", connected_session, {"plan": "maybe"})
        assert result["ok"] is False
        assert "plan" in result["error"].lower()

    def test_sandbox_get_and_set(self, connected_session):
        result = _call("session", "sandbox_get", connected_session, {})
        assert result["ok"] is True

        _call("session", "sandbox_set", connected_session, {"sandbox_policy": "read-only"})
        result = _call("session", "sandbox_get", connected_session, {})
        assert result["ok"] is True
        assert result["sandbox_policy"] == "read-only"

    def test_sandbox_set_invalid_errors(self, connected_session):
        result = _call("session", "sandbox_set", connected_session, {"sandbox_policy": "chaos"})
        assert result["ok"] is False

    def test_approval_get_and_set(self, connected_session):
        _call("session", "approval_set", connected_session, {"approval_policy": "never"})
        result = _call("session", "approval_get", connected_session, {})
        assert result["ok"] is True
        assert result["approval_policy"] == "never"

    def test_approval_set_invalid_errors(self, connected_session):
        result = _call("session", "approval_set", connected_session, {"approval_policy": "always"})
        assert result["ok"] is False

    def test_unknown_action_raises(self, connected_session):
        with pytest.raises(KeyError):
            make_event("session", "fly", connected_session, {})
