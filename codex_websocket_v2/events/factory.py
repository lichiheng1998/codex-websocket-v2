"""Convert parsed WebSocket frames into typed events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ..transport import wire
from .subscribers.approval import (
    FILE_CHANGE_APPROVAL,
    LEGACY_APPLY_PATCH_APPROVAL,
    LEGACY_EXEC_APPROVAL,
    MODERN_COMMAND_APPROVAL,
    PERMISSIONS_APPROVAL,
)
from .models import (
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

if TYPE_CHECKING:
    from ..core.session import CodexSession

APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": MODERN_COMMAND_APPROVAL,
    "item/fileChange/requestApproval": FILE_CHANGE_APPROVAL,
    "item/permissions/requestApproval": PERMISSIONS_APPROVAL,
    "execCommandApproval": LEGACY_EXEC_APPROVAL,
    "applyPatchApproval": LEGACY_APPLY_PATCH_APPROVAL,
}
TASK_SCOPED_REQUESTS = set(APPROVAL_METHODS) | {
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
}
TASK_SCOPED_NOTIFICATIONS = {
    "item/started",
    "item/completed",
    "turn/started",
    "turn/completed",
}


class EventFactory:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    def from_raw(self, raw: dict) -> Any:
        kind, parsed, raw = wire.parse_incoming(raw)
        if kind == "response":
            return RpcResponseEvent(session=self.session, raw=raw, rpc_id=parsed.id.root, result=parsed.result)
        if kind == "error":
            return RpcErrorEvent(session=self.session, raw=raw, rpc_id=parsed.id.root, error=parsed.error)
        if kind == "request":
            return self._request_event(parsed, raw)
        if kind == "notification":
            return self._notification_event(parsed, raw)
        return UnknownFrameEvent(session=self.session, raw=raw, parsed=parsed, reason="unparseable")

    def _request_event(self, req: Any, raw: dict) -> Any:
        method = req.method.value
        rpc_id = req.id.root
        params = req.params
        if method in TASK_SCOPED_REQUESTS:
            thread_id, task, task_id = self._task_context(method, params)
            if thread_id and task is None:
                return self._unbound_task_event(raw, method, params, rpc_id, thread_id)
        else:
            thread_id, task, task_id = None, None, "?"

        if method in APPROVAL_METHODS:
            return ApprovalRequestedEvent(
                session=self.session,
                raw=raw,
                method=method,
                rpc_id=rpc_id,
                params=params,
                approval_kind=APPROVAL_METHODS[method],
                thread_id=thread_id,
                task=task,
                task_id=task_id,
            )
        if method == "item/tool/requestUserInput":
            return UserInputRequestedEvent(
                session=self.session,
                raw=raw,
                method=method,
                rpc_id=rpc_id,
                params=params,
                thread_id=thread_id,
                task=task,
                task_id=task_id,
            )
        if method == "mcpServer/elicitation/request":
            return ElicitationRequestedEvent(
                session=self.session,
                raw=raw,
                method=method,
                rpc_id=rpc_id,
                params=params,
                thread_id=thread_id,
                task=task,
                task_id=task_id,
            )
        return UnknownRequestEvent(session=self.session, raw=raw, method=method, rpc_id=rpc_id, params=params)

    def _notification_event(self, notif: Any, raw: dict) -> Any:
        method = notif.method.value
        params = notif.params
        if method in TASK_SCOPED_NOTIFICATIONS:
            thread_id, task, _ = self._task_context(method, params)
            if thread_id and task is None:
                return self._unbound_task_event(raw, method, params, None, thread_id)
        else:
            thread_id, task = None, None

        if method == "item/started":
            item = params.item.root
            item_type = getattr(getattr(item, "type", None), "value", None) or getattr(item, "type", "")
            return ItemStartedEvent(
                session=self.session,
                raw=raw,
                method=method,
                params=params,
                thread_id=thread_id,
                task=task,
                item=item,
                item_type=item_type,
            )

        if method == "item/completed":
            item = params.item.root
            item_type = getattr(getattr(item, "type", None), "value", None) or getattr(item, "type", "")
            return ItemCompletedEvent(
                session=self.session,
                raw=raw,
                method=method,
                params=params,
                thread_id=thread_id,
                task=task,
                item=item,
                item_type=item_type,
            )

        if method == "turn/completed":
            turn = params.turn
            status = getattr(turn.status, "value", turn.status)
            return TurnCompletedEvent(
                session=self.session,
                raw=raw,
                method=method,
                params=params,
                thread_id=thread_id,
                task=task,
                turn=turn,
                status=status,
            )
        if method == "turn/started":
            return TurnStartedEvent(
                session=self.session,
                raw=raw,
                method=method,
                params=params,
                thread_id=thread_id,
                task=task,
                turn=params.turn,
            )
        if method == "serverRequest/resolved":
            return ServerRequestResolvedEvent(
                session=self.session,
                raw=raw,
                method=method,
                params=params,
                request_id=getattr(params, "requestId", None),
            )
        return UnknownNotificationEvent(session=self.session, raw=raw, method=method, params=params)

    def _thread_id(self, params: Any) -> Optional[str]:
        return getattr(params, "threadId", None) or getattr(params, "conversationId", None)

    def _thread_id_for_method(self, method: str, params: Any) -> Optional[str]:
        if method == "mcpServer/elicitation/request":
            inner = params.root if hasattr(params, "root") else params
            return getattr(inner, "threadId", None)
        return self._thread_id(params)

    def _task_context(self, method: str, params: Any) -> tuple[Optional[str], Any, str]:
        thread_id = self._thread_id_for_method(method, params)
        task, task_id = self._task_meta(thread_id)
        return thread_id, task, task_id

    def _task_meta(self, thread_id: Optional[str]) -> tuple[Any, str]:
        task = self.session.task_for_thread(thread_id) if thread_id else None
        return task, task.task_id if task else "?"

    def _unbound_task_event(
        self,
        raw: dict,
        method: str,
        params: Any,
        rpc_id: Any,
        thread_id: str,
    ) -> UnboundTaskEvent:
        return UnboundTaskEvent(
            session=self.session,
            raw=raw,
            method=method,
            rpc_id=rpc_id,
            params=params,
            thread_id=thread_id,
        )
