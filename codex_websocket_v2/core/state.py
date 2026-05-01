"""Plain data containers for the codex-websocket-v2 plugin.

All session-scoped state lives on ``CodexSession`` and ``Task`` (defined
elsewhere). This module only holds the Result convention helpers and the
``TaskTarget`` notification coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


Result = Dict[str, Any]


def ok(**data: Any) -> Result:
    return {"ok": True, **data}


def err(message: str) -> Result:
    return {"ok": False, "error": message}


@dataclass
class TaskTarget:
    platform: str = ""
    chat_id: str = ""
    thread_id: str = ""


@dataclass
class Task:
    """One Codex task in a session.

    Tasks are owned by ``CodexSession.tasks`` (keyed by ``task_id``). Each
    task has at most one outstanding server→client request at a time
    (approval, elicitation, or input); the three ``request_*`` fields hold
    that pending state. ``request_type`` decides the wire shape of the
    response in ``CodexSession.approve_task`` / ``input_task``.
    """

    task_id: str
    thread_id: str
    cwd: str
    sandbox_policy: str
    approval_policy: str

    # The single in-flight server→client request, if any.
    # request_type: "command" | "elicitation" | "input"
    request_rpc_id: Any = None
    request_type: Optional[str] = None
    request_payload: Optional[Dict[str, Any]] = field(default=None)

    # Buffer for "off" verbose level — holds the most recent item, flushed at turn/completed.
    last_item: Any = None
    last_item_type: str = ""
