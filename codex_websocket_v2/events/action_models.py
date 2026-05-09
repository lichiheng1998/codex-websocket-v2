"""Action event dataclasses for the outbound tool-call bus.

Tools submit one of these events into ``ActionEventBus``. Each concrete
class corresponds to one ``(map_name, action)`` pair so that ``EventBus``
type-dispatch routes it to exactly one subscriber.

All events carry ``args: dict`` (the raw args dict from tools.py) so
subscribers can extract fields directly, matching the validation logic
already present in the tool handlers.

``result_future`` is a ``concurrent.futures.Future``: the subscriber calls
``set_result`` on the bridge loop; tools.py calls ``.result(timeout=60)``
from its own thread — fully thread-safe without any asyncio wiring on the
caller side.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BaseActionEvent:
    session: Any
    result_future: concurrent.futures.Future
    args: dict = field(default_factory=dict)


# ── Task lifecycle ────────────────────────────────────────────────────────────

@dataclass
class StartTaskEvent(BaseActionEvent):
    pass


@dataclass
class ReplyEvent(BaseActionEvent):
    pass


@dataclass
class SteerEvent(BaseActionEvent):
    pass


@dataclass
class StopEvent(BaseActionEvent):
    pass


@dataclass
class ReviveEvent(BaseActionEvent):
    pass


@dataclass
class RemoveEvent(BaseActionEvent):
    pass


# ── Request resolution ────────────────────────────────────────────────────────

@dataclass
class ApproveEvent(BaseActionEvent):
    pass


@dataclass
class DenyEvent(BaseActionEvent):
    pass


@dataclass
class RespondEvent(BaseActionEvent):
    pass


@dataclass
class InputEvent(BaseActionEvent):
    pass


# ── Model ─────────────────────────────────────────────────────────────────────

@dataclass
class ListModelsEvent(BaseActionEvent):
    pass


@dataclass
class GetModelEvent(BaseActionEvent):
    pass


@dataclass
class SetModelEvent(BaseActionEvent):
    pass


# ── Session settings ──────────────────────────────────────────────────────────

@dataclass
class GetStatusEvent(BaseActionEvent):
    pass


@dataclass
class GetPlanEvent(BaseActionEvent):
    pass


@dataclass
class SetPlanEvent(BaseActionEvent):
    pass


@dataclass
class GetVerboseEvent(BaseActionEvent):
    pass


@dataclass
class SetVerboseEvent(BaseActionEvent):
    pass


@dataclass
class GetSandboxEvent(BaseActionEvent):
    pass


@dataclass
class SetSandboxEvent(BaseActionEvent):
    pass


@dataclass
class GetApprovalPolicyEvent(BaseActionEvent):
    pass


@dataclass
class SetApprovalPolicyEvent(BaseActionEvent):
    pass


# ── Query ─────────────────────────────────────────────────────────────────────

@dataclass
class ListTasksEvent(BaseActionEvent):
    pass


@dataclass
class ShowPendingEvent(BaseActionEvent):
    pass


@dataclass
class ArchiveEvent(BaseActionEvent):
    pass


# ── Routing table ─────────────────────────────────────────────────────────────

EVENT_MAP: dict[tuple[str, str], type[BaseActionEvent]] = {
    ("task", "list"):             ListTasksEvent,
    ("task", "show_pending"):     ShowPendingEvent,
    ("task", "archive"):          ArchiveEvent,
    ("approval", "approve"):      ApproveEvent,
    ("approval", "deny"):         DenyEvent,
    ("action", "reply"):          ReplyEvent,
    ("action", "answer"):         InputEvent,
    ("action", "respond"):        RespondEvent,
    ("action", "steer"):          SteerEvent,
    ("action", "stop"):           StopEvent,
    ("model", "list"):            ListModelsEvent,
    ("model", "get"):             GetModelEvent,
    ("model", "set"):             SetModelEvent,
    ("session", "status"):        GetStatusEvent,
    ("session", "plan_get"):      GetPlanEvent,
    ("session", "plan_set"):      SetPlanEvent,
    ("session", "verbose_get"):   GetVerboseEvent,
    ("session", "verbose_set"):   SetVerboseEvent,
    ("session", "sandbox_get"):   GetSandboxEvent,
    ("session", "sandbox_set"):   SetSandboxEvent,
    ("session", "approval_get"):  GetApprovalPolicyEvent,
    ("session", "approval_set"):  SetApprovalPolicyEvent,
}


def make_event(map_name: str, action: str, session: Any, args: dict) -> BaseActionEvent:
    """Create a typed action event for the given (map_name, action) pair.

    Raises ``KeyError`` if the combination is unknown.
    """
    cls = EVENT_MAP.get((map_name, action))
    if cls is None:
        raise KeyError(f"unknown action ({map_name!r}, {action!r})")
    return cls(
        session=session,
        result_future=concurrent.futures.Future(),
        args=args,
    )
