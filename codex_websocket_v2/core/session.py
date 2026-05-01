"""CodexSession — per-hermes-session state and orchestration.

One ``CodexSession`` per hermes session_key. Owns:
  * its own WebSocket via ``self.bridge`` (independent connection + loop)
  * its own task list (``self.tasks: Dict[task_id, Task]``)
  * per-session config (default_model, mode, verbose)

Inbound frames flow through ``MessageHandler`` (``handlers.py``); the handler
mutates session state through three narrow setters:

  * ``task_for_thread(thread_id)`` — look up a task by its codex thread id
  * ``stash_request(task, rpc_id, type, payload)`` — record a pending
     server→client request on the task
  * ``notify(text)`` — push a user-visible message via ``self.target``

The CodexServerManager (process-level) is shared across sessions; the bridge
is exclusive to this session.
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
from .server_manager import CodexServerManager
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
        self.tasks: Dict[str, Task] = {}    # task_id → Task
        self._provider: ProviderInfo = ProviderInfo()
        self.event_bus = EventBus()
        from ..events.subscribers import register_default_subscribers

        register_default_subscribers(self.event_bus, self)

        self._server = CodexServerManager.instance()
        self.bridge: Optional[CodexBridge] = None
        self._ready = threading.Event()
        self._start_lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def ensure_started(self) -> Result:
        if self._ready.is_set():
            return ok()
        with self._start_lock:
            if self._ready.is_set():
                return ok()

            acquired = self._server.acquire()
            if not acquired["ok"]:
                return acquired
            port = acquired["port"]

            self.bridge = CodexBridge(port=port, session=self)
            connect = self.bridge.connect()
            if not connect["ok"]:
                self._server.release()
                self.bridge = None
                return connect

            sync = self.bridge.run_sync(self._sync_config_from_server(), timeout=STARTUP_TIMEOUT)
            if sync["ok"]:
                self.default_model = sync["model"]
            else:
                logger.warning("codex session: failed to sync default model: %s", sync["error"])

            self._ready.set()
            return ok()

    def shutdown(self) -> None:
        if self.bridge is not None:
            try:
                self.bridge.disconnect()
            except Exception:
                pass
            self.bridge = None
        self._server.release()
        self._ready.clear()

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

    async def notify(self, text: str) -> None:
        """Push a message to this session's chat target."""
        await notify_user(self.target, text)

    # ── Task operations ──────────────────────────────────────────────────────

    def start_task(
        self,
        *,
        cwd: str,
        prompt: str,
        approval_policy: str = DEFAULT_APPROVAL_POLICY,
        sandbox_policy: Optional[str] = None,
        base_instructions: Optional[str] = None,
    ) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return started

        resolved_sandbox = sandbox_policy if sandbox_policy is not None else self.sandbox_policy
        task_id = new_task_id()

        async def _boot() -> None:
            asyncio.create_task(self._drive_task(
                task_id=task_id, cwd=cwd, prompt=prompt,
                approval_policy=approval_policy, sandbox_policy=resolved_sandbox,
                base_instructions=base_instructions,
            ))

        boot = self.bridge.run_sync(_boot(), timeout=SHORT_RPC_TIMEOUT)
        if not boot["ok"]:
            return boot
        return ok(task_id=task_id, model=self.default_model)

    def send_reply(self, task_id: str, message: str) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return started

        task = self.tasks.get(task_id)
        if task is None:
            return err(f"unknown task id {task_id!r}")

        # If the task has a pending input request, route the reply as input answers.
        if task.request_type == "input":
            return self.input_task(task_id, message)

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
        sandbox_policy: Optional[str] = None,
        approval_policy: str = DEFAULT_APPROVAL_POLICY,
    ) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return started

        resolved_sandbox = sandbox_policy if sandbox_policy is not None else self.sandbox_policy

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
                self.bridge.rpc("thread/resume", wire.ThreadResumeParams(threadId=thread_id), timeout=RPC_TIMEOUT),
            )
            if not resumed["ok"]:
                return err(f"thread/resume failed: {resumed['error']}")

        task_id = new_task_id()
        self.tasks[task_id] = Task(
            task_id=task_id, thread_id=thread_id, cwd=cwd,
            sandbox_policy=resolved_sandbox, approval_policy=approval_policy,
        )
        return ok(task_id=task_id, thread_id=thread_id, model=self.default_model)

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
            {"task_id": t.task_id, "thread_id": t.thread_id, "cwd": t.cwd}
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
        """Resolve a pending command/elicitation request by sending a WS response.

        Pass ``for_session=True`` with decision="accept" to send the schema-specific
        session-wide approval decision where the approval type supports it.
        """
        task = self.tasks.get(task_id)
        if task is None or task.request_rpc_id is None:
            return err(f"no pending request for task `{task_id}`")
        if task.request_type not in ("command", "elicitation"):
            return err(f"task `{task_id}` has a {task.request_type!r} request, not approvable")

        if task.request_type == "elicitation":
            action = "accept" if decision == "accept" else "decline"
            payload = {"action": action, "content": None}
        else:
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

        task.request_rpc_id = None
        task.request_type = None
        task.request_payload = None
        return ok(decision=decision)

    def input_task(self, task_id: str, answer: str = "", *, responses: "list[str] | None" = None) -> Result:
        """Resolve a pending input request by sending the user's answer(s).

        Pass ``responses`` (one string per question) when the LLM knows all
        answers; fall back to ``answer`` which is replicated across all questions.
        """
        task = self.tasks.get(task_id)
        if task is None or task.request_rpc_id is None:
            return err(f"no pending input for task `{task_id}`")
        if task.request_type != "input":
            return err(f"task `{task_id}` has a {task.request_type!r} request, not input")

        questions = (task.request_payload or {}).get("questions") or []
        n = len(questions) or 1
        if responses is not None:
            # Pad or truncate to match question count.
            pad = responses[-1] if responses else ""
            responses = (list(responses) + [pad] * n)[:n]
        else:
            responses = [answer] * n

        rpc_id = task.request_rpc_id
        send = self.bridge.run_sync(
            self.bridge.ws_send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id,
                "result": {"responses": responses},
            })),
            timeout=SHORT_RPC_TIMEOUT,
        )
        if not send["ok"]:
            return send

        task.request_rpc_id = None
        task.request_type = None
        task.request_payload = None
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

    def get_default_model(self) -> str:
        return self.default_model

    def set_default_model(self, model: str) -> Result:
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

        self.default_model = normalized
        return ok(model=normalized)

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

    def get_sandbox_policy(self) -> str:
        return self.sandbox_policy

    def set_sandbox_policy(self, policy: str) -> Result:
        valid = ("read-only", "workspace-write", "danger-full-access")
        if policy not in valid:
            return err(f"unknown sandbox policy {policy!r}; use read-only / workspace-write / danger-full-access")
        self.sandbox_policy = policy
        return ok(sandbox_policy=policy)

    def get_verbose(self) -> str:
        return self.verbose

    def set_verbose(self, level: str) -> Result:
        if level not in ("off", "mid", "on"):
            return err(f"unknown verbose level '{level}'; use off/mid/on")
        self.verbose = level
        return ok(verbose=self.verbose)

    def get_status(self) -> Result:
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
            verbose=self.verbose,
            sandbox_policy=self.sandbox_policy,
        )

    # ── Drive functions (fire-and-forget) ────────────────────────────────────

    def _build_turn_start(
        self,
        *,
        thread_id: str,
        text: str,
        cwd: str,
        sandbox_policy: str,
        approval_policy: str,
    ) -> "wire.TurnStartParams":
        model = self.default_model
        return wire.TurnStartParams(
            threadId=thread_id,
            input=[{"type": "text", "text": text}],
            model=model,
            approvalPolicy=approval_policy,
            sandboxPolicy=prepare_sandbox(sandbox_policy, cwd),
            collaborationMode=(
                plan_collaboration_mode(model)
                if self.mode == "plan"
                else default_collaboration_mode(model)
            ),
        )

    async def _drive_task(
        self,
        *,
        task_id: str,
        cwd: str,
        prompt: str,
        approval_policy: str,
        sandbox_policy: str,
        base_instructions: Optional[str],
    ) -> None:
        model = self.default_model

        thread_rpc = await self.bridge.rpc(
            "thread/start",
            wire.ThreadStartParams(cwd=cwd, model=model, baseInstructions=base_instructions),
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
            sandbox_policy=sandbox_policy, approval_policy=approval_policy,
        )

        await self.notify(
            f"🤖 Codex task `{task_id}` started\n"
            f"cwd: `{cwd}`\nmodel: `{model}`"
            + ("\nmode: `plan`" if self.mode == "plan" else "")
        )

        turn_rpc = await self.bridge.rpc(
            "turn/start",
            self._build_turn_start(
                thread_id=thread_id, text=prompt, cwd=cwd,
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
                sandbox_policy=task.sandbox_policy, approval_policy=task.approval_policy,
            ),
        )
        if not rpc["ok"]:
            await report_failure(self.target, task_id, "reply failed", rpc["error"])
