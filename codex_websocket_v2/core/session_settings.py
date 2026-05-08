"""Session configuration, policy, model, and status helpers."""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..transport import wire
from .policies import RPC_TIMEOUT
from .provider import known_ids_from_listing, list_models_for, list_models_for_async
from .state import Result, Task, err, ok

logger = logging.getLogger(__name__)


class SessionSettingsMixin:
    _VALID_APPROVAL_POLICIES = ("on-request", "on-failure", "never", "untrusted")
    _VALID_SANDBOX_POLICIES = ("read-only", "workspace-write", "danger-full-access")

    @staticmethod
    def _plan_label(plan: bool) -> str:
        return "on" if plan else "off"

    @staticmethod
    def _status_type(status: Any) -> str:
        if isinstance(status, dict):
            return str(status.get("type") or "")
        root = getattr(status, "root", status)
        typ = getattr(root, "type", "")
        return str(getattr(typ, "value", typ) or "")

    def _task_or_error(self, task_id: Optional[str]) -> tuple[Optional[Task], Optional[Result]]:
        if not task_id:
            return None, None
        task = self.tasks.get(task_id)
        if task is None:
            return None, err(f"unknown task id {task_id!r}")
        return task, None

    def _normalize_plan(self, plan: Any) -> Result:
        if isinstance(plan, bool):
            return ok(plan=plan)
        normalized = (str(plan or "")).strip().lower()
        if normalized in ("on", "true", "1", "enable", "enabled"):
            return ok(plan=True)
        if normalized in ("off", "false", "0", "disable", "disabled"):
            return ok(plan=False)
        return err(f"invalid plan {plan!r}; use on/off")

    def _validate_model_id(self, model: str) -> Result:
        """Validate model id format. Does NOT check against provider list (use async version)."""
        normalized = (model or "").strip()
        if not normalized:
            return err("model id is required")
        return ok(model=normalized)

    async def _validate_model_id_async(self, model: str) -> Result:
        """Validate model id and soft-check against provider list."""
        normalized = (model or "").strip()
        if not normalized:
            return err("model id is required")

        listed = await self.list_models(include_hidden=True)
        if listed["ok"]:
            available = known_ids_from_listing(listed)
            if available and normalized not in available:
                logger.warning(
                    "codex session: model %r not in provider list; setting anyway",
                    normalized,
                )
        else:
            logger.warning(
                "codex session: list_models failed (%s); setting %r without validation",
                listed.get("error"), normalized,
            )

        return ok(model=normalized)

    def _validate_sandbox_policy(self, policy: str) -> Result:
        if policy not in self._VALID_SANDBOX_POLICIES:
            return err(
                f"unknown sandbox policy {policy!r}; use read-only / "
                "workspace-write / danger-full-access"
            )
        return ok(sandbox_policy=policy)

    def _validate_approval_policy(self, policy: str) -> Result:
        if policy not in self._VALID_APPROVAL_POLICIES:
            return err(
                f"unknown approval policy {policy!r}; use on-request / "
                "on-failure / never / untrusted"
            )
        return ok(approval_policy=policy)

    def _resolve_task_policy(
        self,
        *,
        model: Optional[str],
        plan: Any,
        sandbox_policy: Optional[str],
        approval_policy: Optional[str],
    ) -> Result:
        resolved_model = self.default_model if model is None else (model or "").strip()
        if not resolved_model:
            return err("model id is required")

        if plan is None:
            resolved_plan = self.mode == "plan"
        else:
            parsed_plan = self._normalize_plan(plan)
            if not parsed_plan["ok"]:
                return parsed_plan
            resolved_plan = parsed_plan["plan"]

        resolved_sandbox = sandbox_policy if sandbox_policy is not None else self.sandbox_policy
        sandbox = self._validate_sandbox_policy(resolved_sandbox)
        if not sandbox["ok"]:
            return sandbox

        resolved_approval = approval_policy if approval_policy is not None else self.approval_policy
        approval = self._validate_approval_policy(resolved_approval)
        if not approval["ok"]:
            return approval

        return ok(
            model=resolved_model,
            plan=resolved_plan,
            sandbox_policy=resolved_sandbox,
            approval_policy=resolved_approval,
        )

    def _task_model(self, task: Task) -> str:
        return getattr(task, "model", None) or self.default_model

    def _task_plan(self, task: Task) -> bool:
        return bool(getattr(task, "plan", self.mode == "plan"))

    def _task_sandbox_policy(self, task: Task) -> str:
        return getattr(task, "sandbox_policy", None) or self.sandbox_policy

    def _task_approval_policy(self, task: Task) -> str:
        return getattr(task, "approval_policy", None) or self.approval_policy

    def get_default_model(self) -> str:
        return self.default_model

    def set_default_model(self, model: str) -> Result:
        validated = self._validate_model_id(model)
        if not validated["ok"]:
            return validated
        self.default_model = validated["model"]
        return ok(model=self.default_model)

    async def get_model(self, task_id: Optional[str] = None) -> Result:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        if task is not None:
            return ok(scope="task", task_id=task_id, model=self._task_model(task))
        return ok(scope="default", model=self.default_model)

    async def set_model(self, model: str, task_id: Optional[str] = None) -> Result:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        validated = await self._validate_model_id_async(model)
        if not validated["ok"]:
            return validated
        if task is not None:
            task.model = validated["model"]
            return ok(scope="task", task_id=task_id, model=task.model)
        self.default_model = validated["model"]
        return ok(scope="default", model=self.default_model)

    async def list_models(
        self,
        *,
        include_hidden: bool = False,
        limit: Optional[int] = None,
    ) -> Result:
        await self.ensure_started_async()
        return await list_models_for_async(
            self._provider, self.bridge.rpc,
            include_hidden=include_hidden, limit=limit,
        )

    def get_mode(self) -> str:
        return self.mode

    def set_mode(self, mode: str) -> Result:
        if mode not in ("plan", "default"):
            return err(f"invalid mode {mode!r}; expected 'plan' or 'default'")
        self.mode = mode
        return ok(mode=mode)

    async def get_plan(self, task_id: Optional[str] = None) -> Result:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        if task is not None:
            plan = self._task_plan(task)
            return ok(scope="task", task_id=task_id, plan=self._plan_label(plan),
                      mode="plan" if plan else "default")
        plan = self.mode == "plan"
        return ok(scope="default", plan=self._plan_label(plan), mode=self.mode)

    async def set_plan(self, plan: Any, task_id: Optional[str] = None) -> Result:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        parsed = self._normalize_plan(plan)
        if not parsed["ok"]:
            return parsed
        if task is not None:
            task.plan = parsed["plan"]
            return ok(scope="task", task_id=task_id, plan=self._plan_label(task.plan),
                      mode="plan" if task.plan else "default")
        self.mode = "plan" if parsed["plan"] else "default"
        return ok(scope="default", plan=self._plan_label(parsed["plan"]), mode=self.mode)

    async def get_sandbox_policy(self, task_id: Optional[str] = None) -> Any:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        if task is not None:
            return ok(scope="task", task_id=task_id,
                      sandbox_policy=self._task_sandbox_policy(task))
        return self.sandbox_policy

    async def set_sandbox_policy(self, policy: str, task_id: Optional[str] = None) -> Result:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        validated = self._validate_sandbox_policy(policy)
        if not validated["ok"]:
            return validated
        if task is not None:
            task.sandbox_policy = policy
            return ok(scope="task", task_id=task_id, sandbox_policy=policy)
        self.sandbox_policy = policy
        return ok(scope="default", sandbox_policy=policy)

    async def get_approval_policy(self, task_id: Optional[str] = None) -> Result:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        if task is not None:
            return ok(scope="task", task_id=task_id,
                      approval_policy=self._task_approval_policy(task))
        return ok(scope="default", approval_policy=self.approval_policy)

    async def set_approval_policy(self, policy: str, task_id: Optional[str] = None) -> Result:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        validated = self._validate_approval_policy(policy)
        if not validated["ok"]:
            return validated
        if task is not None:
            task.approval_policy = policy
            return ok(scope="task", task_id=task_id, approval_policy=policy)
        self.approval_policy = policy
        return ok(scope="default", approval_policy=policy)

    def get_verbose(self) -> str:
        return self.verbose

    def set_verbose(self, level: str) -> Result:
        if level not in ("off", "mid", "on"):
            return err(f"unknown verbose level '{level}'; use off/mid/on")
        self.verbose = level
        return ok(verbose=self.verbose)

    async def get_status(self, task_id: Optional[str] = None) -> Result:
        if task_id:
            return await self.get_task_status(task_id)

        connected = self.bridge.is_connected() if self.bridge else False
        active_tasks = len(self.tasks)

        total_threads = active_tasks
        if connected:
            try:
                listed = await self.list_threads()
                if listed.get("ok"):
                    total_threads = len(listed.get("data", []))
            except Exception:
                pass

        return ok(
            connected=connected,
            active_tasks=active_tasks,
            total_threads=total_threads,
            model=self.default_model,
            mode=self.mode,
            plan=self._plan_label(self.mode == "plan"),
            verbose=self.verbose,
            sandbox_policy=self.sandbox_policy,
            approval_policy=self.approval_policy,
        )

    async def get_task_status(self, task_id: str) -> Result:
        task = self.tasks.get(task_id)
        if task is None:
            return err(f"unknown task id {task_id!r}")

        warning = ""
        thread_status = getattr(task, "thread_status", "") or ""
        connected = self.bridge.is_connected() if self.bridge else False
        if connected:
            try:
                read = await self.bridge.rpc(
                    "thread/read",
                    wire.ThreadReadParams(threadId=task.thread_id),
                    timeout=RPC_TIMEOUT,
                )
                if read.get("ok"):
                    thread = (read.get("result") or {}).get("thread") or {}
                    thread_status = self._status_type(thread.get("status") or {})
                    task.thread_status = thread_status
                else:
                    warning = read.get("error", "thread/read failed")
            except Exception as exc:
                warning = f"thread/read failed: {exc}"
        else:
            warning = "session is not connected; using cached task status"

        pending = None
        if task.request_rpc_id is not None:
            pending = {"type": task.request_type}

        return ok(
            scope="task",
            task_id=task.task_id,
            thread_id=task.thread_id,
            cwd=task.cwd,
            model=self._task_model(task),
            plan=self._plan_label(self._task_plan(task)),
            mode="plan" if self._task_plan(task) else "default",
            sandbox_policy=self._task_sandbox_policy(task),
            approval_policy=self._task_approval_policy(task),
            pending=pending,
            thread_status=thread_status,
            last_turn_status=getattr(task, "last_turn_status", "") or "",
            warning=warning,
        )
