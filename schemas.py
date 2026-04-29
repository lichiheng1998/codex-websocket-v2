"""Tool schemas for the codex-websocket-v2 plugin (LLM-facing)."""

from .codex_websocket_v2.policies import DEFAULT_APPROVAL_POLICY, DEFAULT_SANDBOX_POLICY

CODEX_REVIVE = {
    "name": "codex_revive",
    "description": (
        "Revive a Codex thread from a previous session (e.g. after gateway "
        "restart). Restores the thread into the active task map so the user "
        "can reply to it via `/codex reply`. Use when the user references an "
        "existing Codex thread_id that is no longer tracked in the current "
        "session. Codex's `thread/read` does NOT return the thread's last "
        "`model`/`sandbox_policy`/`approval_policy` (those are per-turn "
        "overrides), so pass them explicitly if the user wants follow-up "
        "turns to keep the original configuration; otherwise plugin defaults "
        "are used. Plan collaboration mode is a session-wide toggle — use "
        "`/codex plan on|off` rather than passing it here."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "description": "Full Codex thread UUID to revive.",
            },
            "sandbox_policy": {
                "type": "string",
                "enum": ["read-only", "workspace-write", "danger-full-access"],
                "description": (
                    f"Sandbox policy for follow-up turns (default "
                    f"'{DEFAULT_SANDBOX_POLICY}')."
                ),
            },
            "approval_policy": {
                "type": "string",
                "enum": ["on-request", "on-failure", "never", "untrusted"],
                "description": (
                    f"Approval policy for follow-up turns (default "
                    f"'{DEFAULT_APPROVAL_POLICY}')."
                ),
            },
        },
        "required": ["thread_id"],
    },
}

CODEX_TASKS = {
    "name": "codex_tasks",
    "description": (
        "Inspect or act on Codex tasks/threads in the current session. "
        "Action 'list' returns this session's tasks; pass show_threads=true "
        "to instead list every thread on the codex app-server. "
        "'reply' sends a follow-up message to a running task (requires "
        "task_id and message). 'approve'/'deny' resolve a pending command "
        "or elicitation request (requires task_id). 'archive' removes a "
        "single task_id from this session, or pass target='all' to archive "
        "every task in this session, or target='allthreads' to archive "
        "every thread on the server. "
        "Mirrors the user-facing /codex list/reply/approve/deny/archive "
        "subcommands."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "reply", "approve", "deny", "archive"],
            },
            "task_id": {
                "type": "string",
                "description": "Required for reply/approve/deny.",
            },
            "message": {
                "type": "string",
                "description": "Required for reply.",
            },
            "target": {
                "type": "string",
                "description": (
                    "For archive: a task_id, 'all' (this session's tasks), "
                    "or 'allthreads' (every thread on the server)."
                ),
            },
            "show_threads": {
                "type": "boolean",
                "description": "For list: include all server threads instead of session tasks.",
            },
        },
        "required": ["action"],
    },
}


CODEX_MODELS = {
    "name": "codex_models",
    "description": (
        "Inspect or set the default Codex model for this session. "
        "Action 'list' returns models advertised by the codex app-server "
        "(annotated with which one is the current session default). "
        "'get_default' returns the current session default. "
        "'set_default' sets the session default (requires model_id). "
        "Mirrors /codex models / /codex model [<id>]."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get_default", "set_default"],
            },
            "model_id": {
                "type": "string",
                "description": "Required for set_default.",
            },
        },
        "required": ["action"],
    },
}


CODEX_SESSION = {
    "name": "codex_session",
    "description": (
        "Inspect or toggle session-level Codex state. "
        "'status' returns a snapshot (connection, active_tasks, total_threads, "
        "model, mode, verbose). 'plan_get'/'plan_set' read or set plan-mode "
        "(when enabled, future turns use collaborationMode=plan). "
        "'verbose_get'/'verbose_set' read or set verbose level: "
        "'off' (last item/completed + turn/completed), "
        "'mid' (agentMessage + turn/completed), "
        "'on' (all item/completed notifications). "
        "'plan_set' requires enabled=true|false. 'verbose_set' requires level "
        "as a string ('off'/'mid'/'on'). "
        "Mirrors /codex status / plan / verbose."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status",
                    "plan_get",
                    "plan_set",
                    "verbose_get",
                    "verbose_set",
                ],
            },
            "enabled": {
                "type": "boolean",
                "description": "Required for plan_set.",
            },
            "level": {
                "type": "string",
                "enum": ["off", "mid", "on"],
                "description": "Required for verbose_set. 'off'=last item + turn end, 'mid'=agentMessage + turn end, 'on'=all items.",
            },
        },
        "required": ["action"],
    },
}


CODEX_TASK = {
    "name": "codex_task",
    "description": (
        "Delegate a coding task to Codex (OpenAI's code agent) running as a "
        "persistent background thread over the codex-app-server WebSocket. "
        "**Returns immediately with a task_id** — Codex runs asynchronously; "
        "progress updates, approval requests, and the final result are pushed "
        "to the current chat as separate messages. Command/file-change "
        "approvals route through `/codex approve`/`/codex deny`. If Codex "
        "asks the user a question (e.g. plan mode is enabled via "
        "`/codex plan on`), the user replies via `/codex reply <task_id> "
        "<answer>`. Use this when a task is "
        "well-scoped for a code-focused sub-agent (bug fix, feature, "
        "refactor). After calling, report the task_id to the user and return "
        "control — do NOT poll for the result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cwd": {
                "type": "string",
                "description": "Absolute path to the project directory Codex should operate in.",
            },
            "prompt": {
                "type": "string",
                "description": "The task description / instructions for Codex.",
            },
            "approval_policy": {
                "type": "string",
                "enum": ["on-request", "on-failure", "never", "untrusted"],
                "description": (
                    f"Codex approval policy (default '{DEFAULT_APPROVAL_POLICY}'). "
                    "'on-request' surfaces approvals to the user in chat."
                ),
            },
            "sandbox_policy": {
                "type": "string",
                "enum": ["read-only", "workspace-write", "danger-full-access"],
                "description": f"Codex sandbox policy (default '{DEFAULT_SANDBOX_POLICY}').",
            },
            "base_instructions": {
                "type": "string",
                "description": "Optional base instructions prepended to the Codex thread.",
            },
        },
        "required": ["cwd", "prompt"],
    },
}
