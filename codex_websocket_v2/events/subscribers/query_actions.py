"""Action subscriber for read-only queries.

Handles: ListTasks, ListModels, QueryStatus, ShowPending, Archive.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ..action_models import (
    ArchiveActionEvent,
    BaseActionEvent,
    ListModelsActionEvent,
    ListTasksActionEvent,
    QueryStatusActionEvent,
    ShowPendingActionEvent,
)
from ...surfaces.tool_actions import jsonable

if TYPE_CHECKING:
    from ...core.session import CodexSession

logger = logging.getLogger(__name__)


def _serialize_task(session: "CodexSession", task) -> dict:
    pending = None
    if task.request_rpc_id is not None:
        pending = {"type": task.request_type}
        if task.request_type == "elicitation" and task.request_schema:
            pending["schema"] = task.request_schema
    return {
        "task_id": task.task_id,
        "thread_id": task.thread_id,
        "cwd": task.cwd,
        "model": getattr(task, "model", session.get_default_model()),
        "plan": "on" if getattr(task, "plan", session.mode == "plan") else "off",
        "sandbox_policy": getattr(task, "sandbox_policy", session.sandbox_policy),
        "approval_policy": getattr(task, "approval_policy", session.approval_policy),
        "pending": pending,
    }


class QueryActionSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    async def __call__(self, event: BaseActionEvent) -> bool:
        s = self.session
        result = None

        try:
            if isinstance(event, ListTasksActionEvent):
                if event.show_threads:
                    result = await s.list_threads()
                else:
                    result = self._list_tasks(s)
            elif isinstance(event, ListModelsActionEvent):
                result = await s.list_models()
            elif isinstance(event, QueryStatusActionEvent):
                result = await s.get_status(event.task_id)
            elif isinstance(event, ShowPendingActionEvent):
                result = self._show_pending(s, event)
            elif isinstance(event, ArchiveActionEvent):
                result = await self._archive(s, event)
            else:
                return False
        except Exception as exc:
            logger.exception("query action failed: %s", exc)
            result = {"ok": False, "error": str(exc)}

        if not event.result_future.done():
            event.result_future.set_result(result)
        return True

    def _list_tasks(self, s: "CodexSession") -> dict:
        return {"ok": True, "tasks": [
            _serialize_task(s, task) for task in s.tasks.values()
        ]}

    def _show_pending(self, s: "CodexSession", event: ShowPendingActionEvent) -> dict:
        task_id = event.task_id
        if not task_id:
            return {"ok": False, "error": "task_id is required for show_pending"}
        task = s.tasks.get(task_id)
        if task is None:
            return {"ok": False, "error": f"unknown task `{task_id}`"}
        if task.request_rpc_id is None:
            return {"ok": True, "task_id": task_id, "pending": None}
        payload = jsonable(task.request_payload or {})
        return {
            "ok": True,
            "task_id": task_id,
            "pending": {
                "type": task.request_type,
                "rpc_id": task.request_rpc_id,
                "message": payload.get("preview", "") if isinstance(payload, dict) else "",
                "payload": payload,
                "schema": jsonable(task.request_schema) if task.request_schema is not None else None,
            },
        }

    async def _archive(self, s: "CodexSession", event: ArchiveActionEvent) -> dict:
        target = event.target
        if not target:
            return {"ok": False, "error": "target is required for archive"}
        if target == "allthreads":
            result = await s.archive_all_threads()
            return {
                "ok": result.get("ok", False),
                "scope": "allthreads",
                "removed": result.get("removed", 0),
                "skipped": result.get("skipped", []),
                "errors": result.get("errors", []),
            }
        result = await s.archive_thread(target)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "unknown error")}
        return {"ok": True, "scope": "thread", "thread_id": result.get("thread_id", target)}
