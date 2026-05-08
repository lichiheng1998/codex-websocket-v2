from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from codex_websocket_v2.core.session import CodexSession
from codex_websocket_v2.core import session_registry
from codex_websocket_v2.core.state import Task, TaskTarget
from codex_websocket_v2.events.factory import EventFactory
from codex_websocket_v2.events.models import (
    ItemCompletedEvent,
    ItemStartedEvent,
    ServerRequestResolvedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
    UnboundTaskEvent,
    UserInputRequestedEvent,
)
from codex_websocket_v2.events.subscribers.approval import ApprovalRequestSubscriber
from codex_websocket_v2.events.subscribers.elicitation import ElicitationSubscriber
from codex_websocket_v2.events.subscribers.input import UserInputRequestSubscriber
from codex_websocket_v2.events.subscribers.notification import NotificationSubscriber
from codex_websocket_v2.events.subscribers.unhandled import UnboundTaskSubscriber
from codex_websocket_v2.surfaces import commands
from codex_websocket_v2.surfaces.tool_actions import dispatch_remove_tool, dispatch_tool_action


class FakeBridge:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def ws_send(self, message: str) -> dict:
        self.sent.append(message)
        return {"ok": True}

    def run_sync(self, awaitable, timeout=None):
        try:
            awaitable.send(None)
        except StopIteration as exc:
            return exc.value
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
            started_items={},
            active_turn_id="",
            last_item=None,
            last_item_type="",
            last_turn_status="",
        )
        self.notifications: list[str] = []
        self.verbose = "on"

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
    notification = session.notifications[0]
    assert "Fields:" in notification
    assert "`city`*: string — Destination city" in notification
    assert "`days`*: integer — Trip length" in notification
    assert "`includeFood`: boolean — Include food suggestions" in notification
    assert "Full schema: `/codex pending task-1`" in notification
    assert '"properties"' not in notification
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


def test_file_change_approval_lists_file_changes_from_started_item() -> None:
    session = FakeSession()
    session.task.started_items = {
        "item-1": SimpleNamespace(
            id="item-1",
            type=SimpleNamespace(value="fileChange"),
            changes=[
                SimpleNamespace(path="src/app.py", kind=SimpleNamespace(value="update"), diff="-old\n+new"),
                SimpleNamespace(path="README.md", kind=SimpleNamespace(value="create"), diff="new file"),
            ],
        )
    }
    params = SimpleNamespace(
        threadId="thread-1",
        itemId="item-1",
        reason="Need to update files",
        grantRoot="/work/repo",
    )

    asyncio.run(ApprovalRequestSubscriber(session)._handle_file_change_approval(params, 9))

    notification = session.notifications[0]
    assert "`src/app.py`" in notification
    assert "`README.md`" in notification
    assert "Need to update files" in notification
    assert session.task.request_payload["preview"] == "src/app.py, README.md"
    assert session.task.request_payload["started_item"]["changes"][0]["diff"] == "-old\n+new"


def test_command_approval_prefers_command_from_started_item() -> None:
    session = FakeSession()
    session.task.started_items = {
        "cmd-1": SimpleNamespace(
            id="cmd-1",
            type=SimpleNamespace(value="commandExecution"),
            command="pytest tests/test_app.py",
            cwd=SimpleNamespace(root="/work/repo"),
            commandActions=[
                SimpleNamespace(root=SimpleNamespace(type=SimpleNamespace(value="search"), query="test_app")),
            ],
        )
    }
    params = SimpleNamespace(
        threadId="thread-1",
        itemId="cmd-1",
        approvalId="approval-1",
        reason="Run focused tests",
        command=None,
    )

    asyncio.run(ApprovalRequestSubscriber(session)._handle_command_approval(params, 11, "commandExecution"))

    notification = session.notifications[0]
    assert "pytest tests/test_app.py" in notification
    assert "cwd: `/work/repo`" in notification
    assert "Actions:" in notification
    assert "Item: `cmd-1`" in notification
    assert "Approval: `approval-1`" in notification
    assert session.task.request_payload["preview"] == "pytest tests/test_app.py"
    assert session.task.request_payload["started_item"]["command"] == "pytest tests/test_app.py"


def test_command_approval_shows_network_only_context() -> None:
    session = FakeSession()
    params = SimpleNamespace(
        threadId="thread-1",
        itemId="cmd-2",
        reason="Allow network",
        command=None,
        networkApprovalContext=SimpleNamespace(protocol=SimpleNamespace(value="https"), host="example.com"),
        additionalPermissions=SimpleNamespace(network=SimpleNamespace(enabled=True)),
    )

    asyncio.run(ApprovalRequestSubscriber(session)._handle_command_approval(params, 12, "commandExecution"))

    notification = session.notifications[0]
    assert "(codex command)" in notification
    assert "Additional permissions: network access" in notification
    assert "Network request: `https://example.com`" in notification
    assert session.task.request_payload["network_approval_context"]["host"] == "example.com"


def test_file_change_approval_mentions_missing_file_list_and_item_id() -> None:
    session = FakeSession()
    params = SimpleNamespace(
        threadId="thread-1",
        itemId="item-1",
        reason="Need write access",
        grantRoot=None,
    )

    asyncio.run(ApprovalRequestSubscriber(session)._handle_file_change_approval(params, 10))

    notification = session.notifications[0]
    assert "Files: not found in the preceding item/started" in notification
    assert "Item: `item-1`" in notification
    assert session.task.request_payload["preview"] == "write permission — item-1"
    assert session.task.request_payload["item_id"] == "item-1"


def test_notification_subscriber_caches_started_items_until_completed() -> None:
    session = FakeSession()
    item = SimpleNamespace(id="item-1", type=SimpleNamespace(value="fileChange"), changes=[])
    subscriber = NotificationSubscriber(session)

    asyncio.run(subscriber(ItemStartedEvent(session=session, raw={}, task=session.task, item=item, item_type="fileChange")))

    assert session.task.started_items["item-1"] is item

    asyncio.run(subscriber(ItemCompletedEvent(session=session, raw={}, task=session.task, item=item, item_type="fileChange")))

    assert "item-1" not in session.task.started_items


def test_notification_subscriber_does_not_truncate_long_agent_message() -> None:
    session = FakeSession()
    session.verbose = "on"
    subscriber = NotificationSubscriber(session)
    text = "x" * 5000
    item = SimpleNamespace(id="msg-1", text=text)

    asyncio.run(subscriber(ItemCompletedEvent(session=session, raw={}, task=session.task, item=item, item_type="agentMessage")))

    notification = session.notifications[0]
    assert text in notification
    assert "truncated" not in notification


def test_notification_subscriber_middle_ellipsizes_long_command_output() -> None:
    session = FakeSession()
    session.verbose = "on"
    subscriber = NotificationSubscriber(session)
    output = "A" * 800 + "MIDDLE" + "Z" * 800
    item = SimpleNamespace(id="cmd-1", command="pytest", exitCode=0, aggregatedOutput=output)

    asyncio.run(subscriber(ItemCompletedEvent(session=session, raw={}, task=session.task, item=item, item_type="commandExecution")))

    notification = session.notifications[0]
    assert "A" * 100 in notification
    assert "Z" * 100 in notification
    assert "MIDDLE" not in notification
    assert "... omitted " in notification
    assert notification.count("```") == 2


def test_user_input_notification_avoids_html_like_placeholders() -> None:
    session = FakeSession()
    params = SimpleNamespace(
        questions=[
            SimpleNamespace(
                header="Choice",
                question="Pick one",
                options=[SimpleNamespace(label="A", description="Alpha")],
                isOther=False,
                isSecret=False,
            ),
            SimpleNamespace(
                header="Confirm",
                question="Continue?",
                options=[],
                isOther=False,
                isSecret=False,
            ),
        ]
    )
    event = UserInputRequestedEvent(
        session=session,
        raw={},
        rpc_id=13,
        params=params,
        task=session.task,
        thread_id="thread-1",
    )

    asyncio.run(UserInputRequestSubscriber(session)(event))

    notification = session.notifications[0]
    assert "<" not in notification
    assert ">" not in notification
    assert "/codex answer task-1 [answer]" in notification
    assert "answer1 | answer2 | answer3" in notification


def test_server_request_resolved_clears_pending_request_and_notifies() -> None:
    session = FakeSession()
    session.tasks = {"task-1": session.task}
    session.task.request_rpc_id = 10
    session.task.request_type = "command"
    session.task.request_payload = {"preview": "src/app.py"}
    session.task.request_schema = {"type": "object"}
    subscriber = NotificationSubscriber(session)

    asyncio.run(subscriber(ServerRequestResolvedEvent(session=session, raw={}, request_id=10)))

    assert session.task.request_rpc_id is None
    assert session.task.request_type is None
    assert session.task.request_payload is None
    assert session.task.request_schema is None
    assert "request resolved: src/app.py" in session.notifications[0]


def test_notification_subscriber_tracks_active_turn_until_completion() -> None:
    session = FakeSession()
    subscriber = NotificationSubscriber(session)

    asyncio.run(subscriber(TurnStartedEvent(
        session=session,
        raw={},
        task=session.task,
        thread_id="thread-1",
        turn=SimpleNamespace(id="turn-1"),
    )))

    assert session.task.active_turn_id == "turn-1"

    asyncio.run(subscriber(TurnCompletedEvent(
        session=session,
        raw={},
        task=session.task,
        thread_id="thread-1",
        turn=SimpleNamespace(id="turn-1", status="completed", error=None),
        status="completed",
    )))

    assert session.task.active_turn_id == ""
    assert session.task.last_turn_status == "completed"


def test_event_factory_parses_turn_started_notification() -> None:
    session = FakeSession()
    raw = {
        "jsonrpc": "2.0",
        "method": "turn/started",
        "params": {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "inProgress", "items": []},
        },
    }

    event = EventFactory(session).from_raw(raw)

    assert isinstance(event, TurnStartedEvent)
    assert event.thread_id == "thread-1"
    assert event.task is session.task
    assert event.turn.id == "turn-1"


def test_event_factory_returns_unbound_task_event_for_legacy_notifications(caplog) -> None:
    session = FakeSession()
    session.task.thread_id = "other-thread"
    raw_events = [
        {
            "jsonrpc": "2.0",
            "method": "turn/started",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "inProgress", "items": []},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed", "items": []},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "item/started",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "item-1", "type": "plan", "text": "plan"},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "item-1", "type": "plan", "text": "plan"},
            },
        },
    ]

    for raw in raw_events:
        event = EventFactory(session).from_raw(raw)
        assert isinstance(event, UnboundTaskEvent)
        assert event.thread_id == "thread-1"
        asyncio.run(UnboundTaskSubscriber()(event))

    assert session.notifications == []
    assert "ignoring turn/started for unbound thread thread-1" in caplog.text
    assert "ignoring item/completed for unbound thread thread-1" in caplog.text


def test_event_factory_returns_unbound_task_event_for_legacy_requests(caplog) -> None:
    session = FakeSession()
    session.task.thread_id = "other-thread"
    raw_events = [
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "questions": [{"id": "q1", "header": "H", "question": "Q?", "options": []}],
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "command": "pytest",
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "mcpServer/elicitation/request",
            "params": {
                "serverName": "demo",
                "threadId": "thread-1",
                "turnId": "turn-1",
                "message": "Confirm?",
                "mode": "form",
                "requestedSchema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        },
    ]

    for raw in raw_events:
        event = EventFactory(session).from_raw(raw)
        assert isinstance(event, UnboundTaskEvent)
        assert event.thread_id == "thread-1"
        asyncio.run(UnboundTaskSubscriber()(event))

    assert session.notifications == []
    assert session.task.request_rpc_id is None
    assert "ignoring item/tool/requestUserInput for unbound thread thread-1 rpc_id=11" in caplog.text
    assert "ignoring mcpServer/elicitation/request for unbound thread thread-1 rpc_id=13" in caplog.text


def test_notification_subscriber_keeps_active_turn_on_mismatched_completion() -> None:
    session = FakeSession()
    session.task.active_turn_id = "turn-2"
    subscriber = NotificationSubscriber(session)

    asyncio.run(subscriber(TurnCompletedEvent(
        session=session,
        raw={},
        task=session.task,
        thread_id="thread-1",
        turn=SimpleNamespace(id="turn-1", status="completed", error=None),
        status="completed",
    )))

    assert session.task.active_turn_id == "turn-2"


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


class RecordingRpcBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, float | None]] = []
        self.thread_pages = [[{"id": "thread-1"}, {"id": "thread-2"}]]

    def is_connected(self) -> bool:
        return True

    async def rpc(self, method: str, params=None, timeout=None):
        self.calls.append((method, params, timeout))
        if method == "thread/list":
            return {"ok": True, "result": {"data": self.thread_pages[0], "nextCursor": None}}
        return {"ok": True, "result": {}}

    def run_sync(self, awaitable, timeout=None):
        try:
            return awaitable.send(None)
        except StopIteration as exc:
            return exc.value


def make_control_session(active_turn_id: str = "turn-1") -> tuple[CodexSession, RecordingRpcBridge]:
    bridge = RecordingRpcBridge()
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
        active_turn_id=active_turn_id,
    )
    return session, bridge


def params_payload(params) -> dict:
    if hasattr(params, "model_dump"):
        return params.model_dump(by_alias=True, exclude_none=True, mode="json")
    return dict(params)


def test_steer_task_sends_turn_steer_payload() -> None:
    session, bridge = make_control_session()

    result = asyncio.run(session.steer_task("task-1", "please focus tests"))

    assert result == {"ok": True, "task_id": "task-1", "turn_id": "turn-1"}
    method, params, _timeout = bridge.calls[0]
    assert method == "turn/steer"
    assert params_payload(params) == {
        "threadId": "thread-1",
        "expectedTurnId": "turn-1",
        "input": [{"type": "text", "text": "please focus tests", "text_elements": []}],
    }


def test_stop_task_sends_turn_interrupt_payload() -> None:
    session, bridge = make_control_session()

    result = asyncio.run(session.stop_task("task-1"))

    assert result == {"ok": True, "task_id": "task-1", "turn_id": "turn-1"}
    method, params, _timeout = bridge.calls[0]
    assert method == "turn/interrupt"
    assert params_payload(params) == {
        "threadId": "thread-1",
        "turnId": "turn-1",
    }


def test_archive_thread_rejects_bound_thread_without_rpc() -> None:
    session, bridge = make_control_session()

    result = asyncio.run(session.archive_thread("thread-1"))

    assert result == {
        "ok": False,
        "error": "thread 'thread-1' is bound to an active task; remove the task binding first",
    }
    assert bridge.calls == []


def test_archive_thread_sends_thread_archive_for_unbound_thread() -> None:
    session, bridge = make_control_session()

    result = asyncio.run(session.archive_thread("thread-2"))

    assert result == {"ok": True, "thread_id": "thread-2"}
    method, params, _timeout = bridge.calls[0]
    assert method == "thread/archive"
    assert params_payload(params) == {"threadId": "thread-2"}


def test_archive_all_threads_skips_bound_threads_without_clearing_tasks() -> None:
    session, bridge = make_control_session()
    bridge.thread_pages = [[{"id": "thread-1"}, {"id": "thread-2"}]]

    result = asyncio.run(session.archive_all_threads())

    assert result == {
        "ok": True,
        "removed": 1,
        "skipped": [{"thread_id": "thread-1", "owner": "test"}],
        "errors": [],
    }
    assert "task-1" in session.tasks
    assert [method for method, _params, _timeout in bridge.calls] == ["thread/list", "thread/archive"]


def test_remove_task_and_all_only_unbind_local_tasks() -> None:
    session, bridge = make_control_session()
    session.tasks["task-2"] = Task(
        task_id="task-2",
        thread_id="thread-2",
        cwd="/tmp",
        model="gpt-test",
        plan=False,
        sandbox_policy="workspace-write",
        approval_policy="on-request",
    )

    assert session.remove_task("task-1") == {"ok": True, "task_id": "task-1", "thread_id": "thread-1"}
    assert list(session.tasks) == ["task-2"]
    assert bridge.calls == []

    assert session.remove_all_tasks() == {
        "ok": True,
        "removed": 1,
        "tasks": [{"task_id": "task-2", "thread_id": "thread-2"}],
        "errors": [],
    }
    assert session.tasks == {}
    assert bridge.calls == []


def test_steer_and_stop_validate_task_state() -> None:
    session, bridge = make_control_session(active_turn_id="")

    assert asyncio.run(session.steer_task("missing", "hi")) == {"ok": False, "error": "unknown task id 'missing'"}
    assert asyncio.run(session.steer_task("task-1", "")) == {"ok": False, "error": "message is required for steer"}
    assert asyncio.run(session.steer_task("task-1", "hi")) == {
        "ok": False,
        "error": "task 'task-1' has no active turn to steer",
    }
    assert asyncio.run(session.stop_task("task-1")) == {
        "ok": False,
        "error": "task 'task-1' has no active turn to stop",
    }
    assert bridge.calls == []


def test_codex_action_steer_and_stop_route_to_session_methods() -> None:
    session, _bridge = make_control_session()

    steer = asyncio.run(session.steer_task("task-1", "please focus tests"))
    stop = asyncio.run(session.stop_task("task-1"))

    assert steer == {"ok": True, "task_id": "task-1", "turn_id": "turn-1"}
    assert stop == {"ok": True, "task_id": "task-1", "turn_id": "turn-1"}


def test_codex_tasks_archive_is_thread_only_and_remove_tool_unbinds() -> None:
    session, _bridge = make_control_session()

    removed_archive = json.loads(dispatch_tool_action("task", session, "archive", {"target": "all"}))
    removed_task = json.loads(dispatch_remove_tool(session, {"task_id": "task-1"}))

    assert removed_archive == {
        "ok": False,
        "error": "archive target 'all' was removed; use codex_remove with all=true to unbind tasks",
    }
    assert removed_task == {
        "ok": True,
        "scope": "task",
        "task_id": "task-1",
        "thread_id": "thread-1",
    }
    assert session.tasks == {}


def test_codex_remove_all_unbinds_all_tasks() -> None:
    session, _bridge = make_control_session()

    result = json.loads(dispatch_remove_tool(session, {"all": True}))

    assert result == {
        "ok": True,
        "scope": "all",
        "removed": 1,
        "tasks": [{"task_id": "task-1", "thread_id": "thread-1"}],
    }
    assert session.tasks == {}


def test_codex_action_respond_sends_content_not_schema() -> None:
    session, bridge = make_elicitation_session()
    content = {
        "city": "Shanghai",
        "days": 3,
        "budget": "medium",
        "includeFood": True,
    }

    result = dispatch_tool_action("action", session, "respond", {"task_id": "task-1", "content": content})

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
    assert session.tasks["task-1"].request_rpc_id == 99
    assert session.tasks["task-1"].request_type == "elicitation"
    assert session.tasks["task-1"].request_schema is not None
    assert session.tasks["task-1"].request_payload["response_sent"] is True
    assert session.tasks["task-1"].request_payload["decision"] == "respond"


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
    assert session.tasks["task-1"].request_rpc_id == 99
    assert session.tasks["task-1"].request_type == "elicitation"
    assert session.tasks["task-1"].request_schema is not None
    assert session.tasks["task-1"].request_payload["response_sent"] is True
    assert session.tasks["task-1"].request_payload["decision"] == "accept"


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

    for action in ("reply", "answer", "approve", "deny", "respond", "steer", "stop"):
        result = json.loads(dispatch_tool_action("task", session, action, {"task_id": "task-1"}))
        assert result == {"ok": False, "error": f"unknown action {action!r}"}


def test_codex_tasks_show_pending_returns_elicitation_details() -> None:
    session, _ = make_elicitation_session()

    result = json.loads(dispatch_tool_action(
        "task",
        session,
        "show_pending",
        {"task_id": "task-1"},
    ))

    assert result == {
        "ok": True,
        "task_id": "task-1",
        "pending": {
            "type": "elicitation",
            "rpc_id": 99,
            "message": "Need trip details",
            "payload": {"preview": "Need trip details", "server": "elicitation_demo"},
            "schema": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "days": {"type": "integer"},
                },
                "required": ["city", "days"],
            },
        },
    }


def test_codex_tasks_show_pending_returns_null_when_none() -> None:
    session, _ = make_elicitation_session()
    task = session.tasks["task-1"]
    task.request_rpc_id = None
    task.request_type = None
    task.request_payload = None
    task.request_schema = None

    result = json.loads(dispatch_tool_action(
        "task",
        session,
        "show_pending",
        {"task_id": "task-1"},
    ))

    assert result == {"ok": True, "task_id": "task-1", "pending": None}


def test_codex_tasks_show_pending_returns_command_details() -> None:
    session, _ = make_elicitation_session()
    task = session.tasks["task-1"]
    task.request_rpc_id = 42
    task.request_type = "command"
    task.request_payload = {
        "preview": "echo hello",
        "reason": "test command",
        "cmd_type": "commandExecution",
    }
    task.request_schema = None

    result = json.loads(dispatch_tool_action(
        "task",
        session,
        "show_pending",
        {"task_id": "task-1"},
    ))

    assert result == {
        "ok": True,
        "task_id": "task-1",
        "pending": {
            "type": "command",
            "rpc_id": 42,
            "message": "echo hello",
            "payload": {
                "preview": "echo hello",
                "reason": "test command",
                "cmd_type": "commandExecution",
            },
            "schema": None,
        },
    }


def test_codex_tasks_show_pending_returns_input_details() -> None:
    session, _ = make_elicitation_session()
    task = session.tasks["task-1"]
    task.request_rpc_id = 43
    task.request_type = "input"
    task.request_payload = {
        "preview": "Choose one",
        "questions": [
            {
                "id": "q1",
                "header": "Choice",
                "question": "Choose one",
                "options": [{"label": "yes", "description": "Continue"}],
            }
        ],
    }
    task.request_schema = None

    result = json.loads(dispatch_tool_action(
        "task",
        session,
        "show_pending",
        {"task_id": "task-1"},
    ))

    assert result == {
        "ok": True,
        "task_id": "task-1",
        "pending": {
            "type": "input",
            "rpc_id": 43,
            "message": "Choose one",
            "payload": {
                "preview": "Choose one",
                "questions": [
                    {
                        "id": "q1",
                        "header": "Choice",
                        "question": "Choose one",
                        "options": [{"label": "yes", "description": "Continue"}],
                    }
                ],
            },
            "schema": None,
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
    commands.handle_slash("steer task-1 focus tests")
    commands.handle_slash("stop task-1")
    commands.handle_slash("answer task-1 yes")
    commands.handle_slash("pending task-1")
    commands.handle_slash("archive thread-1")
    commands.handle_slash("archive --all")
    commands.handle_slash("remove task-1")
    commands.handle_slash("remove --all")

    assert [tool_name for tool_name, _ in calls] == [
        "codex_approval",
        "codex_approval",
        "codex_action",
        "codex_action",
        "codex_action",
        "codex_action",
        "codex_action",
        "codex_tasks",
        "codex_tasks",
        "codex_tasks",
        "codex_remove",
        "codex_remove",
    ]
    assert calls[7] == (
        "codex_tasks",
        {"action": "show_pending", "task_id": "task-1"},
    )
    assert calls[8] == (
        "codex_tasks",
        {"action": "archive", "target": "thread-1"},
    )
    assert calls[9] == (
        "codex_tasks",
        {"action": "archive", "target": "allthreads"},
    )
    assert calls[10] == (
        "codex_remove",
        {"task_id": "task-1"},
    )
    assert calls[11] == (
        "codex_remove",
        {"all": True},
    )
    assert calls[4] == (
        "codex_action",
        {"action": "steer", "task_id": "task-1", "message": "focus tests"},
    )
    assert calls[5] == (
        "codex_action",
        {"action": "stop", "task_id": "task-1"},
    )
