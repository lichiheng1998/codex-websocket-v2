"""Default subscriber registration."""

from __future__ import annotations

from .approval import ApprovalRequestSubscriber
from .elicitation import ElicitationSubscriber
from .input import UserInputRequestSubscriber
from .notification import NotificationSubscriber
from .rpc import RpcErrorSubscriber, RpcResponseSubscriber
from .unhandled import UnknownFrameSubscriber, UnhandledNotificationSubscriber, UnhandledRequestSubscriber
from ..models import (
    ApprovalRequestedEvent,
    ElicitationRequestedEvent,
    ItemCompletedEvent,
    RpcErrorEvent,
    RpcResponseEvent,
    TurnCompletedEvent,
    UnknownFrameEvent,
    UnknownNotificationEvent,
    UnknownRequestEvent,
    UserInputRequestedEvent,
)


def register_default_subscribers(bus, session) -> None:
    bus.subscribe(RpcResponseEvent, RpcResponseSubscriber(session))
    bus.subscribe(RpcErrorEvent, RpcErrorSubscriber(session))
    bus.subscribe(ApprovalRequestedEvent, ApprovalRequestSubscriber(session))
    bus.subscribe(UserInputRequestedEvent, UserInputRequestSubscriber(session))
    bus.subscribe(ElicitationRequestedEvent, ElicitationSubscriber(session))

    notification = NotificationSubscriber(session)
    bus.subscribe(ItemCompletedEvent, notification)
    bus.subscribe(TurnCompletedEvent, notification)

    bus.subscribe(UnknownRequestEvent, UnhandledRequestSubscriber(session))
    bus.subscribe(UnknownNotificationEvent, UnhandledNotificationSubscriber())
    bus.subscribe(UnknownFrameEvent, UnknownFrameSubscriber())
