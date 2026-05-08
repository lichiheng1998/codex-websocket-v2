"""CodexSession composition root.

One ``CodexSession`` is created per hermes session. It owns the task map,
session defaults, event bus, and one long-lived ``CodexBridge``. Larger
behavior groups live in mixins next to this file; external imports keep using
``codex_websocket_v2.core.session.CodexSession``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

from ..events.action_factory import ActionFactory
from ..events.bus import EventBus
from ..surfaces.notify import notify_user
from ..transport.bridge import CodexBridge
from .policies import (
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_MODEL,
    DEFAULT_SANDBOX_POLICY,
    STARTUP_TIMEOUT,
)
from .provider import ProviderInfo, sync_default_model
from .session_drive import DriveMixin
from .session_requests import RequestResolutionMixin
from .session_settings import SessionSettingsMixin
from .session_tasks import TaskOperationsMixin
from .state import Result, Task, TaskTarget, ok

logger = logging.getLogger(__name__)


class CodexSession(
    TaskOperationsMixin,
    RequestResolutionMixin,
    SessionSettingsMixin,
    DriveMixin,
):
    def __init__(self, session_key: str, target: TaskTarget) -> None:
        self.session_key = session_key
        self.target = target

        self.default_model: str = DEFAULT_MODEL
        self.mode: str = "default"  # "plan" | "default"
        self.verbose: str = "off"  # "off" | "mid" | "on"
        self.sandbox_policy: str = DEFAULT_SANDBOX_POLICY
        self.approval_policy: str = DEFAULT_APPROVAL_POLICY
        self.tasks: Dict[str, Task] = {}
        self._provider: ProviderInfo = ProviderInfo()
        self.event_bus = EventBus()
        from ..events.subscribers import register_default_subscribers

        register_default_subscribers(self.event_bus, self)

        self.bridge: CodexBridge = CodexBridge(session=self)
        self._start_lock = threading.Lock()

        self.action_factory = ActionFactory(self)
        self._register_action_subscribers()

    # ── Action subscriber registration ────────────────────────────────────────

    def _register_action_subscribers(self) -> None:
        from ..events.action_models import (
            AnswerActionEvent, ApproveActionEvent, ArchiveActionEvent,
            DenyActionEvent, GetApprovalActionEvent, GetModelActionEvent,
            GetPlanActionEvent, GetSandboxActionEvent, GetVerboseActionEvent,
            ListModelsActionEvent, ListTasksActionEvent, QueryStatusActionEvent,
            RemoveActionEvent, ReplyActionEvent, RespondActionEvent,
            ReviveActionEvent, SetApprovalActionEvent, SetModelActionEvent,
            SetPlanActionEvent, SetSandboxActionEvent, SetVerboseActionEvent,
            ShowPendingActionEvent, StartTaskActionEvent, SteerActionEvent,
            StopActionEvent,
        )
        from ..events.subscribers.task_actions import TaskActionSubscriber
        from ..events.subscribers.approval_actions import ApprovalActionSubscriber
        from ..events.subscribers.settings_actions import SettingsActionSubscriber
        from ..events.subscribers.query_actions import QueryActionSubscriber

        task_sub = TaskActionSubscriber(self)
        for cls in (StartTaskActionEvent, ReplyActionEvent, SteerActionEvent,
                    StopActionEvent, ReviveActionEvent, RemoveActionEvent):
            self.event_bus.subscribe(cls, task_sub)

        approval_sub = ApprovalActionSubscriber(self)
        for cls in (ApproveActionEvent, DenyActionEvent, AnswerActionEvent,
                    RespondActionEvent):
            self.event_bus.subscribe(cls, approval_sub)

        settings_sub = SettingsActionSubscriber(self)
        for cls in (SetModelActionEvent, GetModelActionEvent, SetPlanActionEvent,
                    GetPlanActionEvent, SetVerboseActionEvent, GetVerboseActionEvent,
                    SetSandboxActionEvent, GetSandboxActionEvent,
                    SetApprovalActionEvent, GetApprovalActionEvent):
            self.event_bus.subscribe(cls, settings_sub)

        query_sub = QueryActionSubscriber(self)
        for cls in (ListTasksActionEvent, ListModelsActionEvent,
                    QueryStatusActionEvent, ShowPendingActionEvent,
                    ArchiveActionEvent):
            self.event_bus.subscribe(cls, query_sub)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def ensure_started(self) -> Result:
        if self.bridge.is_connected():
            return ok()
        with self._start_lock:
            if self.bridge.is_connected():
                return ok()

            connect = self.bridge.ensure_connected()
            if not connect["ok"]:
                return connect

            if connect.get("connected"):
                sync = self.bridge.run_sync(self._sync_config_from_server(), timeout=STARTUP_TIMEOUT)
                if sync["ok"]:
                    self.default_model = sync["model"]
                else:
                    logger.warning("codex session: failed to sync default model: %s", sync["error"])

            return ok()

    async def ensure_started_async(self) -> Result:
        """Async version of ensure_started — for subscribers on the bridge loop."""
        if self.bridge.is_connected():
            return ok()
        with self._start_lock:
            if self.bridge.is_connected():
                return ok()

            connect = self.bridge.ensure_connected()
            if not connect["ok"]:
                return connect

            if connect.get("connected"):
                sync = await self._sync_config_from_server()
                if sync["ok"]:
                    self.default_model = sync["model"]
                else:
                    logger.warning("codex session: failed to sync default model: %s", sync["error"])

            return ok()

    def shutdown(self) -> None:
        try:
            self.bridge.close()
        except Exception:
            pass

    async def _sync_config_from_server(self) -> Result:
        result, provider = await sync_default_model(self.bridge.rpc)
        self._provider = provider
        return result

    # ── Narrow handler interface ─────────────────────────────────────────────

    def task_for_thread(self, thread_id: Optional[str]) -> Optional[Task]:
        if not thread_id:
            return None
        for task in self.tasks.values():
            if task.thread_id == thread_id:
                return task
        return None

    def stash_request(
        self,
        task: Optional[Task],
        rpc_id: Any,
        request_type: str,
        payload: Dict[str, Any],
        *,
        request_schema: Optional[Dict[str, Any]] = None,
    ) -> None:
        if task is None:
            return
        task.request_rpc_id = rpc_id
        task.request_type = request_type
        task.request_payload = payload
        task.request_schema = request_schema

    async def notify(self, text: str) -> None:
        await notify_user(self.target, text)
