<div align="right">

[English](README.md) | [中文](README_CN.md)

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
| `approval_policy` | enum | `on-request` · `on-failure` · `never` · `untrusted`（默认 `never`） |
| `sandbox_policy` | enum | `read-only` · `workspace-write` · `danger-full-access`（默认 `workspace-write`） |
| `base_instructions` | string | 可选的前置指令 |

### `codex_tasks`
管理当前 session 的 task 和 thread。

| Action | 参数 | 说明 |
|---|---|---|
| `list` | `show_threads` | 列出 session task（或服务器全部 thread） |
| `reply` | `task_id`, `message` | 向运行中的 task 发送后续 turn 消息 |
| `answer` | `task_id`, `responses[]` | 回答 `requestUserInput`（每个问题一个字符串） |
| `approve` | `task_id` | 批准挂起的命令 / elicitation |
| `deny` | `task_id` | 拒绝挂起的命令 / elicitation |
| `archive` | `target` | 归档单个 task、`all`（当前 session）或 `allthreads`（服务器全部） |

> **注意：** `turn/completed` 只代表当前 turn 结束，thread 仍然存活。用 `reply` 继续对话。仅在 thread 不在当前 session 中被追踪时（如 gateway 重启后）才使用 `codex_revive`。

### `codex_revive`
将上一个 session 的 thread（如 gateway 重启后丢失的）恢复到当前 session 的 task 表中。

### `codex_models`
列出或设置当前 session 的默认模型（`list` · `get_default` · `set_default`）。

### `codex_session`
查看或切换 session 级状态（`status` · `plan_get/set` · `verbose_get/set`）。

---

## 斜杠命令

```
/codex                              — 列出当前 session 的 task
/codex list [--threads]             — 列出 task（或服务器全部 thread）
/codex reply <task_id> <message>    — 向 Codex 发送后续 turn 消息
/codex answer <task_id> <answer>    — 回答单个 Codex 问题
/codex answer <task_id> <a1> | <a2> | <a3>   — 回答多个问题（用 ' | ' 分隔）
/codex approve <task_id>            — 批准挂起的请求
/codex deny <task_id>               — 拒绝挂起的请求
/codex archive <task_id|all|allthreads>
/codex model [<model_id>]           — 查看或设置默认模型
/codex models                       — 列出可用模型
/codex plan [on|off]                — 切换 plan 协作模式
/codex verbose [off|mid|on]         — 设置通知详细程度
/codex status                       — 查看 session 状态
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
