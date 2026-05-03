"""CodexSession — per-hermes-session state and orchestration.

One ``CodexSession`` per hermes session_key. Owns:
  * its own task list (``self.tasks: Dict[task_id, Task]``)
  * per-session config (default_model, mode, verbose)
  * one long-lived ``CodexBridge`` used for app-server RPC transport

Inbound frames flow through ``MessageHandler`` (``handlers.py``); the handler
mutates session state through three narrow setters:

  * ``task_for_thread(thread_id)`` — look up a task by its codex thread id
  * ``stash_request(task, rpc_id, type, payload)`` — record a pending
     server→client request on the task
  * ``notify(text)`` — push a user-visible message via ``self.target``

The bridge owns the app-server lease and WebSocket lifecycle; the session owns
business state and uses the bridge for transport.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict, Optional

from ..transport import wire
from ..events.subscribers.approval import build_approval_response
from ..transport.bridge import CodexBridge
from ..events.bus import EventBus
from ..surfaces.notify import notify_user, report_failure
from .policies import (
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_MODEL,
    DEFAULT_SANDBOX_POLICY,
    RPC_TIMEOUT,
    SHORT_RPC_TIMEOUT,
    STARTUP_TIMEOUT,
    default_collaboration_mode,
    plan_collaboration_mode,
    prepare_sandbox,
)
from .provider import (
    ProviderInfo,
    known_ids_from_listing,
    list_models_for,
    sync_default_model,
)
from .state import Result, Task, TaskTarget, err, ok
from .utils import extract_thread_id, new_task_id

logger = logging.getLogger(__name__)


class CodexSession:
    def __init__(self, session_key: str, target: TaskTarget) -> None:
        self.session_key = session_key
        self.target = target

        self.default_model: str = DEFAULT_MODEL
        self.mode: str = "default"          # "plan" | "default"
        self.verbose: str = "off"  # "off" | "mid" | "on"
        self.sandbox_policy: str = DEFAULT_SANDBOX_POLICY
        self.approval_policy: str = DEFAULT_APPROVAL_POLICY
        self.tasks: Dict[str, Task] = {}    # task_id → Task
        self._provider: ProviderInfo = ProviderInfo()
        self.event_bus = EventBus()
        from ..events.subscribers import register_default_subscribers

        register_default_subscribers(self.event_bus, self)

        self.bridge: CodexBridge = CodexBridge(session=self)
        self._start_lock = threading.Lock()

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
        """Find the Task in this session whose ``thread_id`` matches."""
        if not thread_id:
            return None
        for t in self.tasks.values():
            if t.thread_id == thread_id:
                return t
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
        """Record a pending server→client request on the task.

        ``task`` may be None (e.g. the request arrived after the task was
        archived); in that case the call is a no-op so the handler can still
        push its notification.
        """
        if task is None:
            return
        task.request_rpc_id = rpc_id
        task.request_type = request_type
        task.request_payload = payload
        task.request_schema = request_schema

    async def notify(self, text: str) -> None:
        """Push a message to this session's chat target."""
        await notify_user(self.target, text)

    # ── Task operations ──────────────────────────────────────────────────────

    def start_task(
        self,
        *,
        cwd: str,
        prompt: str,
        model: Optional[str] = None,
        plan: Any = None,
        approval_policy: Optional[str] = None,
        sandbox_policy: Optional[str] = None,
        base_instructions: Optional[str] = None,
    ) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return started

        resolved = self._resolve_task_policy(
            model=model,
            plan=plan,
            sandbox_policy=sandbox_policy,
            approval_policy=approval_policy,
        )
        if not resolved["ok"]:
            return resolved
        task_id = new_task_id()

        async def _boot() -> None:
            asyncio.create_task(self._drive_task(
                task_id=task_id, cwd=cwd, prompt=prompt,
                model=resolved["model"],
                plan=resolved["plan"],
                approval_policy=resolved["approval_policy"],
                sandbox_policy=resolved["sandbox_policy"],
                base_instructions=base_instructions,
            ))

        boot = self.bridge.run_sync(_boot(), timeout=SHORT_RPC_TIMEOUT)
        if not boot["ok"]:
            return boot
        return ok(
            task_id=task_id,
            model=resolved["model"],
            plan=self._plan_label(resolved["plan"]),
            sandbox_policy=resolved["sandbox_policy"],
            approval_policy=resolved["approval_policy"],
        )

    def send_reply(self, task_id: str, message: str) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return started

        task = self.tasks.get(task_id)
        if task is None:
            return err(f"unknown task id {task_id!r}")

        async def _boot() -> None:
            asyncio.create_task(self._drive_reply(task_id, message))

        boot = self.bridge.run_sync(_boot(), timeout=SHORT_RPC_TIMEOUT)
        if not boot["ok"]:
            return boot
        return ok(task_id=task_id)

    def revive_task(
        self,
        thread_id: str,
        *,
        model: Optional[str] = None,
        plan: Any = None,
        sandbox_policy: Optional[str] = None,
        approval_policy: Optional[str] = None,
    ) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return started

        resolved = self._resolve_task_policy(
            model=model,
            plan=plan,
            sandbox_policy=sandbox_policy,
            approval_policy=approval_policy,
        )
        if not resolved["ok"]:
            return resolved

        # Already tracked in this session?
        for task in self.tasks.values():
            if task.thread_id == thread_id:
                return ok(task_id=task.task_id, thread_id=thread_id,
                          message="thread already tracked in this session")

        # Refuse to steal a thread owned by another active session.
        from . import session_registry

        owner = session_registry.find_thread_owner(thread_id, exclude_key=self.session_key)
        if owner is not None:
            return err(
                f"thread {thread_id!r} is currently held by session {owner!r} — "
                "cannot revive into a different session"
            )

        read = self.bridge.run_sync(
            self.bridge.rpc("thread/read", wire.ThreadReadParams(threadId=thread_id), timeout=RPC_TIMEOUT),
        )
        if not read["ok"]:
            return err(f"thread {thread_id!r} not found: {read['error']}")

        thread_obj = (read["result"] or {}).get("thread") or {}
        if not thread_obj.get("id"):
            return err(f"thread {thread_id!r} not found on server")
        cwd = thread_obj.get("cwd") or ""

        status = thread_obj.get("status") or {}
        if status.get("type") == "notLoaded":
            resumed = self.bridge.run_sync(
                self.bridge.rpc(
                    "thread/resume",
                    wire.ThreadResumeParams(
                        threadId=thread_id,
                        model=resolved["model"],
                        approvalPolicy=resolved["approval_policy"],
                    ),
                    timeout=RPC_TIMEOUT,
                ),
            )
            if not resumed["ok"]:
                return err(f"thread/resume failed: {resumed['error']}")

        task_id = new_task_id()
        self.tasks[task_id] = Task(
            task_id=task_id, thread_id=thread_id, cwd=cwd,
            model=resolved["model"],
            plan=resolved["plan"],
            sandbox_policy=resolved["sandbox_policy"],
            approval_policy=resolved["approval_policy"],
            thread_status=self._status_type(status),
        )
        return ok(
            task_id=task_id,
            thread_id=thread_id,
            model=resolved["model"],
            plan=self._plan_label(resolved["plan"]),
            sandbox_policy=resolved["sandbox_policy"],
            approval_policy=resolved["approval_policy"],
        )

    def remove_task(self, task_id: str) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return started

        task = self.tasks.get(task_id)
        if task is None:
            return err(f"unknown task id {task_id!r}")

        from . import session_registry

        owner = session_registry.find_thread_owner(task.thread_id, exclude_key=self.session_key)
        if owner is not None:
            return err(
                f"thread {task.thread_id!r} is also held by session {owner!r} — "
                "cannot archive a shared thread"
            )

        archived = self.bridge.run_sync(
            self.bridge.rpc("thread/archive", wire.ThreadArchiveParams(threadId=task.thread_id), timeout=RPC_TIMEOUT),
        )
        if not archived["ok"]:
            return archived

        self.tasks.pop(task_id, None)
        return ok(task_id=task_id)

    def remove_all_tasks(self) -> Result:
        task_ids = list(self.tasks.keys())
        errors = []
        for task_id in task_ids:
            result = self.remove_task(task_id)
            if not result["ok"]:
                errors.append(f"{task_id}: {result['error']}")
        return {
            "ok": not errors,
            "removed": len(task_ids) - len(errors),
            "errors": errors,
        }

    def list_tasks(self) -> Result:
        """List tasks in *this* session."""
        return ok(data=[
            {
                "task_id": t.task_id,
                "thread_id": t.thread_id,
                "cwd": t.cwd,
                "model": self._task_model(t),
                "plan": self._plan_label(self._task_plan(t)),
                "sandbox_policy": self._task_sandbox_policy(t),
                "approval_policy": self._task_approval_policy(t),
            }
            for t in self.tasks.values()
        ])

    def list_threads(self, *, limit: Optional[int] = None) -> Result:
        """List all threads on the server (paginated)."""
        started = self.ensure_started()
        if not started["ok"]:
            return started

        all_threads: list = []
        cursor: Optional[str] = None
        while True:
            rpc = self.bridge.run_sync(
                self.bridge.rpc("thread/list", wire.ThreadListParams(cursor=cursor, limit=limit), timeout=RPC_TIMEOUT),
            )
            if not rpc["ok"]:
                return rpc
            server_data = rpc["result"] or {}
            page = server_data.get("data") or []
            all_threads.extend(page)
            cursor = server_data.get("nextCursor")
            if not cursor or not page:
                break
        return ok(data=all_threads)

    def archive_all_threads(self) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return {"ok": False, "removed": 0, "errors": [started["error"]]}

        listed = self.list_threads()
        if not listed["ok"]:
            return {"ok": False, "removed": 0, "errors": [listed["error"]]}

        errors, removed = [], 0
        for t in listed["data"] or []:
            thread_id = t.get("id") or ""
            if not thread_id:
                continue
            from . import session_registry

            owner = session_registry.find_thread_owner(thread_id, exclude_key=self.session_key)
            if owner is not None:
                errors.append(f"{thread_id}: held by session {owner!r}, skipped")
                continue
            archived = self.bridge.run_sync(
                self.bridge.rpc("thread/archive", wire.ThreadArchiveParams(threadId=thread_id), timeout=RPC_TIMEOUT),
            )
            if archived["ok"]:
                removed += 1
            else:
                errors.append(f"{thread_id}: {archived['error']}")

        self.tasks.clear()
        return {"ok": not errors, "removed": removed, "errors": errors}

    # ── Approval / Input resolution ──────────────────────────────────────────

    def approve_task(self, task_id: str, decision: str, *, for_session: bool = False) -> Result:
        """Resolve a pending command approval or simple elicitation response.

        Pass ``for_session=True`` with decision="accept" to send the schema-specific
        session-wide approval decision where the approval type supports it.
        For elicitation requests, approve/deny is a shorthand for accepting or
        declining with empty form content. Use ``respond_task`` to send fields.
        """
        task = self.tasks.get(task_id)
        if task is None or task.request_rpc_id is None:
            return err(f"no pending request for task `{task_id}`")
        if task.request_type == "elicitation":
            action = "accept" if decision == "accept" else "decline"
            payload = {"action": action, "content": {}}
        else:
            if task.request_type not in ("command",):
                return err(f"task `{task_id}` has a {task.request_type!r} request, not approvable")

            built = build_approval_response(task.request_payload, decision, for_session=for_session)
            if not built["ok"]:
                return built
            payload = built["payload"]

        rpc_id = task.request_rpc_id
        send = self.bridge.run_sync(
            self.bridge.ws_send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id, "result": payload,
            })),
            timeout=SHORT_RPC_TIMEOUT,
        )
        if not send["ok"]:
            return send

        if task.request_payload is not None:
            task.request_payload["response_sent"] = True
            task.request_payload["decision"] = decision
        return ok(decision=decision)

    def respond_task(self, task_id: str, content: "dict | None" = None) -> Result:
        """Resolve a pending elicitation request by sending schema data.

        ``content`` is the form data matching ``task.request_schema``.
        Pass ``None`` or an empty dict to accept without data (equivalent to
        the old approve-with-null-content path).
        """
        task = self.tasks.get(task_id)
        if task is None or task.request_rpc_id is None:
            return err(f"no pending request for task `{task_id}`")
        if task.request_type != "elicitation":
            return err(f"task `{task_id}` has a {task.request_type!r} request, not an elicitation")

        payload = {"action": "accept", "content": content}

        rpc_id = task.request_rpc_id
        send = self.bridge.run_sync(
            self.bridge.ws_send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id, "result": payload,
            })),
            timeout=SHORT_RPC_TIMEOUT,
        )
        if not send["ok"]:
            return send

        if task.request_payload is not None:
            task.request_payload["response_sent"] = True
            task.request_payload["decision"] = "respond"
        return ok(task_id=task_id, decision="respond")

    def decline_task(self, task_id: str) -> Result:
        """Decline a pending elicitation request."""
        task = self.tasks.get(task_id)
        if task is None or task.request_rpc_id is None:
            return err(f"no pending request for task `{task_id}`")
        if task.request_type != "elicitation":
            return err(f"task `{task_id}` has a {task.request_type!r} request, not an elicitation")

        payload = {"action": "decline", "content": {}}

        rpc_id = task.request_rpc_id
        send = self.bridge.run_sync(
            self.bridge.ws_send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id, "result": payload,
            })),
            timeout=SHORT_RPC_TIMEOUT,
        )
        if not send["ok"]:
            return send

        if task.request_payload is not None:
            task.request_payload["response_sent"] = True
            task.request_payload["decision"] = "decline"
        return ok(task_id=task_id, decision="decline")

    def input_task(
        self,
        task_id: str,
        answer: str = "",
        *,
        responses: "list[str] | None" = None,
        answers: "list[list[str]] | None" = None,
    ) -> Result:
        """Resolve a pending input request by sending the user's answer(s).

        Pass ``responses`` for one answer per question, or ``answers`` for
        multiple answers per question. Fall back to ``answer`` replicated
        across all questions.
        """
        task = self.tasks.get(task_id)
        if task is None or task.request_rpc_id is None:
            return err(f"no pending input for task `{task_id}`")
        if task.request_type != "input":
            return err(f"task `{task_id}` has a {task.request_type!r} request, not input")

        questions = (task.request_payload or {}).get("questions") or []
        if not questions:
            return err(f"pending input for task `{task_id}` has no questions")
        n = len(questions)
        if answers is not None:
            if not answers or not all(group for group in answers):
                return err("answers must be a non-empty list of non-empty answer groups")
            # Pad or truncate answer groups to match question count.
            pad = list(answers[-1]) if answers else []
            answer_groups = [list(group) for group in answers]
            answer_groups = (answer_groups + [pad] * n)[:n]
        elif responses is not None:
            # Pad or truncate to match question count.
            pad = responses[-1] if responses else ""
            responses = (list(responses) + [pad] * n)[:n]
            answer_groups = [[response] for response in responses]
        else:
            answer_groups = [[answer] for _ in range(n)]

        response_payload = {}
        for idx, question in enumerate(questions):
            question_id = getattr(question, "id", None)
            if not question_id:
                return err(f"pending input question {idx + 1} has no id")
            options = getattr(question, "options", None) or []
            allowed = {
                str(getattr(option, "label", "") or "")
                for option in options
                if getattr(option, "label", None)
            }
            if allowed and not getattr(question, "isOther", False):
                invalid = [answer for answer in answer_groups[idx] if answer not in allowed]
                if invalid:
                    allowed_list = ", ".join(sorted(allowed))
                    return err(
                        f"invalid answer for question {idx + 1}: "
                        f"{', '.join(invalid)}; use one of: {allowed_list}"
                    )
            response_payload[str(question_id)] = {"answers": answer_groups[idx]}

        rpc_id = task.request_rpc_id
        send = self.bridge.run_sync(
            self.bridge.ws_send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id,
                "result": {"answers": response_payload},
            })),
            timeout=SHORT_RPC_TIMEOUT,
        )
        if not send["ok"]:
            return send

        if task.request_payload is not None:
            task.request_payload["response_sent"] = True
            task.request_payload["decision"] = "answer"
        return ok(task_id=task_id)

    def list_pending_requests(self) -> list:
        return [
            {
                "task_id": t.task_id,
                "type": t.request_type,
                "preview": (t.request_payload or {}).get("preview", ""),
            }
            for t in self.tasks.values()
            if t.request_rpc_id is not None
        ]

    # ── Settings ─────────────────────────────────────────────────────────────

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
        normalized = (model or "").strip()
        if not normalized:
            return err("model id is required")

        listed = self.list_models(include_hidden=True)
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

    def get_model(self, task_id: Optional[str] = None) -> Result:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        if task is not None:
            return ok(scope="task", task_id=task_id, model=self._task_model(task))
        return ok(scope="default", model=self.default_model)

    def set_model(self, model: str, task_id: Optional[str] = None) -> Result:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        validated = self._validate_model_id(model)
        if not validated["ok"]:
            return validated
        if task is not None:
            task.model = validated["model"]
            return ok(scope="task", task_id=task_id, model=task.model)
        self.default_model = validated["model"]
        return ok(scope="default", model=self.default_model)

    def list_models(
        self,
        *,
        include_hidden: bool = False,
        limit: Optional[int] = None,
    ) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return started
        return list_models_for(
            self._provider, self.bridge.run_sync, self.bridge.rpc,
            include_hidden=include_hidden, limit=limit,
        )

    def get_mode(self) -> str:
        return self.mode

    def set_mode(self, mode: str) -> Result:
        if mode not in ("plan", "default"):
            return err(f"invalid mode {mode!r}; expected 'plan' or 'default'")
        self.mode = mode
        return ok(mode=mode)

    def get_plan(self, task_id: Optional[str] = None) -> Result:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        if task is not None:
            plan = self._task_plan(task)
            return ok(scope="task", task_id=task_id, plan=self._plan_label(plan),
                      mode="plan" if plan else "default")
        plan = self.mode == "plan"
        return ok(scope="default", plan=self._plan_label(plan), mode=self.mode)

    def set_plan(self, plan: Any, task_id: Optional[str] = None) -> Result:
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

    def get_sandbox_policy(self, task_id: Optional[str] = None) -> Any:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        if task is not None:
            return ok(scope="task", task_id=task_id,
                      sandbox_policy=self._task_sandbox_policy(task))
        return self.sandbox_policy

    def set_sandbox_policy(self, policy: str, task_id: Optional[str] = None) -> Result:
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

    def get_approval_policy(self, task_id: Optional[str] = None) -> Result:
        task, error = self._task_or_error(task_id)
        if error is not None:
            return error
        if task is not None:
            return ok(scope="task", task_id=task_id,
                      approval_policy=self._task_approval_policy(task))
        return ok(scope="default", approval_policy=self.approval_policy)

    def set_approval_policy(self, policy: str, task_id: Optional[str] = None) -> Result:
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

    def get_status(self, task_id: Optional[str] = None) -> Result:
        if task_id:
            return self.get_task_status(task_id)

        connected = self.bridge.is_connected() if self.bridge else False
        active_tasks = len(self.tasks)

        total_threads = active_tasks
        if connected:
            try:
                listed = self.list_threads()
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

    def get_task_status(self, task_id: str) -> Result:
        task = self.tasks.get(task_id)
        if task is None:
            return err(f"unknown task id {task_id!r}")

        warning = ""
        thread_status = getattr(task, "thread_status", "") or ""
        connected = self.bridge.is_connected() if self.bridge else False
        if connected:
            try:
                read = self.bridge.run_sync(
                    self.bridge.rpc(
                        "thread/read",
                        wire.ThreadReadParams(threadId=task.thread_id),
                        timeout=RPC_TIMEOUT,
                    ),
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

    # ── Drive functions (fire-and-forget) ────────────────────────────────────

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
