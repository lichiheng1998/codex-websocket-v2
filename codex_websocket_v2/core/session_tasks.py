"""Task and thread operations for ``CodexSession``."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..transport import wire
from .policies import RPC_TIMEOUT, SHORT_RPC_TIMEOUT
from .state import Result, Task, err, ok
from .utils import new_task_id


class TaskOperationsMixin:
    async def start_task(
        self,
        *,
        cwd: str,
        prompt: str,
        model: Optional[str] = None,
        plan: Any = None,
        approval_policy: Optional[str] = None,
        sandbox_policy: Optional[str] = None,
        base_instructions: Optional[str] = None,
    ) -> Result:
        await self.ensure_started_async()

        resolved = self._resolve_task_policy(
            model=model,
            plan=plan,
            sandbox_policy=sandbox_policy,
            approval_policy=approval_policy,
        )
        if not resolved["ok"]:
            return resolved
        task_id = new_task_id()

        asyncio.create_task(self._drive_task(
            task_id=task_id, cwd=cwd, prompt=prompt,
            model=resolved["model"],
            plan=resolved["plan"],
            approval_policy=resolved["approval_policy"],
            sandbox_policy=resolved["sandbox_policy"],
            base_instructions=base_instructions,
        ))
        return ok(
            task_id=task_id,
            model=resolved["model"],
            plan=self._plan_label(resolved["plan"]),
            sandbox_policy=resolved["sandbox_policy"],
            approval_policy=resolved["approval_policy"],
        )

    async def send_reply(self, task_id: str, message: str) -> Result:
        await self.ensure_started_async()

        task = self.tasks.get(task_id)
        if task is None:
            return err(f"unknown task id {task_id!r}")

        asyncio.create_task(self._drive_reply(task_id, message))
        return ok(task_id=task_id)

    async def steer_task(self, task_id: str, message: str) -> Result:
        await self.ensure_started_async()

        task = self.tasks.get(task_id)
        if task is None:
            return err(f"unknown task id {task_id!r}")

        message = (message or "").strip()
        if not message:
            return err("message is required for steer")

        turn_id = task.active_turn_id
        if not turn_id:
            return err(f"task {task_id!r} has no active turn to steer")

        result = await self.bridge.rpc(
            "turn/steer",
            wire.TurnSteerParams(
                threadId=task.thread_id,
                expectedTurnId=turn_id,
                input=[{"type": "text", "text": message}],
            ),
            timeout=SHORT_RPC_TIMEOUT,
        )
        if not result["ok"]:
            return result
        return ok(task_id=task_id, turn_id=turn_id)

    async def stop_task(self, task_id: str) -> Result:
        await self.ensure_started_async()

        task = self.tasks.get(task_id)
        if task is None:
            return err(f"unknown task id {task_id!r}")

        turn_id = task.active_turn_id
        if not turn_id:
            return err(f"task {task_id!r} has no active turn to stop")

        result = await self.bridge.rpc(
            "turn/interrupt",
            wire.TurnInterruptParams(threadId=task.thread_id, turnId=turn_id),
            timeout=SHORT_RPC_TIMEOUT,
        )
        if not result["ok"]:
            return result
        return ok(task_id=task_id, turn_id=turn_id)

    async def revive_task(
        self,
        thread_id: str,
        *,
        model: Optional[str] = None,
        plan: Any = None,
        sandbox_policy: Optional[str] = None,
        approval_policy: Optional[str] = None,
    ) -> Result:
        await self.ensure_started_async()

        resolved = self._resolve_task_policy(
            model=model,
            plan=plan,
            sandbox_policy=sandbox_policy,
            approval_policy=approval_policy,
        )
        if not resolved["ok"]:
            return resolved

        for task in self.tasks.values():
            if task.thread_id == thread_id:
                return ok(task_id=task.task_id, thread_id=thread_id,
                          message="thread already tracked in this session")

        from . import session_registry

        owner = session_registry.find_thread_owner(thread_id, exclude_key=self.session_key)
        if owner is not None:
            return err(
                f"thread {thread_id!r} is currently held by session {owner!r} — "
                "cannot revive into a different session"
            )

        read = await self.bridge.rpc(
            "thread/read", wire.ThreadReadParams(threadId=thread_id), timeout=RPC_TIMEOUT,
        )
        if not read["ok"]:
            return err(f"thread {thread_id!r} not found: {read['error']}")

        thread_obj = (read["result"] or {}).get("thread") or {}
        if not thread_obj.get("id"):
            return err(f"thread {thread_id!r} not found on server")
        cwd = thread_obj.get("cwd") or ""

        status = thread_obj.get("status") or {}
        if status.get("type") == "notLoaded":
            resumed = await self.bridge.rpc(
                "thread/resume",
                wire.ThreadResumeParams(
                    threadId=thread_id,
                    model=resolved["model"],
                    approvalPolicy=resolved["approval_policy"],
                ),
                timeout=RPC_TIMEOUT,
            )
            if not resumed["ok"]:
                return err(f"thread/resume failed: {resumed['error']}")

        task_id = new_task_id()
        self.tasks[task_id] = Task(
            task_id=task_id, thread_id=thread_id, cwd=cwd,
            model=resolved["model"],
            plan=resolved["plan"],
            sandbox_policy=resolved["sandbox_policy"],
            approval_policy=resolved["approval_policy"],
            thread_status=self._status_type(status),
        )
        return ok(
            task_id=task_id,
            thread_id=thread_id,
            model=resolved["model"],
            plan=self._plan_label(resolved["plan"]),
            sandbox_policy=resolved["sandbox_policy"],
            approval_policy=resolved["approval_policy"],
        )

    def remove_task(self, task_id: str) -> Result:
        task = self.tasks.get(task_id)
        if task is None:
            return err(f"unknown task id {task_id!r}")

        self.tasks.pop(task_id, None)
        return ok(task_id=task_id, thread_id=task.thread_id)

    def remove_all_tasks(self) -> Result:
        task_ids = list(self.tasks.keys())
        removed = []
        for task_id in task_ids:
            result = self.remove_task(task_id)
            if result["ok"]:
                removed.append({
                    "task_id": result.get("task_id"),
                    "thread_id": result.get("thread_id"),
                })
        return {
            "ok": True,
            "removed": len(removed),
            "tasks": removed,
            "errors": [],
        }

    def list_tasks(self) -> Result:
        return ok(data=[
            {
                "task_id": t.task_id,
                "thread_id": t.thread_id,
                "cwd": t.cwd,
                "model": self._task_model(t),
                "plan": self._plan_label(self._task_plan(t)),
                "sandbox_policy": self._task_sandbox_policy(t),
                "approval_policy": self._task_approval_policy(t),
            }
            for t in self.tasks.values()
        ])

    async def list_threads(self, *, limit: Optional[int] = None) -> Result:
        await self.ensure_started_async()

        all_threads: list = []
        cursor: Optional[str] = None
        while True:
            rpc = await self.bridge.rpc(
                "thread/list", wire.ThreadListParams(cursor=cursor, limit=limit), timeout=RPC_TIMEOUT,
            )
            if not rpc["ok"]:
                return rpc
            server_data = rpc["result"] or {}
            page = server_data.get("data") or []
            all_threads.extend(page)
            cursor = server_data.get("nextCursor")
            if not cursor or not page:
                break
        return ok(data=all_threads)

    async def archive_thread(self, thread_id: str) -> Result:
        await self.ensure_started_async()

        thread_id = (thread_id or "").strip()
        if not thread_id:
            return err("thread_id is required")

        owner = self._thread_binding_owner(thread_id)
        if owner is not None:
            return err(f"thread {thread_id!r} is bound to an active task; remove the task binding first")

        archived = await self.bridge.rpc(
            "thread/archive", wire.ThreadArchiveParams(threadId=thread_id), timeout=RPC_TIMEOUT,
        )
        if not archived["ok"]:
            return archived
        return ok(thread_id=thread_id)

    async def archive_all_threads(self) -> Result:
        await self.ensure_started_async()

        listed = await self.list_threads()
        if not listed["ok"]:
            return {"ok": False, "removed": 0, "skipped": [], "errors": [listed["error"]]}

        errors, skipped, removed = [], [], 0
        for t in listed["data"] or []:
            thread_id = t.get("id") or ""
            if not thread_id:
                continue

            owner = self._thread_binding_owner(thread_id)
            if owner is not None:
                skipped.append({"thread_id": thread_id, "owner": owner})
                continue
            archived = await self.bridge.rpc(
                "thread/archive", wire.ThreadArchiveParams(threadId=thread_id), timeout=RPC_TIMEOUT,
            )
            if archived["ok"]:
                removed += 1
            else:
                errors.append(f"{thread_id}: {archived['error']}")

        return {"ok": not errors, "removed": removed, "skipped": skipped, "errors": errors}

    def _thread_binding_owner(self, thread_id: str) -> Optional[str]:
        if any(task.thread_id == thread_id for task in self.tasks.values()):
            return self.session_key

        from . import session_registry

        return session_registry.find_thread_owner(thread_id)
