"""Create typed action events from tool call arguments.

``ActionFactory`` maps ``(tool_name, args)`` to a concrete ``BaseActionEvent``
subclass with a pre-created ``asyncio.Future`` for result delivery.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .action_models import (
    AnswerActionEvent,
    ApproveActionEvent,
    ArchiveActionEvent,
    BaseActionEvent,
    DenyActionEvent,
    GetApprovalActionEvent,
    GetModelActionEvent,
    GetPlanActionEvent,
    GetSandboxActionEvent,
    GetVerboseActionEvent,
    ListModelsActionEvent,
    ListTasksActionEvent,
    QueryStatusActionEvent,
    RemoveActionEvent,
    ReplyActionEvent,
    RespondActionEvent,
    ReviveActionEvent,
    SetApprovalActionEvent,
    SetModelActionEvent,
    SetPlanActionEvent,
    SetSandboxActionEvent,
    SetVerboseActionEvent,
    ShowPendingActionEvent,
    StartTaskActionEvent,
    SteerActionEvent,
    StopActionEvent,
)

if TYPE_CHECKING:
    from ..core.session import CodexSession

logger = logging.getLogger(__name__)


class ActionFactory:
    """Create action events from tool name + args dict."""

    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    def _future(self) -> asyncio.Future:
        from .action_bus import get_action_bus
        loop = get_action_bus()._loop
        if loop is None:
            raise RuntimeError("action bus loop is not running")
        return loop.create_future()

    def create(self, tool_name: str, args: dict) -> BaseActionEvent:
        s = self.session

        if tool_name == "codex_task":
            return StartTaskActionEvent(
                session=s, result_future=self._future(),
                cwd=args.get("cwd", ""),
                prompt=args.get("prompt", ""),
                model=_opt(args, "model"),
                plan=_opt(args, "plan"),
                sandbox_policy=_opt(args, "sandbox_policy"),
                approval_policy=_opt(args, "approval_policy"),
                base_instructions=args.get("base_instructions"),
            )

        if tool_name == "codex_revive":
            return ReviveActionEvent(
                session=s, result_future=self._future(),
                thread_id=(args.get("thread_id") or "").strip(),
                model=_opt(args, "model"),
                plan=_opt(args, "plan"),
                sandbox_policy=_opt(args, "sandbox_policy"),
                approval_policy=_opt(args, "approval_policy"),
            )

        if tool_name == "codex_action":
            return self._action_event(args)

        if tool_name == "codex_approval":
            return self._approval_event(args)

        if tool_name == "codex_models":
            return self._models_event(args)

        if tool_name == "codex_session":
            return self._session_event(args)

        if tool_name == "codex_tasks":
            return self._tasks_event(args)

        if tool_name == "codex_remove":
            return RemoveActionEvent(
                session=s, result_future=self._future(),
                task_id=_opt(args, "task_id"),
                all=bool(args.get("all")),
            )

        raise ValueError(f"unknown tool: {tool_name}")

    def _action_event(self, args: dict) -> BaseActionEvent:
        s = self.session
        f = self._future()
        action = (args.get("action") or "").strip()

        if action == "reply":
            return ReplyActionEvent(
                session=s, result_future=f,
                task_id=args.get("task_id", ""),
                message=args.get("message", ""),
            )
        if action == "steer":
            return SteerActionEvent(
                session=s, result_future=f,
                task_id=args.get("task_id", ""),
                message=args.get("message", ""),
            )
        if action == "stop":
            return StopActionEvent(
                session=s, result_future=f,
                task_id=args.get("task_id", ""),
            )
        if action == "answer":
            return AnswerActionEvent(
                session=s, result_future=f,
                task_id=args.get("task_id", ""),
                responses=args.get("responses"),
                answers=args.get("answers"),
            )
        if action == "respond":
            return RespondActionEvent(
                session=s, result_future=f,
                task_id=args.get("task_id", ""),
                content=args.get("content"),
            )
        raise ValueError(f"unknown codex_action: {action}")

    def _approval_event(self, args: dict) -> BaseActionEvent:
        s = self.session
        f = self._future()
        action = (args.get("action") or "").strip()

        if action == "approve":
            return ApproveActionEvent(
                session=s, result_future=f,
                task_id=args.get("task_id", ""),
                for_session=bool(args.get("for_session")),
            )
        if action == "deny":
            return DenyActionEvent(
                session=s, result_future=f,
                task_id=args.get("task_id", ""),
            )
        raise ValueError(f"unknown codex_approval: {action}")

    def _models_event(self, args: dict) -> BaseActionEvent:
        s = self.session
        f = self._future()
        action = (args.get("action") or "").strip()

        if action == "list":
            return ListModelsActionEvent(session=s, result_future=f)
        if action == "get":
            return GetModelActionEvent(
                session=s, result_future=f,
                task_id=_opt(args, "task_id"),
            )
        if action == "set":
            return SetModelActionEvent(
                session=s, result_future=f,
                model_id=(args.get("model_id") or "").strip(),
                task_id=_opt(args, "task_id"),
            )
        raise ValueError(f"unknown codex_models: {action}")

    def _session_event(self, args: dict) -> BaseActionEvent:
        s = self.session
        f = self._future()
        action = (args.get("action") or "").strip()

        if action == "status":
            return QueryStatusActionEvent(
                session=s, result_future=f,
                task_id=_opt(args, "task_id"),
            )
        if action == "plan_get":
            return GetPlanActionEvent(
                session=s, result_future=f,
                task_id=_opt(args, "task_id"),
            )
        if action == "plan_set":
            return SetPlanActionEvent(
                session=s, result_future=f,
                plan=(args.get("plan") or "").strip(),
                task_id=_opt(args, "task_id"),
            )
        if action == "verbose_get":
            return GetVerboseActionEvent(session=s, result_future=f)
        if action == "verbose_set":
            return SetVerboseActionEvent(
                session=s, result_future=f,
                level=(args.get("level") or "").strip(),
            )
        if action == "sandbox_get":
            return GetSandboxActionEvent(
                session=s, result_future=f,
                task_id=_opt(args, "task_id"),
            )
        if action == "sandbox_set":
            return SetSandboxActionEvent(
                session=s, result_future=f,
                policy=(args.get("sandbox_policy") or "").strip(),
                task_id=_opt(args, "task_id"),
            )
        if action == "approval_get":
            return GetApprovalActionEvent(
                session=s, result_future=f,
                task_id=_opt(args, "task_id"),
            )
        if action == "approval_set":
            return SetApprovalActionEvent(
                session=s, result_future=f,
                policy=(args.get("approval_policy") or "").strip(),
                task_id=_opt(args, "task_id"),
            )
        raise ValueError(f"unknown codex_session: {action}")

    def _tasks_event(self, args: dict) -> BaseActionEvent:
        s = self.session
        f = self._future()
        action = (args.get("action") or "").strip()

        if action == "list":
            return ListTasksActionEvent(
                session=s, result_future=f,
                show_threads=bool(args.get("show_threads")),
            )
        if action == "show_pending":
            return ShowPendingActionEvent(
                session=s, result_future=f,
                task_id=args.get("task_id", ""),
            )
        if action == "archive":
            return ArchiveActionEvent(
                session=s, result_future=f,
                target=args.get("target", ""),
            )
        raise ValueError(f"unknown codex_tasks: {action}")


def _opt(args: dict, key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    return str(value).strip()
