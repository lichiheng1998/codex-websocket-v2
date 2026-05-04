"""Subscriber for MCP elicitation request events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .approval import ApprovalRequestSubscriber
from ..models import ElicitationRequestedEvent

if TYPE_CHECKING:
    from ...core.session import CodexSession

def _elicitation_value(elicitation: Any, inner: Any, name: str, default: Any = None) -> Any:
    """Read modern flat params, while tolerating the old nested shape."""
    if elicitation is not None and hasattr(elicitation, name):
        return getattr(elicitation, name)
    return getattr(inner, name, default)


def _dump_schema(schema: Any) -> dict[str, Any]:
    if schema is None:
        return {}
    if hasattr(schema, "model_dump"):
        return schema.model_dump(by_alias=True, exclude_none=True, mode="json")
    return dict(schema)


def _schema_has_fields(schema: dict[str, Any]) -> bool:
    properties = schema.get("properties")
    return isinstance(properties, dict) and bool(properties)


def _schema_field_summary(schema: dict[str, Any], *, limit: int = 8) -> str:
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    lines = ["Fields:"]
    for name, spec in list(properties.items())[:limit]:
        spec = spec or {}
        if isinstance(spec, dict):
            field_type = spec.get("type") or "any"
            title = spec.get("title")
        else:
            field_type = "any"
            title = None
        required_mark = "*" if name in required else ""
        suffix = f" — {title}" if title and title != name else ""
        lines.append(f"- `{name}`{required_mark}: {field_type}{suffix}")
    if len(properties) > limit:
        lines.append(f"- ... and {len(properties) - limit} more fields")
    return "\n".join(lines)


class ElicitationSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    async def __call__(self, event: ElicitationRequestedEvent) -> bool:
        inner = event.params.root if hasattr(event.params, "root") else event.params
        task = event.task
        task_id = event.task_id
        server_name = getattr(inner, "serverName", None) or "MCP server"
        elicitation = getattr(inner, "elicitation", None)
        mode_value = _elicitation_value(elicitation, inner, "mode", "form")
        mode = getattr(mode_value, "value", mode_value)
        elicit_msg = _elicitation_value(elicitation, inner, "message", "") or ""

        if mode == "url":
            url = _elicitation_value(elicitation, inner, "url", "") or ""
            heading = f"🔗 `{task_id}` MCP `{server_name}` needs you to visit a link:"
            body = f"{url}\n{elicit_msg}"
            footer = ApprovalRequestSubscriber.approval_footer(task_id, accept_label="When done", decline_label="Cancel")
            stash_schema = None
        else:
            schema = _elicitation_value(elicitation, inner, "requestedSchema")
            schema_dict = _dump_schema(schema)
            if _schema_has_fields(schema_dict):
                stash_schema = schema_dict
                heading = f"❓ `{task_id}` MCP `{server_name}` requests input:"
                body = (
                    f"{elicit_msg}\n"
                    f"{_schema_field_summary(schema_dict)}\n"
                    f"Full schema: `/codex pending {task_id}`"
                )
                footer = (
                    "Use `respond` to provide schema data, or `approve`/`deny` "
                    "to send empty content.\n"
                    + f"Approve empty: `/codex approve {task_id}`\n"
                    + f"Respond: `/codex respond {task_id} {{...}}`\n"
                    + f"Decline: `/codex deny {task_id}`"
                )
            else:
                stash_schema = None
                heading = f"❓ `{task_id}` MCP `{server_name}` requests confirmation:"
                body = elicit_msg
                footer = (
                    f"Approve: `/codex approve {task_id}`\n"
                    + f"Decline: `/codex deny {task_id}`"
                )

        notification = "\n".join([heading, body, "", footer])
        self.session.stash_request(task, event.rpc_id, "elicitation",
                                   {"preview": elicit_msg, "server": server_name},
                                   request_schema=stash_schema)
        await self.session.notify(notification)
        return True
