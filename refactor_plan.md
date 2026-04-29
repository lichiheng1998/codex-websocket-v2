# Refactor: Session-per-Connection Architecture

## 1. 背景与目标

### 当前问题

`CodexBridge` 是进程级单例，所有 hermes session 共享：
- 一条 WebSocket 连接
- 一个 `_task_map` / `_threads` / `_pending_approvals` 全局 dict
- 一份 `_default_model` / `_plan_enabled` / `_verbose_enabled` 配置

后果：
- 多 session 并发时，model/mode/verbose 互相覆盖
- 不同 session 启动的 task 通知路由到 `_threads[thread_id].target`，最后写入者赢，其他 session 收不到通知
- session 关闭无法干净地清理自己的 task（共享全局状态）

### 设计目标

1. **每个 hermes session 拥有独立的 `CodexSession` 实例**，持有自己的 WebSocket 连接、event loop、task 列表、配置
2. **app-server 进程仍然共用**（只一个子进程，通过引用计数管理）
3. **多 session 可同时 `thread/resume` 同一 thread_id**，各自独立收通知
4. **session 关闭只关 WebSocket**，task 在 app-server 上保留，下次可以 revive

---

## 2. 模块拆分

```
codex_websocket/
├── server_manager.py     [新增] CodexServerManager — app-server 进程引用计数
├── session.py            [新增] CodexSession — 每个 hermes session 一个实例
├── session_registry.py   [新增] 全局 _sessions 表 + 增删查
├── bridge.py             [大改] 精简为纯连接层（WS + event loop + RPC plumbing）
├── handlers.py           [改]   MessageHandler 不再注入全局 dict，持有 session 引用
├── state.py              [改]   删除 _PendingThread/_PendingInput/_PendingApproval/
│                                 _PendingElicitation；新增 Task
├── commands.py           [改]   接收 session_id+platform，从 registry 查 session
├── tools.py              [改]   同上
├── policies.py           [不变]
├── provider.py           [不变]
├── wire.py               [不变]
├── notify.py             [不变]
└── utils.py              [不变]
```

---

## 3. 数据结构

### 3.1 Task（state.py）

替代旧的 `_PendingThread` + `_PendingApproval` + `_PendingInput`：

```python
@dataclass
class Task:
    task_id: str            # 本地短 ID
    thread_id: str          # codex server thread UUID
    cwd: str
    sandbox_policy: str
    approval_policy: str

    # 当前挂起的 server→client request（同一时刻最多一个）
    request_rpc_id: Any = None              # JSON-RPC id，用于发响应
    request_type: str = None                # "command" | "elicitation" | "input"
    request_payload: Dict[str, Any] = None  # 缓存原始 params，slash command 显示用
```

> `request_type` 决定响应的 wire 格式（在 `CodexSession.approve_task` / `input_task` 里 switch）：
> - `"command"` → `{"decision": "accept"|"decline"}`（commandExecution / fileChange / permissions 共用）
> - `"elicitation"` → `{"action": "accept"|"decline", "content": None}`
> - `"input"` → `{"responses": [...]}`

### 3.2 TaskTarget（state.py，不变）

```python
@dataclass
class TaskTarget:
    platform: str = ""
    chat_id: str = ""
    thread_id: str = ""
```

---

## 4. 类设计

### 4.1 CodexServerManager（server_manager.py，新增）

进程级单例，管理 app-server 子进程的生命周期。

```python
class CodexServerManager:
    _instance: ClassVar["CodexServerManager"] = None
    _lock: ClassVar[Lock] = Lock()

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self._log_file = None
        self._ref_count = 0
        self._start_lock = Lock()

    @classmethod
    def instance(cls) -> "CodexServerManager"

    def acquire(self) -> Result:
        """
        引用计数 +1。如果是第一个 session，spawn codex app-server 子进程。
        返回 {ok, port}。
        """

    def release(self) -> None:
        """
        引用计数 -1。降到 0 时 terminate 子进程并关闭日志文件。
        """
```

迁移自 `bridge.py` 的：`_spawn_server`（含 port pick、subprocess.Popen、port probe loop）、`shutdown` 中关进程那段。

---

### 4.2 CodexBridge（bridge.py，精简）

每个 `CodexSession` 持有一个 bridge 实例。bridge 只负责：
- 启动并持有自己的 event loop 线程
- 维护一条 WebSocket 连接
- RPC 请求/响应配对（`_pending_rpc`）
- 把入站帧交给 `MessageHandler`

```python
class CodexBridge:
    def __init__(
        self,
        port: int,                          # 来自 CodexServerManager
        session: "CodexSession",            # 反向引用，构造 MessageHandler 时传入
    ) -> None:
        self.port = port
        self.session = session
        self.ws = None
        self.loop: Optional[AbstractEventLoop] = None
        self.loop_thread: Optional[Thread] = None
        self._pending_rpc: Dict[int, Future] = {}
        self._next_id = 1
        self._id_lock = Lock()
        self._handler: Optional[MessageHandler] = None

    # 公共方法
    def connect(self) -> Result:
        """启动 loop 线程，建 WS，做 initialize 握手，启动 _reader_loop。"""

    def disconnect(self) -> None:
        """关 WS，停 loop 线程。不关 app-server 子进程。"""

    async def rpc(self, method, params, timeout) -> Result:
        """同现在 _rpc，唯一变化：从 self._pending_rpc 拿 Future。"""

    async def ws_send(self, payload) -> Result:
        """同现在 _ws_send。"""

    def run_sync(self, coro, timeout) -> Result:
        """同现在 _run_sync。"""

    # 内部
    def _next_rpc_id(self) -> int
    async def _reader_loop(self) -> None:
        """收帧 → self._handler.dispatch(msg)"""
```

迁移自现在 `bridge.py` 的：`_start_loop_thread`、`_connect_and_initialize`、`_close_ws`、`_rpc`、`_ws_send`、`_run_sync`、`_reader_loop`、`_next_rpc_id`。

---

### 4.3 MessageHandler（handlers.py，改）

只做帧识别和分发，业务逻辑（task 状态、通知）回调到 `CodexSession`。

```python
class MessageHandler:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    async def dispatch(self, raw: dict) -> None:
        kind, parsed, _ = wire.parse_incoming(raw)
        match kind:
            case "response":
                self._resolve_rpc(parsed.id.root, result=parsed.result)
            case "error":
                self._resolve_rpc(parsed.id.root, error=parsed.error)
            case "request":
                await self.session.on_server_request(parsed)
            case "notification":
                await self.session.on_server_notification(parsed)

    def _resolve_rpc(self, rpc_id, *, result=None, error=None) -> None:
        # 直接操作 self.session.bridge._pending_rpc
        # str/int id 互转逻辑保留（既有 bug fix）
```

> 现有 `handlers.py` 里所有 `_handle_*_approval`、`_on_item_completed`、`_on_turn_completed` 等格式化逻辑全部移到 `CodexSession`，因为它们需要访问 task / target / verbose 状态。

---

### 4.4 CodexSession（session.py，新增 — 核心）

```python
class CodexSession:
    def __init__(
        self,
        session_id: str,
        platform: str,
        target: TaskTarget,
    ) -> None:
        self.session_id = session_id
        self.platform = platform
        self.target = target

        self.default_model: str = DEFAULT_MODEL
        self.mode: str = "default"          # "plan" | "default"
        self.verbose: bool = False
        self.tasks: Dict[str, Task] = {}    # task_id → Task

        self._provider: ProviderInfo = ProviderInfo()
        self._server = CodexServerManager.instance()
        self.bridge: Optional[CodexBridge] = None
        self._ready = Event()
        self._start_lock = Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def ensure_started(self) -> Result:
        """幂等：acquire 进程 → 建 bridge → connect → sync config。"""

    def shutdown(self) -> None:
        """关 bridge（WS + loop），release 进程引用。task 保留在 app-server 上。"""

    # ── Task 操作 ─────────────────────────────────────────────────────────

    def start_task(self, *, cwd, prompt, approval_policy, sandbox_policy,
                   base_instructions=None) -> Result
    def send_reply(self, task_id, message) -> Result
    def remove_task(self, task_id) -> Result
    def revive_task(self, thread_id, *, sandbox_policy, approval_policy) -> Result
    def list_tasks(self) -> Result
    def list_threads(self, *, limit=None) -> Result   # 服务器全局，不分 session
    def archive_all_threads(self) -> Result

    # ── Approval / Input 解析 ─────────────────────────────────────────────

    def approve_task(self, task_id, decision: str) -> Result:
        """
        从 self.tasks[task_id] 取 request_rpc_id / request_type，
        按 type 构造响应 payload，发 ws response，清除 request_*。
        """

    def input_task(self, task_id, answer) -> Result:
        """
        同 approve_task，但 type 必须是 "input"，payload = {"responses": [...]}。
        """

    def list_pending_requests(self) -> list:
        """遍历 self.tasks，返回挂起 request 的 task 列表。"""

    # ── 设置（per-session） ───────────────────────────────────────────────

    def get_default_model(self) -> str
    def set_default_model(self, model) -> Result
    def list_models(self, *, include_hidden=False, limit=None) -> Result
    def get_mode(self) -> str
    def set_mode(self, mode: str) -> Result        # "plan" | "default"
    def get_verbose(self) -> bool
    def set_verbose(self, on: bool) -> Result
    def get_status(self) -> Result

    # ── MessageHandler 回调（由 bridge._reader_loop 间接触发）────────────

    async def on_server_request(self, req) -> None:
        """
        对应现 handlers.py:_on_server_request。
        识别 method，从 params 取 thread_id 找 task，写：
          task.request_rpc_id = req.id.root
          task.request_type   = "command" | "elicitation" | "input"
          task.request_payload = params (用于 slash command 显示)
        然后调 notify_user(self.target, 格式化文本)。
        """

    async def on_server_notification(self, notif) -> None:
        """
        对应现 handlers.py:_on_server_notification + _on_item_completed +
        _on_turn_completed 等所有格式化。访问 self.verbose / self.target /
        self.tasks[thread_id]。
        """
```

---

### 4.5 SessionRegistry（session_registry.py，新增）

```python
_sessions: Dict[str, CodexSession] = {}     # key = f"{platform}:{session_id}"
_lock = Lock()

def _key(platform: str, session_id: str) -> str:
    return f"{platform}:{session_id}"

def get_or_create(session_id, platform, target) -> CodexSession
def get(session_id, platform) -> Optional[CodexSession]
def remove(session_id, platform) -> Optional[CodexSession]
def all_sessions() -> List[CodexSession]    # 用于全局 shutdown
```

---

## 5. 生命周期管理（不用 hook）

### 5.1 创建：懒初始化

不注册 `on_session_start` hook。CodexSession 的创建延迟到第一次 tool / slash
调用——`resolve_current_session()` 找不到时才 `get_or_create`。

理由：
- `on_session_start` 触发时 contextvars 还没设，无法用统一的 `resolve_current_session()` 路径
- 大量 hermes session 不会用 codex，提前创建是浪费

### 5.2 清理：用 atexit 注册进程退出钩子

hermes 的 `VALID_HOOKS` 里**没有进程退出 hook**——只有 session 级别的
`on_session_start/end/finalize/reset`，且 `on_session_finalize` 给的 kwargs
里没有 `session_key`，与我们 registry 的 key 对不上。

CodexSession 的常驻成本只是一条 idle WS 连接，进程级 app-server 子进程也只一个。
但 app-server 是 hermes 的子进程，进程退出时若不显式 terminate 可能留下野进程。

用 Python 标准库的 `atexit` 钩子做兜底清理：

```python
import atexit

def register(ctx) -> None:
    ctx.register_command("codex", handler=handle_slash, ...)
    ctx.register_tool("codex_task", ..., handler=tools.codex_task)
    ctx.register_tool("codex_revive", ..., handler=tools.codex_revive)

    def _shutdown_all():
        for session in registry.all_sessions():
            try:
                session.shutdown()      # 关 WS + release server ref
            except Exception:
                pass
        registry.clear()
        # registry.clear() 后 ref_count 应该是 0，CodexServerManager 自动 terminate

    atexit.register(_shutdown_all)
```

> 不再用 `on_session_finalize`——那个 hook 在 `/reset` 时会触发，
> 但我们没法把 hook 给的 `session_id` 映射回 `session_key`。
> 用户主动 `/reset` 不会清理对应的 CodexSession，会有冗余 WS 连接残留，
> 但功能上没问题（下次操作会创建新 CodexSession，旧的至多占着一条 idle 连接，
> 直到进程退出 atexit 清理）。如果以后 hermes 提供进程级 shutdown hook 再迁移。

---

## 6. Slash Command / Tool 路由（commands.py、tools.py）

### 6.1 Hermes 上下文读取机制

hermes 的 slash command handler 签名只有 `(raw_args: str)`，tool handler 是
`(args: dict, **kwargs)`——**两者都不通过参数传 session_id**。
但 hermes 通过 contextvars 暴露 session 信息，handler 可以直接读：

- `tools.approval.get_current_session_key()` — 返回 hermes 的 session_key
- `gateway.session_context.get_session_env("HERMES_SESSION_*")` — 读 platform/chat_id 等

contextvars 是 task-local（gateway 用 `set_session_vars()` 在 message handler 入口处设，
出口处 clear），同一进程多个并发消息互不串。

> 现有代码 `utils.py:get_session_context()` 已经做了这件事，只是返回了
> `(session_key, TaskTarget)` 二元组。重构后改成返回完整的 `CodexSession`。

### 6.2 统一的 session 解析函数

```python
# session_registry.py
from tools.approval import get_current_session_key
from gateway.session_context import get_session_env
from .state import TaskTarget

def resolve_current_session() -> CodexSession:
    """从 hermes contextvars 解析当前 session_key，找/建 CodexSession。"""
    session_key = get_current_session_key()
    target = TaskTarget(
        platform=get_session_env("HERMES_SESSION_PLATFORM", ""),
        chat_id=get_session_env("HERMES_SESSION_CHAT_ID", ""),
        thread_id=get_session_env("HERMES_SESSION_THREAD_ID", ""),
    )
    return get_or_create(session_key, target)
```

> registry 的 key 直接用 `session_key`（hermes 内部已经把 platform + chat_id
> 编码进去了），无需额外的 platform 前缀。

### 6.3 调用方

```python
# commands.py
def handle_slash(raw_args: str) -> str:
    session = resolve_current_session()
    # 所有 _cmd_* 函数接受 session 参数

# tools.py
def codex_task(args: dict, **kwargs) -> str:
    session = resolve_current_session()
    return session.start_task(cwd=args["cwd"], prompt=args["prompt"], ...)

def codex_revive(args: dict, **kwargs) -> str:
    session = resolve_current_session()
    return session.revive_task(args["thread_id"], ...)
```

---

## 7. 关键流程

### 7.1 第一个 session 启动

```
on_session_start(session_id="abc", platform="telegram")
  → registry.get_or_create() 创建 CodexSession，但不 ensure_started
  
首次 codex_task tool call
  → session.ensure_started()
    → CodexServerManager.acquire()    # ref_count: 0 → 1, spawn codex app-server, return port
    → bridge = CodexBridge(port, session)
    → bridge.connect()                 # 起 loop 线程，建 WS，initialize 握手
    → sync_default_model               # 拉 config/read，写 self.default_model + self._provider
    → self._ready.set()
  → session.start_task(...)
```

### 7.2 第二个 session 启动

```
on_session_start(session_id="xyz", platform="discord")
  → 新 CodexSession，独立的 bridge / loop / WS

首次 codex_task tool call
  → session.ensure_started()
    → CodexServerManager.acquire()    # ref_count: 1 → 2, 进程已存在，直接返回同一个 port
    → 新 WS 连接到同一个 app-server
    → 独立 initialize 握手
```

### 7.3 用户对同一 thread 操作（跨 session）

```
session A: start_task → thread_id = T1, A.tasks[task_a1].thread_id = T1
session B: revive_task(T1) → B.tasks[task_b1].thread_id = T1, app-server 把 T1 的通知推给 B 的 WS

之后 T1 上的 turn/started 等通知会同时到 A 和 B 两条 WS 连接，
各自路由到自己的 target。
```

### 7.4 Approval 流程（per-session）

```
codex 发 item/commandExecution/requestApproval(rpc_id=42, threadId=T1) 到 session A 的 WS
  → A.bridge._reader_loop → A.bridge._handler.dispatch
  → A.session.on_server_request(req)
    → 找 task: A.tasks lookup by thread_id == T1
    → task.request_rpc_id = 42
    → task.request_type   = "command"
    → task.request_payload = params
    → notify(A.target, "⚠️ Codex task ... requests ...")

用户输入 /codex approve <task_id>
  → handle_slash → session A → session.approve_task(task_id, "accept")
    → 取 task.request_rpc_id 和 request_type
    → 构造 payload {"decision": "accept"}
    → bridge.run_sync(bridge.ws_send(json.dumps({jsonrpc, id: 42, result: payload})))
    → 清空 task.request_*
```

### 7.5 Session 结束

```
on_session_finalize(session_id="abc", platform="telegram")
  → registry.remove → 拿到 CodexSession
  → session.shutdown()
    → bridge.disconnect()             # close WS, stop loop, join loop_thread
    → CodexServerManager.release()    # ref_count: 2 → 1, 进程还活着
    
当最后一个 session shutdown:
  → CodexServerManager.release()      # ref_count: 1 → 0, terminate codex app-server
```

---

## 8. 与现有架构的差异

| 项 | 现状 | 重构后 |
|---|---|---|
| `CodexBridge` 实例数量 | 进程级单例 | 每个 hermes session 一个 |
| WebSocket 连接 | 全局共享 1 条 | 每个 session 独立 1 条 |
| event loop 线程 | 全局共享 | 每个 session 独立 |
| app-server 子进程 | 第一次 ensure_started 隐式启动，无人 release | `CodexServerManager` 显式引用计数管理 |
| `_pending_approvals` / `_pending_inputs` | 全局 dict（task_id → _Pending*） | `Task` 字段 `request_rpc_id` / `request_type` / `request_payload` |
| `_PendingApproval` / `_PendingElicitation` 类 | 用继承 + `to_response_payload` 多态 | 删除，按 `request_type` switch 一个函数 |
| `_threads` / `_task_map` | 全局 dict | `CodexSession.tasks: Dict[task_id, Task]`（task_id → Task，Task 自带 thread_id） |
| `MessageHandler` 状态 | 注入 5 个全局 dict | 持有 `session` 引用 |
| `MessageHandler` 业务逻辑 | 格式化、stash、route 通知都在 handler 里 | 只做 dispatch，业务回调到 session |
| `default_model` / `plan_enabled` / `verbose_enabled` | 全局 | per-session 字段 |
| `session_key` 字段 | 存而不用 | 删除（被 session_id 替代） |

---

## 9. 实施顺序（建议）

1. **state.py**：定义 `Task`，删除 `_PendingThread/_PendingInput/_PendingApproval/_PendingElicitation`
2. **server_manager.py**：实现 `CodexServerManager`，从 `bridge.py` 抽出 `_spawn_server` 逻辑
3. **bridge.py**：精简为纯连接层，构造时接收 `port` + `session` 引用
4. **handlers.py**：精简 `MessageHandler` 为分发器，业务逻辑迁出
5. **session.py**：实现 `CodexSession`，迁入 task 操作 + 通知格式化 + per-session 配置
6. **session_registry.py**：实现 registry
7. **__init__.py**：去掉 `on_session_finalize` hook，改 `atexit.register(_shutdown_all)`
8. **commands.py / tools.py**：改路由，所有入口先调 `resolve_current_session()`
9. **测试**：现有测试基于全局 bridge，重写为 per-session fixture——构造 CodexSession 时
   绕过 contextvars（直接传 session_key + target），保持纯单元测试可运行

---

## 10. 已解决 / 已知限制

### 已解决

- ✅ slash command 拿 session：`tools.approval.get_current_session_key()` + contextvars
- ✅ tool handler 拿 session：同上（gateway 在调 tool 前已经 `_set_session_env`)
- ✅ registry key：直接用 hermes 的 `session_key`（platform 已编码）
- ✅ session 创建：懒初始化，第一次 tool/slash 调用时 `get_or_create`
- ✅ 进程退出清理：用 `atexit` 注册（hermes 没有进程级 shutdown hook）

### 已知限制

- 用户 `/reset` 后旧 CodexSession 不会立即清理。下次操作会创建新 CodexSession，
  旧 session 占一条 idle WS 连接直到进程退出 atexit 清理。功能正确，仅资源略浪费。
- 如果以后 hermes 提供进程级 shutdown hook，可以替换 `atexit`。