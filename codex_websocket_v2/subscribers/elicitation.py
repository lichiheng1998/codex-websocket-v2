"""Subscriber for MCP elicitation request events."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..approval_handler import ApprovalRequestHandler
from ..events import ElicitationRequestedEvent

if TYPE_CHECKING:
    from ..session import CodexSession

MAX_ELICITATION_SCHEMA_PREVIEW = 300


class ElicitationSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    async def __call__(self, event: ElicitationRequestedEvent) -> bool:
        inner = event.params.root if hasattr(event.params, "root") else event.params
        task = event.task
        task_id = event.task_id
        server_name = getattr(inner, "serverName", None) or "MCP server"
        elicitation = getattr(inner, "elicitation", None)
        mode = getattr(getattr(elicitation, "mode", None), "value", "form") if elicitation else "form"
        elicit_msg = getattr(elicitation, "message", "") if elicitation else ""

        if mode == "url":
            url = getattr(elicitation, "url", "") or ""
            heading = f"🔗 `{task_id}` MCP `{server_name}` needs you to visit a link:"
            body = f"{url}\n{elicit_msg}"
            footer = ApprovalRequestHandler.approval_footer(task_id, accept_label="When done", decline_label="Cancel")
        else:
            schema = getattr(elicitation, "requestedSchema", None) if elicitation else None
            schema_json = json.dumps(
                schema.model_dump(mode="json") if hasattr(schema, "model_dump") else (schema or {}),
                ensure_ascii=False,
            )[:MAX_ELICITATION_SCHEMA_PREVIEW]
            heading = f"❓ `{task_id}` MCP `{server_name}` requests input:"
            body = f"{elicit_msg}\nSchema: `{schema_json}`"
            footer = ApprovalRequestHandler.approval_footer(task_id, accept_label="Accept", decline_label="Decline")

        notification = "\n".join([heading, body, "", footer])
        self.session.stash_request(task, event.rpc_id, "elicitation",
                                   {"preview": elicit_msg, "server": server_name})
        await self.session.notify(notification)
        return True
