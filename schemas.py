"""Tool schemas for the codex-websocket-v2 plugin (LLM-facing)."""

from .codex_websocket_v2.core.policies import DEFAULT_APPROVAL_POLICY

CODEX_REVIVE = {
    "name": "codex_revive",
    "description": (
        "Revive a Codex thread from a previous session (e.g. after gateway "
        "restart). Restores the thread into the active task map so the user "
        "can send follow-up turns via codex_tasks reply. "
        "IMPORTANT: only revive threads whose status is truly terminated or "
        "unknown — do NOT revive a thread just because you received a "
        "turn/completed notification. turn/completed means one turn finished "
        "and Codex is idle waiting for the next prompt; the thread is still "
        "alive and tracked. Use codex_tasks reply to continue it instead. "
        "The session's sandbox_policy (set via codex_session sandbox_set or "
        "/codex sandbox) is used automatically for revived threads. "
        "Plan collaboration mode is a session-wide toggle — use "
        "`/codex plan on|off` rather than passing it here."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "description": "Full Codex thread UUID to revive.",
            },
            "approval_policy": {
                "type": "string",
                "enum": ["on-request", "on-failure", "never", "untrusted"],
                "description": (
                    f"Shell command execution prompting for follow-up turns "
                    f"(default '{DEFAULT_APPROVAL_POLICY}'). Does NOT control "
                    "file writes — use codex_session sandbox_set for that."
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
        "'reply' sends a follow-up turn message to a running task (requires "
        "task_id and message). Use this to continue a conversation after "
        "turn/completed — turn/completed only means the current turn ended, "
        "NOT that the thread is finished; the thread stays alive and tracked. "
        "'answer' resolves a pending requestUserInput from Codex (requires "
        "task_id and responses array). Use this — not reply — when Codex "
        "explicitly asked one or more questions and is waiting for answers; "
        "provide one string per question in order. "
        "'approve'/'deny' resolve a pending command or elicitation request "
        "(requires task_id). 'archive' removes a single task_id from this "
        "session, or pass target='all' to archive every task in this session, "
        "or target='allthreads' to archive every thread on the server. "
        "Mirrors the user-facing /codex list/reply/approve/deny/archive "
        "subcommands."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "reply", "answer", "approve", "deny", "archive"],
            },
            "task_id": {
                "type": "string",
                "description": "Required for reply/answer/approve/deny.",
            },
            "message": {
                "type": "string",
                "description": "Required for reply: the follow-up turn message to send.",
            },
            "responses": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Required for answer: one string per question in the order "
                    "Codex presented them. The array is sent as-is; if shorter "
                    "than the question count the last entry is repeated."
                ),
            },
            "target": {
                "type": "string",
                "description": (
                    "For archive: a task_id, 'all' (this session's tasks), "
                    "or 'allthreads' (every thread on the server)."
                ),
            },
            "for_session": {
                "type": "boolean",
                "description": (
                    "For approve: send acceptForSession instead of accept. "
                    "Valid for command-execution and file-change approvals (not permissions). "
                    "Tells Codex to stop prompting for similar commands/writes for the rest of the session."
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
        "model, mode, verbose, sandbox_policy). "
        "'plan_get'/'plan_set' read or set plan-mode "
        "(when enabled, future turns use collaborationMode=plan). "
        "'verbose_get'/'verbose_set' read or set verbose level: "
        "'off' (last item/completed + turn/completed), "
        "'mid' (agentMessage + turn/completed), "
        "'on' (all item/completed notifications). "
        "'sandbox_get'/'sandbox_set' read or set the session default sandbox "
        "policy applied to all new and revived tasks. "
        "'read-only' = every file write triggers a fileChange approval; "
        "'workspace-write' = Codex writes freely inside cwd (no approval); "
        "'danger-full-access' = no restrictions. "
        "'plan_set' requires enabled=true|false. 'verbose_set' requires level "
        "('off'/'mid'/'on'). 'sandbox_set' requires sandbox_policy. "
        "Mirrors /codex status / plan / verbose / sandbox."
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
                    "sandbox_get",
                    "sandbox_set",
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
            "sandbox_policy": {
                "type": "string",
                "enum": ["read-only", "workspace-write", "danger-full-access"],
                "description": (
                    "Required for sandbox_set. Controls file write access for "
                    "all subsequent tasks: 'read-only' triggers fileChange "
                    "approvals on every write; 'workspace-write' allows free "
                    "writes inside cwd; 'danger-full-access' removes all limits."
                ),
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
        "approvals route through codex_tasks approve/deny. "
        "IMPORTANT: a turn/completed notification means one turn ended and "
        "Codex is idle — it does NOT mean the thread is finished or needs "
        "reviving. To send a follow-up prompt use codex_tasks reply. Only use "
        "codex_revive for threads that are no longer tracked in this session "
        "(e.g. after a gateway restart). "
        "If Codex asks one or more questions (requestUserInput), answer with "
        "codex_tasks answer (not reply). "
        "File write access is controlled by the session sandbox_policy "
        "(default: workspace-write = Codex writes freely inside cwd; "
        "read-only = every write triggers a fileChange approval). "
        "Change it with codex_session sandbox_set or /codex sandbox. "
        "Use this when a task is well-scoped for a code-focused sub-agent "
        "(bug fix, feature, refactor). After calling, report the task_id to "
        "the user and return control — do NOT poll for the result."
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
                    f"Controls shell command execution prompting only (default "
                    f"'{DEFAULT_APPROVAL_POLICY}'). 'on-request' = every shell "
                    "command triggers an approval request; 'never' = all "
                    "commands run without prompting. Does NOT affect file "
                    "writes — use codex_session sandbox_set for that."
                ),
            },
            "base_instructions": {
                "type": "string",
                "description": "Optional base instructions prepended to the Codex thread.",
            },
        },
        "required": ["cwd", "prompt"],
    },
}
