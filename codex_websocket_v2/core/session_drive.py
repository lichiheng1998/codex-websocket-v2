"""Fire-and-forget turn drive functions for ``CodexSession``."""

from __future__ import annotations

from typing import Any, Optional

from ..surfaces.notify import report_failure
from ..transport import wire
from .policies import (
    default_collaboration_mode,
    plan_collaboration_mode,
    prepare_sandbox,
)
from .state import Task
from .utils import extract_thread_id


class DriveMixin:
    def _build_turn_start(
        self,
        *,
        thread_id: str,
        text: str,
        cwd: str,
        model: str,
        plan: bool,
        sandbox_policy: str,
        approval_policy: str,
    ) -> "wire.TurnStartParams":
        return wire.TurnStartParams(
            threadId=thread_id,
            input=[{"type": "text", "text": text}],
            model=model,
            approvalPolicy=approval_policy,
            sandboxPolicy=prepare_sandbox(sandbox_policy, cwd),
            collaborationMode=(
                plan_collaboration_mode(model)
                if plan
                else default_collaboration_mode(model)
            ),
        )

    @staticmethod
    def _record_active_turn_from_result(task: Optional[Task], result: Any) -> None:
        if task is None or result is None:
            return
        if isinstance(result, dict):
            turn = result.get("turn")
        else:
            turn = getattr(result, "turn", None)

        if isinstance(turn, dict):
            turn_id = turn.get("id")
        else:
            turn_id = getattr(turn, "id", None)
        if turn_id:
            task.active_turn_id = str(turn_id)

    async def _drive_task(
        self,
        *,
        task_id: str,
        cwd: str,
        prompt: str,
        model: str,
        plan: bool,
        approval_policy: str,
        sandbox_policy: str,
        base_instructions: Optional[str],
    ) -> None:
        thread_rpc = await self.bridge.rpc(
            "thread/start",
            wire.ThreadStartParams(
                cwd=cwd,
                model=model,
                approvalPolicy=approval_policy,
                baseInstructions=base_instructions,
            ),
        )
        if not thread_rpc["ok"]:
            await report_failure(self.target, task_id, "thread/start failed", thread_rpc["error"])
            return

        thread_id = extract_thread_id(thread_rpc["result"])
        if not thread_id:
            await report_failure(self.target, task_id, "thread/start", "no thread id in response")
            return

        self.tasks[task_id] = Task(
            task_id=task_id, thread_id=thread_id, cwd=cwd,
            model=model,
            plan=plan,
            sandbox_policy=sandbox_policy, approval_policy=approval_policy,
        )

        await self.notify(
            f"🤖 Codex task `{task_id}` started\n"
            f"cwd: `{cwd}`\nmodel: `{model}`"
            + ("\nplan: `on`" if plan else "")
        )

        turn_rpc = await self.bridge.rpc(
            "turn/start",
            self._build_turn_start(
                thread_id=thread_id, text=prompt, cwd=cwd,
                model=model, plan=plan,
                sandbox_policy=sandbox_policy, approval_policy=approval_policy,
            ),
        )
        if not turn_rpc["ok"]:
            self.tasks.pop(task_id, None)
            await report_failure(self.target, task_id, "turn/start failed", turn_rpc["error"])
            return
        self._record_active_turn_from_result(self.tasks.get(task_id), turn_rpc.get("result"))

    async def _drive_reply(self, task_id: str, message: str) -> None:
        task = self.tasks.get(task_id)
        if task is None:
            await report_failure(self.target, task_id, "reply failed", "task not found")
            return

        rpc = await self.bridge.rpc(
            "turn/start",
            self._build_turn_start(
                thread_id=task.thread_id, text=message, cwd=task.cwd,
                model=self._task_model(task),
                plan=self._task_plan(task),
                sandbox_policy=self._task_sandbox_policy(task),
                approval_policy=self._task_approval_policy(task),
            ),
        )
        if not rpc["ok"]:
            await report_failure(self.target, task_id, "reply failed", rpc["error"])
            return
        self._record_active_turn_from_result(task, rpc.get("result"))
