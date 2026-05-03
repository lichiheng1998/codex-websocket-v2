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
    │       └── CodexBridge             ← server lease + JSON-RPC over WebSocket
    │               │
    │       └── CodexServerManager      ← ref-counted codex app-server subprocess
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
| `model` | string | Task model. If omitted, copied from the session default |
| `plan` | enum | `on` · `off`. If omitted, copied from the session default |
| `sandbox_policy` | enum | `read-only` · `workspace-write` · `danger-full-access`. If omitted, copied from the session default |
| `approval_policy` | enum | `on-request` · `on-failure` · `never` · `untrusted`. If omitted, copied from the session default |
| `base_instructions` | string | Optional instructions prepended to the thread |

Task policy values are fixed when the task is created. Later replies use the task's own `model`, `plan`, `sandbox_policy`, and `approval_policy`; changing session defaults affects only future tasks.

### `codex_tasks`
List or archive tasks and threads in the current session.

| Action | Parameters | Description |
|---|---|---|
| `list` | `show_threads` | List session tasks (or all server threads) |
| `show_pending` | `task_id` | Return the current pending request details for a task, if any |
| `archive` | `target` | Archive a specific task (`task_id`), all session tasks (`all`), or every server thread (`allthreads`). Blocked if the thread is held by another active session. |

### `codex_action`
Send follow-up actions to existing tasks.

| Action | Parameters | Description |
|---|---|---|
| `reply` | `task_id`, `message` | Send a follow-up turn message to a running task |
| `answer` | `task_id`, `responses[]` or `answers[][]` | Answer a `requestUserInput`; use `answers[][]` for multiple answers per question |
| `respond` | `task_id`, `content` | Respond to a pending elicitation with form data matching its schema |

### `codex_approval`
Resolve pending approval-style requests.

| Action | Parameters | Description |
|---|---|---|
| `approve` | `task_id`, `for_session` | Approve a pending command, or accept a pending elicitation with empty content `{}`. Set `for_session=true` to send `acceptForSession` for command approvals only |
| `deny` | `task_id` | Deny a pending command, or decline a pending elicitation with empty content `{}` |

For `requestUserInput` questions with options, answer with the exact option label shown in the notification.

> **Note:** `turn/completed` means one turn ended — the thread is still alive. Use `reply` to continue. Only use `codex_revive` for threads no longer tracked in the current session.

### `codex_revive`
Restore a thread from a previous session (e.g. after gateway restart) into the active task map. Blocked if the thread is currently held by another active session.

### `codex_models`
List shared models, or get/set the default/task model (`list` · `get` · `set`). For `get`/`set`, omit `task_id` to operate on the session default; pass `task_id` to operate on that task. `list` is shared across tasks and keeps the old behavior.

### `codex_session`
Inspect or toggle session/task state (`status` · `plan_get/set` · `verbose_get/set` · `sandbox_get/set` · `approval_get/set`).

For scoped actions, omit `task_id` to operate on the session default; pass `task_id` to operate on that task. The default `sandbox_set` action sets the file write policy copied into future tasks:

| Value | Behaviour |
|---|---|
| `read-only` | Every file write triggers a `fileChange` approval request |
| `workspace-write` | Codex writes freely inside `cwd` (default) |
| `danger-full-access` | No restrictions |

---

## Slash Commands

```
/codex                                        — list this session's tasks
/codex list [--threads]                       — list tasks (or all server threads)
/codex reply <task_id> <message>              — send a follow-up turn to Codex
/codex answer <task_id> <answer>              — answer a single Codex question
/codex answer <task_id> <a1> | <a2> | <a3>   — answer multiple questions (separated by ' | ')
/codex answer <task_id> [q1a|q1b] [q2a]      — multiple answers for individual questions
/codex approve <task_id>                      — approve a pending request
/codex approve --all <task_id>                — approve and stop prompting for similar commands this session
/codex deny <task_id>                         — deny a pending request
/codex pending <task_id>                      — show a task's pending request details
/codex archive <task_id>                      — archive a specific task
/codex archive --all                          — archive all tasks in this session
/codex archive --threads                      — archive every thread on the server
/codex model [<model_id>]                     — show or set default model
/codex model <task_id> [<model_id>]           — show or set a task's model
/codex models                                 — list available models
/codex plan [on|off]                          — show or set default plan mode
/codex plan <task_id> [on|off]                — show or set a task's plan mode
/codex verbose [off|mid|on]                   — set notification verbosity
/codex sandbox [read|write|full]              — show or set default sandbox policy
/codex sandbox <task_id> [read|write|full]    — show or set a task's sandbox policy
/codex approval [policy]                      — show or set default approval policy
/codex approval <task_id> [policy]            — show or set a task's approval policy
/codex status [task_id]                       — show session or task status
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
| `session.py` | `CodexSession` — per-session core (tasks and config) |
| `session_registry.py` | Global `{ platform:chat_id → CodexSession }` registry |
| `transport/bridge.py` | `CodexBridge` — server lease, WebSocket connection, JSON-RPC pairing |
| `transport/server_manager.py` | `CodexServerManager` — ref-counted app-server subprocess |
| `handlers.py` | `MessageHandler` — inbound frame dispatch → session callbacks |
| `state.py` | `Task`, `TaskTarget` dataclasses |
| `notify.py` | Cross-loop platform notification + session transcript mirroring |
| `provider.py` | Sync default model and provider info from app-server |
| `wire.py` | Pydantic models for JSON-RPC wire format |
| `policies.py` | Default policies, timeouts, sandbox/collaboration mode helpers |
| `commands.py` | `/codex` slash command handlers |
| `schemas.py` | LLM-facing tool schemas |
| `tools.py` | Tool handler functions |
