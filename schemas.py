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
        "/codex sandbox) is used automatically for revived threads unless "
        "sandbox_policy is passed. Model, plan, sandbox, and approval values "
        "are copied onto the revived task; future replies use the task's "
        "own values."
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
                    "Optional task approval policy. If omitted, uses the "
                    "session default approval policy."
                ),
            },
            "sandbox_policy": {
                "type": "string",
                "enum": ["read-only", "workspace-write", "danger-full-access"],
                "description": "Optional task sandbox policy. If omitted, uses the session default.",
            },
            "model": {
                "type": "string",
                "description": "Optional task model. If omitted, uses the session default model.",
            },
            "plan": {
                "type": "string",
                "enum": ["on", "off"],
                "description": "Optional task plan mode. Only 'on' or 'off' are accepted.",
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
        "List available Codex models, or inspect/set the default/task model. "
        "Action 'list' returns models advertised by the codex app-server "
        "(shared across tasks and annotated with the current session default). "
        "'get' returns the session default model when task_id is omitted, or "
        "the task model when task_id is provided. 'set' behaves the same and "
        "requires model_id. "
        "Mirrors /codex models / /codex model [<id>]."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "set"],
            },
            "task_id": {
                "type": "string",
                "description": "Optional for get/set. Omit to operate on the session default; pass a task_id to operate on that task.",
            },
            "model_id": {
                "type": "string",
                "description": "Required for set.",
            },
        },
        "required": ["action"],
    },
}


CODEX_SESSION = {
    "name": "codex_session",
    "description": (
        "Inspect or toggle session-level Codex state. "
        "'status' returns session status when task_id is omitted, or task "
        "status when task_id is provided. "
        "'plan_get'/'plan_set', 'sandbox_get'/'sandbox_set', and "
        "'approval_get'/'approval_set' operate on the session default when "
        "task_id is omitted, or on a task when task_id is provided. "
        "'verbose_get'/'verbose_set' read or set verbose level: "
        "'off' (last item/completed + turn/completed), "
        "'mid' (agentMessage + turn/completed), "
        "'on' (all item/completed notifications). "
        "'read-only' = every file write triggers a fileChange approval; "
        "'workspace-write' = Codex writes freely inside cwd (no approval); "
        "'danger-full-access' = no restrictions. "
        "'plan_set' requires plan='on'|'off'. 'verbose_set' requires level "
        "('off'/'mid'/'on'). 'sandbox_set' requires sandbox_policy. "
        "'approval_set' requires approval_policy. "
        "Mirrors /codex status / plan / verbose / sandbox / approval."
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
                    "approval_get",
                    "approval_set",
                ],
            },
            "task_id": {
                "type": "string",
                "description": "Optional for status and scoped get/set actions. Omit to operate on the session default/status.",
            },
            "plan": {
                "type": "string",
                "enum": ["on", "off"],
                "description": "Required for plan_set. Only 'on' or 'off' are accepted.",
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
            "approval_policy": {
                "type": "string",
                "enum": ["on-request", "on-failure", "never", "untrusted"],
                "description": "Required for approval_set. Omit task_id to set the session default; pass task_id to set a task.",
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
        "Model, plan, sandbox_policy, and approval_policy are fixed on the "
        "task when it is created. Omitted values are copied from the session "
        "defaults; future replies use the task's own values. "
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
                    "Optional task approval policy. If omitted, uses the "
                    f"session default (initially '{DEFAULT_APPROVAL_POLICY}')."
                ),
            },
            "sandbox_policy": {
                "type": "string",
                "enum": ["read-only", "workspace-write", "danger-full-access"],
                "description": "Optional task sandbox policy. If omitted, uses the session default.",
            },
            "model": {
                "type": "string",
                "description": "Optional task model. If omitted, uses the session default model.",
            },
            "plan": {
                "type": "string",
                "enum": ["on", "off"],
                "description": "Optional task plan mode. Only 'on' or 'off' are accepted.",
            },
            "base_instructions": {
                "type": "string",
                "description": "Optional base instructions prepended to the Codex thread.",
            },
        },
        "required": ["cwd", "prompt"],
    },
}
