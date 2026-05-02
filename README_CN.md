<div align="center">

## [English](README.md) | [中文](README_CN.md)

</div>

# codex-websocket-v2

一个 [hermes](https://github.com/lichi/hermes-agent) 插件，通过 WebSocket 将 hermes 会话桥接到 [OpenAI Codex CLI](https://github.com/openai/codex) app-server，让用户可以通过任意聊天平台（微信、Telegram、Discord 等）将编程任务委托给 Codex。

---

## 架构

```
hermes session
    │
    ├── Tools / Slash commands          ← 用户侧 API
    │
    ├── CodexSession（每 session 一个）  ← 独立 WS + event loop + task 表 + 配置
    │       │
    │       └── CodexBridge             ← WebSocket 上的 JSON-RPC
    │               │
    ├── CodexServerManager（进程级）     ← 引用计数管理 codex app-server 子进程
    │
    └── codex app-server                ← 本地子进程，跨 session 共享
```

**核心设计：**

- **per-session 隔离** — 每个 hermes session 拥有独立的 `CodexSession`（独立 WebSocket 连接、asyncio event loop、task 表和配置），多用户/多 session 互不干扰。
- **共享 app-server** — 通过引用计数共享一个 `codex app-server` 子进程，第一个 session 启动时 spawn，最后一个 session 退出时关闭。
- **跨 event loop 通知** — Codex bridge 运行在自己的 event loop 线程，hermes 平台（如微信的 aiohttp）绑定主 loop，`notify.py` 通过 `run_coroutine_threadsafe` 跨 loop 调度。

---

## 依赖

- Python ≥ 3.10
- [hermes-agent](https://github.com/lichi/hermes-agent)
- [Codex CLI](https://github.com/openai/codex)（`codex` 在 `$PATH` 中可用）
- `websockets >= 11.0`
- `pydantic >= 2.0`

---

## 安装

```bash
hermes plugins install lichiheng1998/codex-websocket-v2
```

然后安装 Python 依赖并确认 Codex CLI 可用：

```bash
pip install websockets>=11.0 pydantic>=2.0
codex --version
```

下次启动 hermes 时插件会自动注册。

---

## 工具（Tools）

插件加载后，以下工具对 LLM agent 可用。

### `codex_task`
在后台 Codex 线程中启动一个编程任务，立即返回 `task_id`；进度和结果以独立消息推送到聊天。

| 参数 | 类型 | 说明 |
|---|---|---|
| `cwd` | string（必填） | 项目目录的绝对路径 |
| `prompt` | string（必填） | 任务描述 |
| `model` | string | task 模型；不填则从 session default 拷贝 |
| `plan` | enum | `on` · `off`；不填则从 session default 拷贝 |
| `sandbox_policy` | enum | `read-only` · `workspace-write` · `danger-full-access`；不填则从 session default 拷贝 |
| `approval_policy` | enum | `on-request` · `on-failure` · `never` · `untrusted`；不填则从 session default 拷贝 |
| `base_instructions` | string | 可选的前置指令 |

Task 创建时会固定自己的 `model`、`plan`、`sandbox_policy`、`approval_policy`。后续 reply 使用 task 自己的值；修改 session default 只影响未来 task。

### `codex_tasks`
列出或归档当前 session 的 task 和 thread。

| Action | 参数 | 说明 |
|---|---|---|
| `list` | `show_threads` | 列出 session task（或服务器全部 thread） |
| `pending_schema` | `task_id` | 返回 task 当前挂起 MCP elicitation 的 schema（如果有） |
| `archive` | `target` | 归档指定 task（`task_id`）、当前 session 全部 task（`all`）或服务器全部 thread（`allthreads`）。若 thread 被其他 session 持有则拒绝。 |

### `codex_action`
向已有 task 发送后续动作。

| Action | 参数 | 说明 |
|---|---|---|
| `reply` | `task_id`, `message` | 向运行中的 task 发送后续 turn 消息 |
| `answer` | `task_id`, `responses[]` 或 `answers[][]` | 回答 `requestUserInput`；一个问题多个答案时用 `answers[][]` |
| `respond` | `task_id`, `content` | 用符合 schema 的表单数据响应挂起的 elicitation |

### `codex_approval`
处理挂起的审批类请求。

| Action | 参数 | 说明 |
|---|---|---|
| `approve` | `task_id`, `for_session` | 批准挂起的命令，或用空 content `{}` 接受挂起的 elicitation。`for_session=true` 仅对命令审批发送 `acceptForSession` |
| `deny` | `task_id` | 拒绝挂起的命令，或用空 content `{}` 拒绝挂起的 elicitation |

对于带 options 的 `requestUserInput` 问题，请用通知里显示的 option label 原样回答。

> **注意：** `turn/completed` 只代表当前 turn 结束，thread 仍然存活。用 `reply` 继续对话。仅在 thread 不在当前 session 中被追踪时（如 gateway 重启后）才使用 `codex_revive`。

### `codex_revive`
将上一个 session 的 thread（如 gateway 重启后丢失的）恢复到当前 session 的 task 表中。若 thread 当前被其他活跃 session 持有则拒绝。

### `codex_models`
列出共享模型，或查看/设置 default/task 模型（`list` · `get` · `set`）。`get`/`set` 不传 `task_id` 操作 session default，传 `task_id` 操作该 task；`list` 是所有 task 共享列表，行为保持不变。

### `codex_session`
查看或切换 session/task 状态（`status` · `plan_get/set` · `verbose_get/set` · `sandbox_get/set` · `approval_get/set`）。

这些支持 task scope 的 action 不传 `task_id` 就操作 session default，传 `task_id` 就操作该 task。default `sandbox_set` 设置未来 task 会拷贝的文件写入策略：

| 值 | 行为 |
|---|---|
| `read-only` | 每次文件写入触发 `fileChange` 审批请求 |
| `workspace-write` | Codex 在 `cwd` 内自由写入（默认） |
| `danger-full-access` | 无限制 |

---

## 斜杠命令

```
/codex                                        — 列出当前 session 的 task
/codex list [--threads]                       — 列出 task（或服务器全部 thread）
/codex reply <task_id> <message>              — 向 Codex 发送后续 turn 消息
/codex answer <task_id> <answer>              — 回答单个 Codex 问题
/codex answer <task_id> <a1> | <a2> | <a3>   — 回答多个问题（用 ' | ' 分隔）
/codex answer <task_id> [q1a|q1b] [q2a]      — 为单个问题提供多个答案
/codex approve <task_id>                      — 批准挂起的请求
/codex approve --all <task_id>                — 批准并本 session 内不再为类似命令弹审批
/codex deny <task_id>                         — 拒绝挂起的请求
/codex pending-schema <task_id>               — 查看 task 挂起 elicitation 的 schema
/codex archive <task_id>                      — 归档指定 task
/codex archive --all                          — 归档当前 session 全部 task
/codex archive --threads                      — 归档服务器全部 thread
/codex model [<model_id>]                     — 查看或设置默认模型
/codex model <task_id> [<model_id>]           — 查看或设置 task 模型
/codex models                                 — 列出可用模型
/codex plan [on|off]                          — 查看或设置默认 plan 模式
/codex plan <task_id> [on|off]                — 查看或设置 task plan 模式
/codex verbose [off|mid|on]                   — 设置通知详细程度
/codex sandbox [read|write|full]              — 查看或设置默认 sandbox 策略
/codex sandbox <task_id> [read|write|full]    — 查看或设置 task sandbox 策略
/codex approval [policy]                      — 查看或设置默认 approval 策略
/codex approval <task_id> [policy]            — 查看或设置 task approval 策略
/codex status [task_id]                       — 查看 session 或 task 状态
/codex help [<subcommand>]
```

**Verbose 级别：**
- `off` — 只推送最后一条 `item/completed` + `turn/completed`
- `mid` — `agentMessage` + `turn/completed`
- `on` — 所有 `item/completed` 通知

---

## 模块说明

| 模块 | 职责 |
|---|---|
| `__init__.py` | 插件注册，捕获主 event loop |
| `session.py` | `CodexSession` — per-session 核心（task、配置、WS 生命周期） |
| `session_registry.py` | 全局 `{ platform:chat_id → CodexSession }` 注册表 |
| `bridge.py` | `CodexBridge` — WebSocket 连接 + JSON-RPC 请求/响应配对 |
| `server_manager.py` | `CodexServerManager` — 引用计数管理 app-server 子进程 |
| `handlers.py` | `MessageHandler` — 入站帧分发到 session 回调 |
| `state.py` | `Task`、`TaskTarget` 数据类 |
| `notify.py` | 跨 loop 平台通知 + session 记录镜像 |
| `provider.py` | 从 app-server 同步默认 model 和 provider 信息 |
| `wire.py` | JSON-RPC 消息的 Pydantic 模型 |
| `policies.py` | 默认策略、超时常量、sandbox/collaboration mode 辅助函数 |
| `commands.py` | `/codex` 斜杠命令处理器 |
| `schemas.py` | LLM 侧工具 schema |
| `tools.py` | 工具处理函数 |
