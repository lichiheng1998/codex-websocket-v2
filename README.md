<div align="center">

## [English](README.md) | [中文](README_CN.md)

</div>

# codex-websocket-v2

A [hermes](https://github.com/lichi/hermes-agent) plugin that bridges hermes sessions to the [OpenAI Codex CLI](https://github.com/openai/codex) app-server over WebSocket, allowing users to delegate coding tasks through any chat platform (Telegram, WeChat, Discord, etc.).

---

## Architecture

```
hermes session
    │
    ├── Tools / Slash commands          ← user-facing API
    │
    ├── CodexSession (per-session)      ← isolated WS + event loop + task list + config
    │       │
    │       └── CodexBridge             ← JSON-RPC over WebSocket
    │               │
    ├── CodexServerManager (process)    ← ref-counted codex app-server subprocess
    │
    └── codex app-server                ← local process, shared across sessions
```

**Key design decisions:**

- **Per-session isolation** — each hermes session has its own `CodexSession` with an independent WebSocket connection, asyncio event loop, task map, and configuration. Multiple users/sessions don't interfere with each other.
- **Shared app-server** — a single `codex app-server` subprocess is shared across all sessions via reference counting. It starts on the first session and shuts down when the last session exits.
- **Cross-loop notification** — Codex runs on its own event loop thread; hermes platforms (e.g. WeChat's aiohttp) bind to the main loop. `notify.py` bridges the two via `run_coroutine_threadsafe`.

---

## Requirements

- Python ≥ 3.10
- [hermes-agent](https://github.com/lichi/hermes-agent)
- [Codex CLI](https://github.com/openai/codex) (`codex` available in `$PATH`)
- `websockets >= 11.0`
- `pydantic >= 2.0`

---

## Installation

```bash
hermes plugins install lichiheng1998/codex-websocket-v2
```

Then install Python dependencies and verify the Codex CLI is available:

```bash
pip install websockets>=11.0 pydantic>=2.0
codex --version
```

hermes will auto-discover and register the plugin on the next startup.

---

## Tools

These tools are available to the LLM agent when the plugin is loaded.

### `codex_task`
Start a new coding task in a background Codex thread. Returns immediately with a `task_id`; progress and results are pushed to the chat as separate messages.

| Parameter | Type | Description |
|---|---|---|
| `cwd` | string (required) | Absolute path to the project directory |
| `prompt` | string (required) | Task description / instructions for Codex |
| `approval_policy` | enum | `on-request` · `on-failure` · `never` · `untrusted` (default: `never`) |
| `sandbox_policy` | enum | `read-only` · `workspace-write` · `danger-full-access` (default: `workspace-write`) |
| `base_instructions` | string | Optional instructions prepended to the thread |

### `codex_tasks`
Manage tasks and threads in the current session.

| Action | Parameters | Description |
|---|---|---|
| `list` | `show_threads` | List session tasks (or all server threads) |
| `reply` | `task_id`, `message` | Send a follow-up turn message to a running task |
| `answer` | `task_id`, `responses[]` | Answer a `requestUserInput` — one string per question in order |
| `approve` | `task_id`, `for_session` | Approve a pending command / elicitation. Set `for_session=true` to send `acceptForSession` (command-execution only: stops Codex prompting for similar commands this session) |
| `deny` | `task_id` | Deny a pending command / elicitation |
| `archive` | `target` | Archive a specific task (`task_id`), all session tasks (`all`), or every server thread (`allthreads`). Blocked if the thread is held by another active session. |

> **Note:** `turn/completed` means one turn ended — the thread is still alive. Use `reply` to continue. Only use `codex_revive` for threads no longer tracked in the current session.

### `codex_revive`
Restore a thread from a previous session (e.g. after gateway restart) into the active task map. Blocked if the thread is currently held by another active session.

### `codex_models`
List or set the default model for the current session (`list` · `get_default` · `set_default`).

### `codex_session`
Inspect or toggle session-level state (`status` · `plan_get/set` · `verbose_get/set`).

---

## Slash Commands

```
/codex                                        — list this session's tasks
/codex list [--threads]                       — list tasks (or all server threads)
/codex reply <task_id> <message>              — send a follow-up turn to Codex
/codex answer <task_id> <answer>              — answer a single Codex question
/codex answer <task_id> <a1> | <a2> | <a3>   — answer multiple questions (separated by ' | ')
/codex approve <task_id>                      — approve a pending request
/codex approve --all <task_id>                — approve and stop prompting for similar commands this session
/codex deny <task_id>                         — deny a pending request
/codex archive <task_id>                      — archive a specific task
/codex archive --all                          — archive all tasks in this session
/codex archive --threads                      — archive every thread on the server
/codex model [<model_id>]                     — show or set default model
/codex models                                 — list available models
/codex plan [on|off]                          — toggle plan collaboration mode
/codex verbose [off|mid|on]                   — set notification verbosity
/codex status                                 — show session status
/codex help [<subcommand>]
```

**Verbose levels:**
- `off` — last `item/completed` + `turn/completed` only
- `mid` — `agentMessage` + `turn/completed`
- `on` — all `item/completed` notifications

---

## Module Reference

| Module | Role |
|---|---|
| `__init__.py` | Plugin registration, main event loop capture |
| `session.py` | `CodexSession` — per-session core (tasks, config, WS lifecycle) |
| `session_registry.py` | Global `{ platform:chat_id → CodexSession }` registry |
| `bridge.py` | `CodexBridge` — WebSocket connection + JSON-RPC pairing |
| `server_manager.py` | `CodexServerManager` — ref-counted app-server subprocess |
| `handlers.py` | `MessageHandler` — inbound frame dispatch → session callbacks |
| `state.py` | `Task`, `TaskTarget` dataclasses |
| `notify.py` | Cross-loop platform notification + session transcript mirroring |
| `provider.py` | Sync default model and provider info from app-server |
| `wire.py` | Pydantic models for JSON-RPC wire format |
| `policies.py` | Default policies, timeouts, sandbox/collaboration mode helpers |
| `commands.py` | `/codex` slash command handlers |
| `schemas.py` | LLM-facing tool schemas |
| `tools.py` | Tool handler functions |
