# Subscriber/Event 架构详细实现计划

## 1. 目标

当前项目的 inbound WebSocket message 处理链路大致是：

```text
CodexBridge._reader_loop
  -> MessageHandler.dispatch(raw)
  -> MessageHandler 内部按 response/request/notification 分支
  -> 直接调用 session.stash_request / session.notify / bridge.ws_send
```

目标是改成 subscriber design pattern：

```text
CodexBridge._reader_loop
  -> MessageHandler.dispatch(raw)
  -> EventFactory.from_raw(raw)
  -> EventBus.publish(event)
  -> 对应 Subscriber.handle(event)
```

核心原则：

- `MessageHandler` 只负责把 raw frame 转成 event 并发布，不再承载业务逻辑。
- 不同 request/response message 使用不同 subscriber。
- notification 类 message 可以由一个 `NotificationSubscriber` 统一处理，内部再用 item handler registry 分派。
- `CodexSession` 仍然是 session/task state owner，不把 state 分散到 event bus 里。
- 外部接口不变：`/codex approve`、`/codex deny`、`/codex answer`、`codex_tasks` 等行为保持兼容。

## 2. 新增模块设计

### 2.1 `codex_websocket_v2/events.py`

定义所有事件 dataclass。第一版建议使用 `dataclasses.dataclass`，不使用 pydantic，避免高频 notification 产生额外序列化成本。

建议事件结构：

```python
from dataclasses import dataclass
from typing import Any, Optional

@dataclass
class BaseEvent:
    session: Any
    raw: dict

@dataclass
class UnknownFrameEvent(BaseEvent):
    parsed: Any = None
    reason: str = ""

@dataclass
class RpcResponseEvent(BaseEvent):
    rpc_id: Any = None
    result: Any = None

@dataclass
class RpcErrorEvent(BaseEvent):
    rpc_id: Any = None
    error: Any = None

@dataclass
class ServerRequestEvent(BaseEvent):
    method: str = ""
    rpc_id: Any = None
    params: Any = None

@dataclass
class ApprovalRequestedEvent(ServerRequestEvent):
    approval_kind: str = ""
    thread_id: Optional[str] = None
    task: Any = None
    task_id: str = "?"

@dataclass
class UserInputRequestedEvent(ServerRequestEvent):
    thread_id: Optional[str] = None
    task: Any = None
    task_id: str = "?"

@dataclass
class ElicitationRequestedEvent(ServerRequestEvent):
    thread_id: Optional[str] = None
    task: Any = None
    task_id: str = "?"

@dataclass
class UnknownRequestEvent(ServerRequestEvent):
    pass

@dataclass
class ServerNotificationEvent(BaseEvent):
    method: str = ""
    params: Any = None

@dataclass
class ItemCompletedEvent(ServerNotificationEvent):
    thread_id: Optional[str] = None
    task: Any = None
    item: Any = None
    item_type: str = ""

@dataclass
class TurnCompletedEvent(ServerNotificationEvent):
    thread_id: Optional[str] = None
    task: Any = None
    turn: Any = None
    status: str = ""

@dataclass
class UnknownNotificationEvent(ServerNotificationEvent):
    pass
```

说明：

- `BaseEvent.session` 指向当前 `CodexSession`，subscriber 不需要额外闭包也能访问 session。
- `raw` 保留原始 frame，方便 debug 和 fallback。
- request 类 event 保留 `rpc_id`，因为 unknown request 或 approval response 需要 JSON-RPC response id。
- `ApprovalRequestedEvent.approval_kind` 固定使用当前 `approval_handler.py` 中的 kind：
  - `commandExecution`
  - `fileChange`
  - `permissions`
  - `legacyExecCommand`
  - `legacyApplyPatch`

### 2.2 `codex_websocket_v2/event_bus.py`

实现轻量事件总线。

建议接口：

```python
from collections import defaultdict
from typing import Any, Awaitable, Callable, DefaultDict, Type

Subscriber = Callable[[Any], Awaitable[bool | None]]

class EventBus:
    def __init__(self) -> None:
        self._subscribers: DefaultDict[type, list[Subscriber]] = defaultdict(list)

    def subscribe(self, event_type: type, subscriber: Subscriber) -> None:
        self._subscribers[event_type].append(subscriber)

    async def publish(self, event: Any) -> bool:
        handled = False
        for event_type in type(event).__mro__:
            for subscriber in self._subscribers.get(event_type, []):
                consumed = await subscriber(event)
                handled = True
                if consumed is True:
                    return True
        return handled
```

设计选择：

- 默认串行执行 subscriber，避免同一个 `Task` 的 pending request、last item buffer 出现竞态。
- 允许按父类订阅，例如订阅 `ServerRequestEvent` 做通用日志。
- subscriber 返回 `True` 表示 consume，不再继续传播。
- subscriber 返回 `False` 或 `None` 表示已执行但允许后续 subscriber 继续处理。
- 第一版不做并发 fan-out，不引入 priority，保持行为可预测。

### 2.3 `codex_websocket_v2/event_factory.py`

负责从 raw frame 生成 typed event。

职责：

- 调用 `wire.parse_incoming(raw)`。
- response -> `RpcResponseEvent`。
- error -> `RpcErrorEvent`。
- request -> 根据 method 转成具体 request event。
- notification -> 根据 method 和 item type 转成具体 notification event。
- 无法识别 -> `UnknownFrameEvent` / `UnknownRequestEvent` / `UnknownNotificationEvent`。

建议关键逻辑：

```python
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "commandExecution",
    "item/fileChange/requestApproval": "fileChange",
    "item/permissions/requestApproval": "permissions",
    "execCommandApproval": "legacyExecCommand",
    "applyPatchApproval": "legacyApplyPatch",
}

class EventFactory:
    def __init__(self, session):
        self.session = session

    def from_raw(self, raw: dict):
        kind, parsed, raw = wire.parse_incoming(raw)
        if kind == "response":
            return RpcResponseEvent(self.session, raw, parsed.id.root, parsed.result)
        if kind == "error":
            return RpcErrorEvent(self.session, raw, parsed.id.root, parsed.error)
        if kind == "request":
            return self._request_event(parsed, raw)
        if kind == "notification":
            return self._notification_event(parsed, raw)
        return UnknownFrameEvent(self.session, raw, parsed, "unparseable")
```

Request classification：

- approval methods -> `ApprovalRequestedEvent`
- `item/tool/requestUserInput` -> `UserInputRequestedEvent`
- `mcpServer/elicitation/request` -> `ElicitationRequestedEvent`
- 其它 request -> `UnknownRequestEvent`

Notification classification：

- `item/completed` -> `ItemCompletedEvent`
- `turn/completed` -> `TurnCompletedEvent`
- 其它 notification -> `UnknownNotificationEvent`

Factory 也负责查 task：

```python
def _task_meta(self, thread_id):
    task = self.session.task_for_thread(thread_id) if thread_id else None
    return task, task.task_id if task else "?"
```

## 3. Subscriber 设计

### 3.1 `codex_websocket_v2/subscribers/rpc.py`

负责 response/error。

#### `RpcResponseSubscriber`

处理 `RpcResponseEvent`：

- 根据 `event.rpc_id` 查 `session.bridge._pending_rpc`。
- 兼容 int/string id。
- 找到 future 且未 done 时 `fut.set_result(event.result)`。
- 返回 `True`。

#### `RpcErrorSubscriber`

处理 `RpcErrorEvent`：

- 根据 `event.rpc_id` 查 pending future。
- 找到后 `fut.set_exception(RuntimeError(...))`。
- 返回 `True`。

迁移来源：

- 当前 `MessageHandler._resolve_rpc`。

### 3.2 `codex_websocket_v2/subscribers/approval.py`

负责 approval request。

可以复用当前 `ApprovalRequestHandler`，但将入口改成 subscriber：

```python
class ApprovalRequestSubscriber:
    def __init__(self, session):
        self.session = session

    async def __call__(self, event: ApprovalRequestedEvent) -> bool:
        ...
        return True
```

处理内容：

- `commandExecution` / `legacyExecCommand`
  - 格式化 command preview。
  - `session.stash_request(task, rpc_id, "command", payload)`。
  - payload 写入 `cmd_type = event.approval_kind`。
  - `session.notify(...)`。
- `fileChange`
  - 格式化 write permission 文案。
  - stash `cmd_type = "fileChange"`。
- `permissions`
  - 保存 `permissions = jsonable(params.permissions)`。
  - stash `cmd_type = "permissions"`。
- `legacyApplyPatch`
  - 格式化文件变更列表。
  - stash `cmd_type = "legacyApplyPatch"`。

保留 `build_approval_response(...)` 在同模块或拆到 `approval_response.py` 均可。建议第一版仍放 `approval_handler.py`，避免文件过多。

### 3.3 `codex_websocket_v2/subscribers/input.py`

处理 `UserInputRequestedEvent`。

迁移来源：

- 当前 `MessageHandler._handle_user_input_request`。

行为：

- 如果找不到 task：发送 JSON-RPC error 或 decline response，保持当前行为。
- 找到 task：
  - 读取 `params.questions`。
  - stash `request_type="input"`，payload 保存 questions。
  - notify 用户 `/codex answer <task_id> ...`。
- 返回 `True`。

### 3.4 `codex_websocket_v2/subscribers/elicitation.py`

处理 `ElicitationRequestedEvent`。

迁移来源：

- 当前 `MessageHandler._handle_elicitation_request`。

行为：

- 处理 `mode=url` 和 `mode=form` 两类展示。
- stash `request_type="elicitation"`。
- 使用 approval footer 样式：`/codex approve`、`/codex deny`。
- 返回 `True`。

### 3.5 `codex_websocket_v2/subscribers/notification.py`

notification 类 response 可以写在一起，因此用一个 `NotificationSubscriber`。

职责：

- 处理 `ItemCompletedEvent`。
- 处理 `TurnCompletedEvent`。
- 内部用 registry 分派 item type，避免一个巨大 `if/elif`。

建议结构：

```python
class NotificationSubscriber:
    def __init__(self, session):
        self.session = session
        self.item_handlers = {
            "agentMessage": self._agent_message,
            "plan": self._plan,
            "commandExecution": self._command_execution,
            "fileChange": self._file_change,
            "webSearch": self._web_search,
            "enteredReviewMode": self._entered_review_mode,
            "exitedReviewMode": self._exited_review_mode,
            "contextCompaction": self._context_compaction,
        }

    async def __call__(self, event):
        if isinstance(event, ItemCompletedEvent):
            await self._item_completed(event)
            return True
        if isinstance(event, TurnCompletedEvent):
            await self._turn_completed(event)
            return True
        return False
```

迁移来源：

- 当前 `MessageHandler._on_server_notification`
- 当前 `MessageHandler._on_item_completed`
- 当前 `_on_agent_message`
- 当前 `_on_plan`
- 当前 `_on_command_execution`
- 当前 `_on_file_change`
- 当前 `_on_web_search`
- 当前 `_on_entered_review_mode`
- 当前 `_on_exited_review_mode`
- 当前 `_on_turn_completed`
- 当前 `_show_last_item`

保留现有 verbose 语义：

- `verbose == "on"`：即时展示所有支持 item。
- `verbose == "mid"`：展示重要 item。
- `verbose == "off"`：缓存 last item，在 turn completed 前 flush。

### 3.6 `codex_websocket_v2/subscribers/unhandled.py`

负责兜底。

#### `UnhandledRequestSubscriber`

处理 `UnknownRequestEvent`：

- 发送 JSON-RPC error：

```json
{
  "jsonrpc": "2.0",
  "id": rpc_id,
  "error": {"code": -32601, "message": "unhandled: <method>"}
}
```

- 返回 `True`。

#### `UnhandledNotificationSubscriber`

处理 `UnknownNotificationEvent`：

- debug log。
- 不 notify 用户。
- 返回 `True`。

#### `UnknownFrameSubscriber`

处理 `UnknownFrameEvent`：

- debug log raw frame。
- 返回 `True`。

## 4. Session 注册方式

在 `CodexSession.__init__` 里新增：

```python
from .event_bus import EventBus
from .subscribers import register_default_subscribers

self.event_bus = EventBus()
register_default_subscribers(self.event_bus, self)
```

新增 `codex_websocket_v2/subscribers/__init__.py`：

```python
def register_default_subscribers(bus, session):
    bus.subscribe(RpcResponseEvent, RpcResponseSubscriber(session))
    bus.subscribe(RpcErrorEvent, RpcErrorSubscriber(session))
    bus.subscribe(ApprovalRequestedEvent, ApprovalRequestSubscriber(session))
    bus.subscribe(UserInputRequestedEvent, UserInputRequestSubscriber(session))
    bus.subscribe(ElicitationRequestedEvent, ElicitationSubscriber(session))
    bus.subscribe(ItemCompletedEvent, NotificationSubscriber(session))
    bus.subscribe(TurnCompletedEvent, NotificationSubscriber(session))
    bus.subscribe(UnknownRequestEvent, UnhandledRequestSubscriber(session))
    bus.subscribe(UnknownNotificationEvent, UnhandledNotificationSubscriber(session))
    bus.subscribe(UnknownFrameEvent, UnknownFrameSubscriber(session))
```

注意：

- `NotificationSubscriber` 应该只实例化一次，然后订阅两个 event type：

```python
notification = NotificationSubscriber(session)
bus.subscribe(ItemCompletedEvent, notification)
bus.subscribe(TurnCompletedEvent, notification)
```

否则两个实例可能各自维护状态，后续如果引入内部缓存会有问题。

## 5. MessageHandler 收缩

最终 `MessageHandler` 应该变成：

```python
class MessageHandler:
    def __init__(self, session):
        self.session = session
        self.event_factory = EventFactory(session)

    async def dispatch(self, raw: dict) -> None:
        event = self.event_factory.from_raw(raw)
        handled = await self.session.event_bus.publish(event)
        if not handled:
            logger.debug("codex handler: event not handled: %r", event)
```

然后删除 `MessageHandler` 中的：

- `_resolve_rpc`
- `_on_server_request`
- `_handle_user_input_request`
- `_handle_elicitation_request`
- `_on_server_notification`
- `_on_item_completed`
- 所有 `_on_*` item formatter
- `_on_turn_completed`
- `_show_last_item`

## 6. 迁移顺序

### Step 1：加 event 基础设施，不改变现有 handler

新增：

- `events.py`
- `event_bus.py`
- `event_factory.py`

只写单元测试或手动断言，暂时不接入 `MessageHandler`。

验收：

- raw response 能转 `RpcResponseEvent`。
- raw approval request 能转 `ApprovalRequestedEvent`。
- raw item completed 能转 `ItemCompletedEvent`。

### Step 2：迁移 RPC response/error

新增 `RpcResponseSubscriber` / `RpcErrorSubscriber`。

修改 `MessageHandler.dispatch`：

- response/error 先走 event bus。
- request/notification 暂时走旧逻辑。

验收：

- `bridge.rpc(...)` 仍能收到 response。
- timeout 和 error 行为不变。

### Step 3：迁移 approval request

把现有 `ApprovalRequestHandler` 改为 `ApprovalRequestSubscriber`。

修改 `MessageHandler._on_server_request`：

- approval methods 走 event bus。
- 其它 request 暂时旧逻辑。

验收：

- `/codex approve`、`/codex approve --all`、`/codex deny` payload 不变。
- permissions approve 仍回传 requested permissions。

### Step 4：迁移 user input / elicitation / unknown request

新增：

- `UserInputRequestSubscriber`
- `ElicitationSubscriber`
- `UnhandledRequestSubscriber`

修改 request 全量走 event bus。

验收：

- `/codex answer` 仍可回复 pending input。
- MCP elicitation 仍可 approve/deny。
- unknown request 仍发送 `-32601`。

### Step 5：迁移 notification

新增 `NotificationSubscriber`。

修改 notification 全量走 event bus。

验收：

- agent message、plan、command execution、file change、web search 展示不变。
- verbose on/mid/off 行为不变。
- turn completed 前 flush last item。
- failed/interrupted/completed 文案不变。

### Step 6：删除旧 MessageHandler 业务逻辑

在所有 request/response/notification 都走 event bus 后，删除 `MessageHandler` 内旧方法。

验收：

- `handlers.py` 只保留 event dispatch shell。
- 没有 `_handle_*approval`、`_on_item_completed`、`_resolve_rpc` 残留。

## 7. 测试计划

### 7.1 EventFactory 测试

覆盖：

- JSON-RPC response -> `RpcResponseEvent`
- JSON-RPC error -> `RpcErrorEvent`
- `item/commandExecution/requestApproval` -> `ApprovalRequestedEvent(commandExecution)`
- `item/fileChange/requestApproval` -> `ApprovalRequestedEvent(fileChange)`
- `item/permissions/requestApproval` -> `ApprovalRequestedEvent(permissions)`
- `execCommandApproval` -> `ApprovalRequestedEvent(legacyExecCommand)`
- `applyPatchApproval` -> `ApprovalRequestedEvent(legacyApplyPatch)`
- `item/tool/requestUserInput` -> `UserInputRequestedEvent`
- `mcpServer/elicitation/request` -> `ElicitationRequestedEvent`
- `item/completed` -> `ItemCompletedEvent`
- `turn/completed` -> `TurnCompletedEvent`
- unknown request -> `UnknownRequestEvent`
- unknown notification -> `UnknownNotificationEvent`

### 7.2 EventBus 测试

覆盖：

- 按 event class 找到 subscriber。
- 按注册顺序执行多个 subscriber。
- subscriber 返回 `True` 后停止传播。
- subscriber 返回 `False/None` 时继续传播。
- 父类订阅可接收子类 event。

### 7.3 Subscriber 回归测试

覆盖：

- RPC subscriber 正确 resolve pending future。
- Approval subscriber stash payload 的 `cmd_type` 正确。
- Approval response builder 对 approve/deny/for_session 映射正确。
- Input subscriber stash `request_type="input"`。
- Elicitation subscriber stash `request_type="elicitation"`。
- Notification subscriber 对每种 item type 产生原有通知文本。
- Turn completed 对 `failed`、`interrupted`、`completed` 三种状态行为不变。

### 7.4 编译/静态检查

至少运行：

```bash
python -m py_compile codex_websocket_v2/*.py codex_websocket_v2/subscribers/*.py
```

如果项目后续有 pytest，则补：

```bash
pytest
```

## 8. 风险与注意事项

- 不要让多个 subscriber 并发修改同一个 `Task`，第一版必须串行 publish。
- `NotificationSubscriber` 如果订阅多个 event type，应复用同一个实例。
- `EventFactory` 不应该做通知格式化，只负责分类和补充 task/task_id metadata。
- `Subscriber` 不应该重新 parse raw frame，应该只消费 event 字段。
- `CodexSession.approve_task` 仍然是 slash command approval 的出口，不要把 approve/deny response 发送逻辑塞进 request subscriber。
- unknown request 必须继续返回 JSON-RPC error，否则 app-server 可能挂起等待 response。
- notification 不需要 response，未知 notification 只 log，不打扰用户。

## 9. 最终文件结构建议

```text
codex_websocket_v2/
  events.py
  event_bus.py
  event_factory.py
  handlers.py                  # only dispatch raw -> event bus
  approval_handler.py           # approval response builder, or kept as approval helpers
  subscribers/
    __init__.py                 # register_default_subscribers
    rpc.py
    approval.py
    input.py
    elicitation.py
    notification.py
    unhandled.py
```

如果想少建文件，第一版也可以用：

```text
codex_websocket_v2/
  events.py
  event_bus.py
  event_factory.py
  event_subscribers.py
```

但长期推荐 `subscribers/` 目录，因为 request、response、notification 的职责边界更清楚。

## 10. 推荐最终设计决策

- Request 类 message：不同类型使用不同 subscriber。
- Response/error 类 message：使用独立 RPC subscriber。
- Notification 类 message：使用一个 `NotificationSubscriber`，内部用 item handler registry。
- EventBus：串行、按类型分发、支持 consumed stop propagation。
- Event：dataclass typed event，不引入额外依赖。
- 外部 API：完全保持兼容。
