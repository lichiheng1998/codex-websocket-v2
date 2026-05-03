"""Typed events emitted from inbound app-server WebSocket frames."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class BaseEvent:
    session: Any
    raw: dict


@dataclass
class UnknownFrameEvent(BaseEvent):
    parsed: Any = None
    reason: str = ""


@dataclass
class RpcResponseEvent(BaseEvent):
    rpc_id: Any = None
    result: Any = None


@dataclass
class RpcErrorEvent(BaseEvent):
    rpc_id: Any = None
    error: Any = None


@dataclass
class ServerRequestEvent(BaseEvent):
    method: str = ""
    rpc_id: Any = None
    params: Any = None


@dataclass
class ApprovalRequestedEvent(ServerRequestEvent):
    approval_kind: str = ""
    thread_id: Optional[str] = None
    task: Any = None
    task_id: str = "?"


@dataclass
class UserInputRequestedEvent(ServerRequestEvent):
    thread_id: Optional[str] = None
    task: Any = None
    task_id: str = "?"


@dataclass
class ElicitationRequestedEvent(ServerRequestEvent):
    thread_id: Optional[str] = None
    task: Any = None
    task_id: str = "?"


@dataclass
class UnknownRequestEvent(ServerRequestEvent):
    pass


@dataclass
class ServerNotificationEvent(BaseEvent):
    method: str = ""
    params: Any = None


@dataclass
class ItemStartedEvent(ServerNotificationEvent):
    thread_id: Optional[str] = None
    task: Any = None
    item: Any = None
    item_type: str = ""


@dataclass
class ItemCompletedEvent(ServerNotificationEvent):
    thread_id: Optional[str] = None
    task: Any = None
    item: Any = None
    item_type: str = ""


@dataclass
class TurnCompletedEvent(ServerNotificationEvent):
    thread_id: Optional[str] = None
    task: Any = None
    turn: Any = None
    status: str = ""


@dataclass
class ServerRequestResolvedEvent(ServerNotificationEvent):
    request_id: Any = None


@dataclass
class UnknownNotificationEvent(ServerNotificationEvent):
    pass
