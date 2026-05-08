"""Typed action events for outbound tool calls and slash commands.

These events are published on the EventBus via the ActionEventBus consumer.
Action event types are disjoint from notification event types so the two
worlds share the same EventBus without collision.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class BaseActionEvent:
    """Root of all action events."""

    session: Any
    result_future: asyncio.Future


# ── Task lifecycle ──────────────────────────────────────────────────────────


@dataclass
class StartTaskActionEvent(BaseActionEvent):
    cwd: str = ""
    prompt: str = ""
    model: Optional[str] = None
    plan: Optional[str] = None
    sandbox_policy: Optional[str] = None
    approval_policy: Optional[str] = None
    base_instructions: Optional[str] = None


@dataclass
class ReplyActionEvent(BaseActionEvent):
    task_id: str = ""
    message: str = ""


@dataclass
class SteerActionEvent(BaseActionEvent):
    task_id: str = ""
    message: str = ""


@dataclass
class StopActionEvent(BaseActionEvent):
    task_id: str = ""


@dataclass
class ReviveActionEvent(BaseActionEvent):
    thread_id: str = ""
    model: Optional[str] = None
    plan: Optional[str] = None
    sandbox_policy: Optional[str] = None
    approval_policy: Optional[str] = None


@dataclass
class RemoveActionEvent(BaseActionEvent):
    task_id: Optional[str] = None
    all: bool = False


# ── Request resolution ──────────────────────────────────────────────────────


@dataclass
class ApproveActionEvent(BaseActionEvent):
    task_id: str = ""
    for_session: bool = False


@dataclass
class DenyActionEvent(BaseActionEvent):
    task_id: str = ""


@dataclass
class AnswerActionEvent(BaseActionEvent):
    task_id: str = ""
    responses: Optional[list[str]] = None
    answers: Optional[list[list[str]]] = None


@dataclass
class RespondActionEvent(BaseActionEvent):
    task_id: str = ""
    content: Optional[Dict[str, Any]] = None


# ── Settings ────────────────────────────────────────────────────────────────


@dataclass
class SetModelActionEvent(BaseActionEvent):
    model_id: str = ""
    task_id: Optional[str] = None


@dataclass
class GetModelActionEvent(BaseActionEvent):
    task_id: Optional[str] = None


@dataclass
class SetPlanActionEvent(BaseActionEvent):
    plan: str = ""
    task_id: Optional[str] = None


@dataclass
class GetPlanActionEvent(BaseActionEvent):
    task_id: Optional[str] = None


@dataclass
class SetVerboseActionEvent(BaseActionEvent):
    level: str = ""


@dataclass
class GetVerboseActionEvent(BaseActionEvent):
    pass


@dataclass
class SetSandboxActionEvent(BaseActionEvent):
    policy: str = ""
    task_id: Optional[str] = None


@dataclass
class GetSandboxActionEvent(BaseActionEvent):
    task_id: Optional[str] = None


@dataclass
class SetApprovalActionEvent(BaseActionEvent):
    policy: str = ""
    task_id: Optional[str] = None


@dataclass
class GetApprovalActionEvent(BaseActionEvent):
    task_id: Optional[str] = None


# ── Query ───────────────────────────────────────────────────────────────────


@dataclass
class ListTasksActionEvent(BaseActionEvent):
    show_threads: bool = False


@dataclass
class ListModelsActionEvent(BaseActionEvent):
    pass


@dataclass
class QueryStatusActionEvent(BaseActionEvent):
    task_id: Optional[str] = None


@dataclass
class ShowPendingActionEvent(BaseActionEvent):
    task_id: str = ""


@dataclass
class ArchiveActionEvent(BaseActionEvent):
    target: str = ""
