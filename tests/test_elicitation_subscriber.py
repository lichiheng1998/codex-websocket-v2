from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from codex_websocket_v2.core.session import CodexSession
from codex_websocket_v2.core.state import Task, TaskTarget
from codex_websocket_v2.events.factory import EventFactory
from codex_websocket_v2.events.subscribers.elicitation import ElicitationSubscriber
from codex_websocket_v2.surfaces import commands
from codex_websocket_v2.surfaces.tool_actions import dispatch_tool_action


class FakeBridge:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def ws_send(self, message: str) -> None:
        self.sent.append(message)

    def run_sync(self, awaitable, timeout=None):
        try:
            awaitable.send(None)
        except StopIteration:
            return {"ok": True}
        return {"ok": False, "error": "awaitable did not finish synchronously"}


class FakeSession:
    def __init__(self) -> None:
        self.task = SimpleNamespace(
            thread_id="thread-1",
            task_id="task-1",
            request_rpc_id=None,
            request_type=None,
            request_payload=None,
            request_schema=None,
        )
        self.notifications: list[str] = []

    def task_for_thread(self, thread_id: str):
        return self.task if thread_id == self.task.thread_id else None

    def stash_request(self, task, rpc_id, request_type, payload, *, request_schema=None) -> None:
        task.request_rpc_id = rpc_id
        task.request_type = request_type
        task.request_payload = payload
        task.request_schema = request_schema

    async def notify(self, text: str) -> None:
        self.notifications.append(text)


def test_elicitation_subscriber_stashes_flat_requested_schema() -> None:
    raw = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "mcpServer/elicitation/request",
        "params": {
            "serverName": "elicitation_demo",
            "threadId": "thread-1",
            "turnId": "turn-1",
            "message": "Need trip details",
            "mode": "form",
            "requestedSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "city": {"type": "string", "title": "Destination city"},
                    "days": {"type": "integer", "title": "Trip length"},
                    "includeFood": {
                        "type": "boolean",
                        "title": "Include food suggestions",
                    },
                },
                "required": ["city", "days"],
            },
        },
    }
    session = FakeSession()
    event = EventFactory(session).from_raw(raw)

    asyncio.run(ElicitationSubscriber(session)(event))

    assert session.task.request_schema == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "properties": {
            "city": {"title": "Destination city", "type": "string"},
            "days": {"title": "Trip length", "type": "integer"},
            "includeFood": {"title": "Include food suggestions", "type": "boolean"},
        },
        "required": ["city", "days"],
        "type": "object",
    }
    assert '"city"' in session.notifications[0]
    assert "/codex approve task-1" in session.notifications[0]
    assert "/codex respond task-1" in session.notifications[0]


def test_elicitation_subscriber_treats_empty_schema_as_confirmation() -> None:
    raw = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "mcpServer/elicitation/request",
        "params": {
            "serverName": "elicitation-demo",
            "threadId": "thread-1",
            "turnId": "turn-1",
            "message": 'Allow the elicitation-demo MCP server to run tool "plan_trip"?',
            "mode": "form",
            "requestedSchema": {
                "type": "object",
                "properties": {},
            },
        },
    }
    session = FakeSession()
    event = EventFactory(session).from_raw(raw)

    asyncio.run(ElicitationSubscriber(session)(event))

    notification = session.notifications[0]
    assert session.task.request_schema is None
    assert "requests confirmation" in notification
    assert "Schema:" not in notification
    assert "/codex respond task-1" not in notification
    assert "/codex approve task-1" in notification
    assert "/codex deny task-1" in notification


def make_elicitation_session() -> tuple[CodexSession, FakeBridge]:
    bridge = FakeBridge()
    session = CodexSession("test", TaskTarget())
    session.bridge = bridge
    session.tasks["task-1"] = Task(
        task_id="task-1",
        thread_id="thread-1",
        cwd="/tmp",
        model="gpt-test",
        plan=False,
        sandbox_policy="workspace-write",
        approval_policy="on-request",
        request_rpc_id=99,
        request_type="elicitation",
        request_payload={"preview": "Need trip details", "server": "elicitation_demo"},
        request_schema={
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "days": {"type": "integer"},
            },
            "required": ["city", "days"],
        },
    )
    return session, bridge


def test_codex_action_respond_sends_content_not_schema() -> None:
    session, bridge = make_elicitation_session()
    content = {
        "city": "Shanghai",
        "days": 3,
        "budget": "medium",
        "includeFood": True,
    }

    result = dispatch_tool_action(
        "action",
        session,
        "respond",
        {"task_id": "task-1", "content": content},
    )

    assert json.loads(result) == {
        "ok": True,
        "task_id": "task-1",
        "decision": "respond",
    }
    assert json.loads(bridge.sent[0]) == {
        "jsonrpc": "2.0",
        "id": 99,
        "result": {"action": "accept", "content": content},
    }
    assert session.tasks["task-1"].request_rpc_id is None
    assert session.tasks["task-1"].request_type is None
    assert session.tasks["task-1"].request_schema is None


def test_codex_approval_approve_accepts_elicitation_with_empty_content() -> None:
    session, bridge = make_elicitation_session()

    result = dispatch_tool_action("approval", session, "approve", {"task_id": "task-1"})

    assert json.loads(result) == {
        "ok": True,
        "task_id": "task-1",
        "decision": "accept",
    }
    assert json.loads(bridge.sent[0]) == {
        "jsonrpc": "2.0",
        "id": 99,
        "result": {"action": "accept", "content": {}},
    }
    assert session.tasks["task-1"].request_rpc_id is None
    assert session.tasks["task-1"].request_type is None
    assert session.tasks["task-1"].request_schema is None


def test_codex_approval_deny_declines_elicitation() -> None:
    session, bridge = make_elicitation_session()

    result = dispatch_tool_action("approval", session, "deny", {"task_id": "task-1"})

    assert json.loads(result) == {
        "ok": True,
        "task_id": "task-1",
        "decision": "decline",
    }
    assert json.loads(bridge.sent[0]) == {
        "jsonrpc": "2.0",
        "id": 99,
        "result": {"action": "decline", "content": {}},
    }


def test_codex_tasks_rejects_moved_actions() -> None:
    session, _ = make_elicitation_session()

    for action in ("reply", "answer", "approve", "deny", "respond"):
        result = json.loads(dispatch_tool_action("task", session, action, {"task_id": "task-1"}))
        assert result == {"ok": False, "error": f"unknown action {action!r}"}


def test_codex_tasks_pending_schema_returns_elicitation_schema() -> None:
    session, _ = make_elicitation_session()

    result = json.loads(dispatch_tool_action(
        "task",
        session,
        "pending_schema",
        {"task_id": "task-1"},
    ))

    assert result == {
        "ok": True,
        "task_id": "task-1",
        "has_pending": True,
        "pending_type": "elicitation",
        "has_schema": True,
        "schema": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "days": {"type": "integer"},
            },
            "required": ["city", "days"],
        },
    }


def test_slash_commands_route_to_split_tools() -> None:
    calls: list[tuple[str, dict]] = []

    def dispatch(tool_name: str, args: dict) -> str:
        calls.append((tool_name, args))
        return json.dumps({"ok": True})

    commands.set_dispatch(dispatch)

    commands.handle_slash("approve task-1")
    commands.handle_slash("deny task-1")
    commands.handle_slash("respond task-1 '{\"city\":\"Shanghai\"}'")
    commands.handle_slash("reply task-1 hello")
    commands.handle_slash("answer task-1 yes")
    commands.handle_slash("pending-schema task-1")
    commands.handle_slash("archive task-1")

    assert [tool_name for tool_name, _ in calls] == [
        "codex_approval",
        "codex_approval",
        "codex_action",
        "codex_action",
        "codex_action",
        "codex_tasks",
        "codex_tasks",
    ]
    assert calls[-2] == (
        "codex_tasks",
        {"action": "pending_schema", "task_id": "task-1"},
    )
