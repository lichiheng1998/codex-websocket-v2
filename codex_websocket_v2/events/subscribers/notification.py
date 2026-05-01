"""Subscriber for server notification events."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..models import ItemCompletedEvent, TurnCompletedEvent

if TYPE_CHECKING:
    from ...core.session import CodexSession
    from ...core.state import Task

logger = logging.getLogger(__name__)

MAX_NOTIFY_TEXT = 4000
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

    async def __call__(self, event: ItemCompletedEvent | TurnCompletedEvent) -> bool:
        if isinstance(event, ItemCompletedEvent):
            if event.task is not None:
                await self._safe(self._item_completed(event))
            return True
        if isinstance(event, TurnCompletedEvent):
            if event.task is not None:
                await self._safe(self._turn_completed(event))
            return True
        return False

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except Exception as exc:
            logger.warning("codex handler task failed: %s", exc)

    async def _item_completed(self, event: ItemCompletedEvent) -> None:
        task = event.task
        item = event.item
        item_type = event.item_type
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
        max_text = MAX_NOTIFY_TEXT - len(prefix)
        if len(text) > max_text:
            text = text[:max_text] + "\n…(truncated)"
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
            lines.append(f"```\n{output[:MAX_COMMAND_OUTPUT]}\n```")
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

    async def _turn_completed(self, event: TurnCompletedEvent) -> None:
        task = event.task
        turn = event.turn
        status = event.status
        task.last_turn_status = status

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
                f"Continue: `/codex reply {task.task_id} <message>`",
            )

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
