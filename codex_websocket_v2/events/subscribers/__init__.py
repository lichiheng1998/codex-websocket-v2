"""Default subscriber registration."""

from __future__ import annotations

from .approval import ApprovalRequestSubscriber
from .elicitation import ElicitationSubscriber
from .input import UserInputRequestSubscriber
from .notification import NotificationSubscriber
from .rpc import RpcErrorSubscriber, RpcResponseSubscriber
from .unhandled import (
    UnboundTaskSubscriber,
    UnknownFrameSubscriber,
    UnhandledNotificationSubscriber,
    UnhandledRequestSubscriber,
)
from ..models import (
    ApprovalRequestedEvent,
    ElicitationRequestedEvent,
    ItemCompletedEvent,
    ItemStartedEvent,
    RpcErrorEvent,
    RpcResponseEvent,
    ServerRequestResolvedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
    UnboundTaskEvent,
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
    bus.subscribe(UnboundTaskEvent, UnboundTaskSubscriber())

    notification = NotificationSubscriber(session)
    bus.subscribe(ItemStartedEvent, notification)
    bus.subscribe(ItemCompletedEvent, notification)
    bus.subscribe(TurnStartedEvent, notification)
    bus.subscribe(TurnCompletedEvent, notification)
    bus.subscribe(ServerRequestResolvedEvent, notification)

    bus.subscribe(UnknownRequestEvent, UnhandledRequestSubscriber(session))
    bus.subscribe(UnknownNotificationEvent, UnhandledNotificationSubscriber())
    bus.subscribe(UnknownFrameEvent, UnknownFrameSubscriber())


def register_action_subscribers(bus, session) -> None:
    """Register subscribers that handle outbound tool-call action events."""
    from .task_actions import (
        StartTaskSubscriber,
        ReplySubscriber,
        SteerSubscriber,
        StopSubscriber,
        ReviveSubscriber,
        RemoveSubscriber,
    )
    from .approval_actions import (
        ApproveSubscriber,
        DenySubscriber,
        RespondSubscriber,
        InputSubscriber,
    )
    from .settings_actions import (
        ListModelsSubscriber,
        GetModelSubscriber,
        SetModelSubscriber,
        GetStatusSubscriber,
        GetPlanSubscriber,
        SetPlanSubscriber,
        GetVerboseSubscriber,
        SetVerboseSubscriber,
        GetSandboxSubscriber,
        SetSandboxSubscriber,
        GetApprovalPolicySubscriber,
        SetApprovalPolicySubscriber,
    )
    from .query_actions import (
        ListTasksSubscriber,
        ShowPendingSubscriber,
        ArchiveSubscriber,
    )
    from ..action_models import (
        StartTaskEvent,
        ReplyEvent,
        SteerEvent,
        StopEvent,
        ReviveEvent,
        RemoveEvent,
        ApproveEvent,
        DenyEvent,
        RespondEvent,
        InputEvent,
        ListModelsEvent,
        GetModelEvent,
        SetModelEvent,
        GetStatusEvent,
        GetPlanEvent,
        SetPlanEvent,
        GetVerboseEvent,
        SetVerboseEvent,
        GetSandboxEvent,
        SetSandboxEvent,
        GetApprovalPolicyEvent,
        SetApprovalPolicyEvent,
        ListTasksEvent,
        ShowPendingEvent,
        ArchiveEvent,
    )

    bus.subscribe(StartTaskEvent, StartTaskSubscriber())
    bus.subscribe(ReplyEvent, ReplySubscriber())
    bus.subscribe(SteerEvent, SteerSubscriber())
    bus.subscribe(StopEvent, StopSubscriber())
    bus.subscribe(ReviveEvent, ReviveSubscriber())
    bus.subscribe(RemoveEvent, RemoveSubscriber())

    bus.subscribe(ApproveEvent, ApproveSubscriber())
    bus.subscribe(DenyEvent, DenySubscriber())
    bus.subscribe(RespondEvent, RespondSubscriber())
    bus.subscribe(InputEvent, InputSubscriber())

    bus.subscribe(ListModelsEvent, ListModelsSubscriber())
    bus.subscribe(GetModelEvent, GetModelSubscriber())
    bus.subscribe(SetModelEvent, SetModelSubscriber())
    bus.subscribe(GetStatusEvent, GetStatusSubscriber())
    bus.subscribe(GetPlanEvent, GetPlanSubscriber())
    bus.subscribe(SetPlanEvent, SetPlanSubscriber())
    bus.subscribe(GetVerboseEvent, GetVerboseSubscriber())
    bus.subscribe(SetVerboseEvent, SetVerboseSubscriber())
    bus.subscribe(GetSandboxEvent, GetSandboxSubscriber())
    bus.subscribe(SetSandboxEvent, SetSandboxSubscriber())
    bus.subscribe(GetApprovalPolicyEvent, GetApprovalPolicySubscriber())
    bus.subscribe(SetApprovalPolicyEvent, SetApprovalPolicySubscriber())

    bus.subscribe(ListTasksEvent, ListTasksSubscriber())
    bus.subscribe(ShowPendingEvent, ShowPendingSubscriber())
    bus.subscribe(ArchiveEvent, ArchiveSubscriber())
