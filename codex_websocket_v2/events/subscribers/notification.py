"""Subscriber for server notification events."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..models import (
    ItemCompletedEvent,
    ItemStartedEvent,
    ServerRequestResolvedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)

if TYPE_CHECKING:
    from ...core.session import CodexSession
    from ...core.state import Task

logger = logging.getLogger(__name__)

MAX_COMMAND_OUTPUT = 1000


class NotificationSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session
        self.item_handlers = {
            "agentMessage": self._agent_message,
            "plan": self._plan,
            "commandExecution": self._command_execution,
            "fileChange": self._file_change,
            "webSearch": self._web_search,
            "enteredReviewMode": self._entered_review_mode,
            "exitedReviewMode": self._exited_review_mode,
            "contextCompaction": self._context_compaction,
        }

    async def __call__(
        self,
        event: (
            ItemStartedEvent
            | ItemCompletedEvent
            | TurnStartedEvent
            | TurnCompletedEvent
            | ServerRequestResolvedEvent
        ),
    ) -> bool:
        if isinstance(event, ItemStartedEvent):
            if event.task is not None:
                self._item_started(event)
            return True
        if isinstance(event, ItemCompletedEvent):
            if event.task is not None:
                await self._safe(self._item_completed(event))
            return True
        if isinstance(event, TurnStartedEvent):
            if event.task is not None:
                self._turn_started(event)
            return True
        if isinstance(event, TurnCompletedEvent):
            if event.task is not None:
                await self._safe(self._turn_completed(event))
            return True
        if isinstance(event, ServerRequestResolvedEvent):
            await self._safe(self._server_request_resolved(event))
            return True
        return False

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except Exception as exc:
            logger.warning("codex handler task failed: %s", exc)

    def _item_started(self, event: ItemStartedEvent) -> None:
        item_id = getattr(event.item, "id", None)
        if item_id:
            event.task.started_items[item_id] = event.item

    async def _item_completed(self, event: ItemCompletedEvent) -> None:
        task = event.task
        item = event.item
        item_type = event.item_type
        item_id = getattr(item, "id", None)
        if item_id:
            task.started_items.pop(item_id, None)
        level = self.session.verbose

        task.last_item = item
        task.last_item_type = item_type

        if level == "off":
            return

        if level == "mid":
            if item_type == "agentMessage":
                await self._agent_message(task, item)
            return

        handler = self.item_handlers.get(item_type)
        if handler is not None:
            await handler(task, item)
        else:
            logger.debug("codex handler: item type %r ignored", item_type)

    async def _agent_message(self, task: "Task", item: Any) -> None:
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            return
        prefix = f"🤖 `{task.task_id}`\n\n"
        await self.session.notify(prefix + text)

    async def _plan(self, task: "Task", item: Any) -> None:
        text = (getattr(item, "text", "") or "").strip()
        if text:
            await self.session.notify(f"📋 `{task.task_id}` plan\n\n{text}")

    async def _command_execution(self, task: "Task", item: Any) -> None:
        cmd = (getattr(item, "command", "") or "").strip()
        exit_code = getattr(item, "exitCode", None)
        output = (getattr(item, "aggregatedOutput", "") or "").strip()
        icon = "✅" if exit_code == 0 else "❌"
        lines = [f"{icon} `{cmd}` (exit {exit_code})"]
        if output:
            lines.append(f"```\n{_middle_ellipsize(output, MAX_COMMAND_OUTPUT)}\n```")
        await self.session.notify("\n".join(lines))

    async def _file_change(self, task: "Task", item: Any) -> None:
        changes = getattr(item, "changes", None) or []
        if not changes:
            return
        lines = [f"📝 `{task.task_id}` file changes"]
        for change in changes:
            path = getattr(change, "path", None) or "?"
            kind = getattr(change, "kind", None) or "modify"
            kind_value = getattr(kind, "value", kind)
            icon = {"create": "➕", "delete": "➖"}.get(kind_value, "✏️")
            lines.append(f"  {icon} `{path}`")
        await self.session.notify("\n".join(lines))

    async def _web_search(self, task: "Task", item: Any) -> None:
        query = (getattr(item, "query", "") or "").strip()
        if query:
            await self.session.notify(f"🔍 `{task.task_id}` search: {query}")

    async def _entered_review_mode(self, task: "Task", item: Any) -> None:
        review = (getattr(item, "review", "") or "").strip()
        await self.session.notify(f"👁️ `{task.task_id}` entered review mode: {review}")

    async def _exited_review_mode(self, task: "Task", item: Any) -> None:
        review = (getattr(item, "review", "") or "").strip()
        msg = f"👁️ `{task.task_id}` review complete"
        if review:
            msg += f"\n\n{review}"
        await self.session.notify(msg)

    async def _context_compaction(self, task: "Task", item: Any) -> None:
        await self.session.notify(f"🗜️ `{task.task_id}` context compacted")

    def _turn_started(self, event: TurnStartedEvent) -> None:
        turn_id = getattr(event.turn, "id", "") or ""
        if turn_id:
            event.task.active_turn_id = turn_id

    async def _turn_completed(self, event: TurnCompletedEvent) -> None:
        task = event.task
        turn = event.turn
        status = event.status
        task.last_turn_status = status
        turn_id = getattr(turn, "id", "") or ""
        if turn_id and task.active_turn_id == turn_id:
            task.active_turn_id = ""

        if self.session.verbose == "off" and task.last_item is not None:
            await self._show_last_item(task)

        if status == "failed":
            error = turn.error
            msg_text = getattr(error, "message", "") or "unknown error"
            code = getattr(error, "codexErrorInfo", None)
            code_str = getattr(code, "value", code) if code else ""
            error_str = f"{msg_text} ({code_str})" if code_str else msg_text
            await self.session.notify(f"❌ Codex task `{task.task_id}` failed: {error_str}")
        elif status == "interrupted":
            await self.session.notify(f"⏱️ Codex task `{task.task_id}` interrupted")
        else:
            await self.session.notify(
                f"✅ Codex task `{task.task_id}` turn completed "
                f"(thread still alive — use reply to continue)\n"
                f"Continue: `/codex reply {task.task_id} [message]`",
            )

    async def _server_request_resolved(self, event: ServerRequestResolvedEvent) -> None:
        request_id = event.request_id
        task = self._task_for_request_id(request_id)
        if task is None:
            logger.debug("codex handler: resolved unknown server request %r", request_id)
            return

        request_type = task.request_type or "request"
        preview = (task.request_payload or {}).get("preview", "")
        task.request_rpc_id = None
        task.request_type = None
        task.request_payload = None
        task.request_schema = None

        message = f"✅ Codex task `{task.task_id}` {request_type} request resolved"
        if preview:
            message += f": {preview}"
        await self.session.notify(message)

    def _task_for_request_id(self, request_id: Any) -> "Task | None":
        for task in self.session.tasks.values():
            if _same_request_id(task.request_rpc_id, request_id):
                return task
        return None

    async def _show_last_item(self, task: "Task") -> None:
        item = task.last_item
        item_type = task.last_item_type
        task.last_item = None
        task.last_item_type = ""
        handler = self.item_handlers.get(item_type)
        if handler is not None:
            await handler(task, item)
        else:
            logger.debug("codex handler: last item type %r ignored", item_type)


def _same_request_id(left: Any, right: Any) -> bool:
    if left == right:
        return True
    left_root = getattr(left, "root", left)
    right_root = getattr(right, "root", right)
    if left_root == right_root:
        return True
    return str(left_root) == str(right_root)


def _middle_ellipsize(text: str, max_chars: int) -> str:
    """Keep the beginning and end of long command output with an explicit gap."""
    if len(text) <= max_chars:
        return text
    marker_template = "\n... omitted {omitted} characters ...\n"
    marker = marker_template.format(omitted=0)
    budget = max(0, max_chars - len(marker))
    head_len = (budget + 1) // 2
    tail_len = budget // 2
    omitted = len(text) - head_len - tail_len
    marker = marker_template.format(omitted=omitted)
    while head_len + tail_len + len(marker) > max_chars and (head_len or tail_len):
        if head_len >= tail_len and head_len > 0:
            head_len -= 1
        elif tail_len > 0:
            tail_len -= 1
        omitted = len(text) - head_len - tail_len
        marker = marker_template.format(omitted=omitted)
    tail = text[len(text) - tail_len :].lstrip() if tail_len else ""
    return text[:head_len].rstrip() + marker + tail
