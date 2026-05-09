"""Action subscribers for query/list/archive operations."""

from __future__ import annotations

import json
import logging

from ...surfaces.tool_actions import (
    error,
    jsonable,
    ok,
    tool_error_from_result,
    _serialize_task,
)
from ..action_models import ArchiveEvent, ListTasksEvent, ShowPendingEvent

logger = logging.getLogger(__name__)


class ListTasksSubscriber:
    async def __call__(self, event: ListTasksEvent) -> bool:
        session = event.session

        if event.args.get("show_threads"):
            try:
                result = await session.list_threads()
            except Exception as exc:
                event.result_future.set_result(error(f"list_threads failed: {exc}"))
                return True
            if err_str := tool_error_from_result(result):
                event.result_future.set_result(err_str)
                return True
            threads = result.get("data") or []
            event.result_future.set_result(ok(threads=threads, total=len(threads)))
            return True

        event.result_future.set_result(ok(
            tasks=[_serialize_task(session, task) for task in session.tasks.values()]
        ))
        return True


class ShowPendingSubscriber:
    async def __call__(self, event: ShowPendingEvent) -> bool:
        args = event.args
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            event.result_future.set_result(error("task_id is required for show_pending"))
            return True

        session = event.session
        task = session.tasks.get(task_id)
        if task is None:
            event.result_future.set_result(error(f"unknown task `{task_id}`"))
            return True

        if task.request_rpc_id is None:
            event.result_future.set_result(ok(task_id=task_id, pending=None))
            return True

        payload = jsonable(task.request_payload or {})
        event.result_future.set_result(ok(
            task_id=task_id,
            pending={
                "type": task.request_type,
                "rpc_id": task.request_rpc_id,
                "message": payload.get("preview", "") if isinstance(payload, dict) else "",
                "payload": payload,
                "schema": jsonable(task.request_schema) if task.request_schema is not None else None,
            },
        ))
        return True


class ArchiveSubscriber:
    async def __call__(self, event: ArchiveEvent) -> bool:
        args = event.args
        target = (args.get("target") or "").strip()
        if not target:
            event.result_future.set_result(error("target is required for archive"))
            return True

        session = event.session

        if target == "allthreads":
            try:
                result = await session.archive_all_threads()
            except Exception as exc:
                event.result_future.set_result(error(f"archive_all_threads failed: {exc}"))
                return True
            event.result_future.set_result(json.dumps({
                "ok": result.get("ok", False),
                "scope": "allthreads",
                "removed": result.get("removed", 0),
                "skipped": result.get("skipped", []),
                "errors": result.get("errors", []),
            }, ensure_ascii=False))
            return True

        if target == "all":
            event.result_future.set_result(
                error("archive target 'all' was removed; use codex_remove with all=true to unbind tasks")
            )
            return True

        try:
            result = await session.archive_thread(target)
        except Exception as exc:
            event.result_future.set_result(error(f"archive_thread failed: {exc}"))
            return True

        if err_str := tool_error_from_result(result):
            event.result_future.set_result(err_str)
            return True

        event.result_future.set_result(ok(scope="thread", thread_id=result.get("thread_id", target)))
        return True
