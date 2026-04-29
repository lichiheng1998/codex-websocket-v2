"""Frame dispatch and inbound-message handling for codex-websocket-v2.

Lives between ``CodexBridge._reader_loop`` and ``CodexSession``. Responsibilities:

  1. Identify the frame kind (response / error / request / notification).
  2. For RPC responses: resolve the matching Future on ``bridge._pending_rpc``.
  3. For server→client requests: parse params, find the matching ``Task``,
     stash the request id/type/payload on the task, push a formatted user
     notification.
  4. For server→client notifications: route ``item/completed`` and
     ``turn/completed`` to the appropriate formatter and push notifications.

The handler reads/mutates session state through narrow setters:
``session.task_for_thread``, ``session.stash_request``, ``session.notify``.
It does not start tasks, send replies, or touch the bridge except via
``session.bridge.ws_send`` for the "unknown method, decline" path.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Optional

from . import wire
from .notify import notify_user
from .state import Task

if TYPE_CHECKING:
    from .session import CodexSession

logger = logging.getLogger(__name__)

MAX_NOTIFY_TEXT = 4000
MAX_COMMAND_OUTPUT = 1000
MAX_ELICITATION_SCHEMA_PREVIEW = 300
MAX_APPROVAL_CMD_PREVIEW = 200


class MessageHandler:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    # ── Top-level dispatch ───────────────────────────────────────────────────

    async def dispatch(self, raw: dict) -> None:
        kind, parsed, _ = wire.parse_incoming(raw)
        if kind == "response":
            self._resolve_rpc(parsed.id.root, result=parsed.result)
        elif kind == "error":
            self._resolve_rpc(parsed.id.root, error=parsed.error)
        elif kind == "request":
            await self._on_server_request(parsed)
        elif kind == "notification":
            await self._on_server_notification(parsed)
        else:
            logger.debug("codex bridge: unparseable frame dropped")

    def _resolve_rpc(self, rpc_id: Any, *, result: Any = None, error: Any = None) -> None:
        # The server may echo the id back as a string; pending dict keys by int.
        pending = self.session.bridge._pending_rpc
        fut = pending.get(rpc_id)
        if fut is None:
            if isinstance(rpc_id, str):
                try:
                    fut = pending.get(int(rpc_id))
                except ValueError:
                    pass
            elif isinstance(rpc_id, int):
                fut = pending.get(str(rpc_id))
        if fut is None or fut.done():
            return
        if error is not None:
            fut.set_exception(RuntimeError(f"{error.code}: {error.message}"))
        else:
            fut.set_result(result)

    # ── Server → client requests ─────────────────────────────────────────────

    async def _on_server_request(self, req: Any) -> None:
        method = req.method.value
        rpc_id = req.id.root
        params = req.params

        if method == "item/commandExecution/requestApproval" \
                or method in ("execCommandApproval", "applyPatchApproval"):
            await self._handle_command_approval(params, rpc_id)
        elif method == "item/fileChange/requestApproval":
            await self._handle_file_change_approval(params, rpc_id)
        elif method == "item/permissions/requestApproval":
            await self._handle_permissions_approval(params, rpc_id)
        elif method == "item/tool/requestUserInput":
            await self._handle_user_input_request(params, rpc_id)
        elif method == "mcpServer/elicitation/request":
            await self._handle_elicitation_request(params, rpc_id)
        else:
            logger.debug("codex handler: unhandled server request %s", method)
            await self.session.bridge.ws_send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32601, "message": f"unhandled: {method}"},
            }))

    def _approval_meta(self, params: Any) -> tuple[Optional[Task], str]:
        thread_id = getattr(params, "threadId", None) or getattr(params, "conversationId", None)
        task = self.session.task_for_thread(thread_id) if thread_id else None
        task_id = task.task_id if task else "?"
        return task, task_id

    @staticmethod
    def _approval_footer(task_id: str, *, accept_label: str = "Approve", decline_label: str = "Deny") -> str:
        return (
            f"{accept_label}: `/codex approve {task_id}`\n"
            f"{decline_label}: `/codex deny {task_id}`"
        )

    async def _handle_command_approval(self, params: Any, rpc_id: Any) -> None:
        task, task_id = self._approval_meta(params)
        reason = (getattr(params, "reason", "") or "").strip() or "Codex approval"
        command = getattr(params, "command", None) or getattr(params, "commandText", None) or ""
        if isinstance(command, list):
            command = " ".join(str(x) for x in command)
        cmd_str = str(command) or "(codex command)"
        cmd_preview = cmd_str[:MAX_APPROVAL_CMD_PREVIEW]

        notification = "\n".join([
            f"⚠️ Codex task `{task_id}` requests to run a command:",
            f"```\n{cmd_preview}\n```",
            f"Reason: {reason}",
            "",
            self._approval_footer(task_id),
        ])
        self.session.stash_request(task, rpc_id, "command",
                                   {"preview": cmd_preview, "reason": reason})
        await self.session.notify(notification)

    async def _handle_file_change_approval(self, params: Any, rpc_id: Any) -> None:
        task, task_id = self._approval_meta(params)
        reason = (getattr(params, "reason", "") or "").strip() or "Codex file change"
        change = getattr(params, "fileChange", None)
        preview = str(change)[:MAX_APPROVAL_CMD_PREVIEW] if change else "(file change)"

        notification = "\n".join([
            f"⚠️ Codex task `{task_id}` requests file changes:",
            f"```\n{preview}\n```",
            f"Reason: {reason}",
            "",
            self._approval_footer(task_id),
        ])
        self.session.stash_request(task, rpc_id, "command",
                                   {"preview": preview, "reason": reason})
        await self.session.notify(notification)

    async def _handle_permissions_approval(self, params: Any, rpc_id: Any) -> None:
        task, task_id = self._approval_meta(params)
        reason = (getattr(params, "reason", "") or "").strip() or "Codex permissions"

        perms = getattr(params, "permissions", None)
        fs = getattr(perms, "fileSystem", None) if perms else None
        writes = getattr(fs, "write", None) if fs else None
        net = getattr(perms, "network", None) if perms else None
        parts = []
        if writes:
            parts.append("Write paths: " + ", ".join(f"`{getattr(p, 'root', p)}`" for p in writes))
        if net and getattr(net, "enabled", False):
            parts.append("Network access")
        preview = "\n".join(parts) or "(no details)"

        notification = "\n".join([
            f"⚠️ Codex task `{task_id}` requests permissions:",
            preview,
            f"Reason: {reason}",
            "",
            self._approval_footer(task_id),
        ])
        self.session.stash_request(task, rpc_id, "command",
                                   {"preview": preview, "reason": reason})
        await self.session.notify(notification)

    async def _handle_elicitation_request(self, params: Any, rpc_id: Any) -> None:
        inner = params.root if hasattr(params, "root") else params
        task, task_id = self._approval_meta(inner)
        server_name = getattr(inner, "serverName", None) or "MCP server"
        elicitation = getattr(inner, "elicitation", None)
        mode = getattr(getattr(elicitation, "mode", None), "value", "form") if elicitation else "form"
        elicit_msg = getattr(elicitation, "message", "") if elicitation else ""

        if mode == "url":
            url = getattr(elicitation, "url", "") or ""
            heading = f"🔗 `{task_id}` MCP `{server_name}` needs you to visit a link:"
            body = f"{url}\n{elicit_msg}"
            footer = self._approval_footer(task_id, accept_label="When done", decline_label="Cancel")
        else:
            schema = getattr(elicitation, "requestedSchema", None) if elicitation else None
            schema_json = json.dumps(
                schema.model_dump(mode="json") if hasattr(schema, "model_dump") else (schema or {}),
                ensure_ascii=False,
            )[:MAX_ELICITATION_SCHEMA_PREVIEW]
            heading = f"❓ `{task_id}` MCP `{server_name}` requests input:"
            body = f"{elicit_msg}\nSchema: `{schema_json}`"
            footer = self._approval_footer(task_id, accept_label="Accept", decline_label="Decline")

        notification = "\n".join([heading, body, "", footer])
        self.session.stash_request(task, rpc_id, "elicitation",
                                   {"preview": elicit_msg, "server": server_name})
        await self.session.notify(notification)

    async def _handle_user_input_request(self, params: Any, rpc_id: Any) -> None:
        thread_id = getattr(params, "threadId", "")
        questions = getattr(params, "questions", None) or []
        task = self.session.task_for_thread(thread_id) if thread_id else None

        if task is None:
            logger.warning(
                "codex handler: requestUserInput for unknown thread %s — denying", thread_id)
            await self.session.bridge.ws_send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32000, "message": "no active task for thread"},
            }))
            return

        lines = [f"🤔 Codex task `{task.task_id}` needs you to answer:"]
        for idx, q in enumerate(questions, 1):
            question_text = getattr(q, "question", "") or ""
            lines.append(f"\n{idx}. {question_text}")
            if getattr(q, "isOther", False):
                lines.append("   _(free-text reply accepted)_")
        lines.append(f"\nReply with: `/codex reply {task.task_id} <your answer>`")

        self.session.stash_request(task, rpc_id, "input", {
            "questions": questions,
            "preview": questions[0].question if questions else "",
        })
        await self.session.notify("\n".join(lines))

    # ── Server → client notifications ────────────────────────────────────────

    async def _on_server_notification(self, notif: Any) -> None:
        method = notif.method.value
        params = notif.params

        if method == "item/agentMessage/delta":
            return  # streaming token — keep thread "warm", no UI

        if method == "item/completed":
            task = self.session.task_for_thread(params.threadId)
            if task is not None:
                await self._safe(self._on_item_completed(task, params))
            return

        if method == "turn/completed":
            task = self.session.task_for_thread(params.threadId)
            if task is not None:
                await self._safe(self._on_turn_completed(task, params))
            return

        logger.debug("codex handler: notification %s ignored", method)

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except Exception as exc:
            logger.warning("codex handler task failed: %s", exc)

    # ── item/completed formatters ────────────────────────────────────────────

    async def _on_item_completed(self, task: Task, params: Any) -> None:
        item = params.item.root
        item_type = getattr(getattr(item, "type", None), "value", None) or getattr(item, "type", "")
        level = self.session.verbose

        task.last_item = item
        task.last_item_type = item_type

        if level == "off":
            # Buffer only — flushed on turn/completed.
            return

        if level == "mid":
            # Only agentMessage is shown live; everything else stays silent.
            if item_type == "agentMessage":
                await self._on_agent_message(task, item)
            return

        # level == "on" — show everything immediately
        if item_type == "agentMessage":
            await self._on_agent_message(task, item)
        elif item_type == "plan":
            await self._on_plan(task, item)
        elif item_type == "commandExecution":
            await self._on_command_execution(task, item)
        elif item_type == "fileChange":
            await self._on_file_change(task, item)
        elif item_type == "webSearch":
            await self._on_web_search(task, item)
        elif item_type == "enteredReviewMode":
            await self._on_entered_review_mode(task, item)
        elif item_type == "exitedReviewMode":
            await self._on_exited_review_mode(task, item)
        elif item_type == "contextCompaction":
            await self.session.notify(f"🗜️ `{task.task_id}` context compacted")
        else:
            logger.debug("codex handler: item type %r ignored", item_type)

    async def _on_agent_message(self, task: Task, item: Any) -> None:
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            return
        prefix = f"🤖 `{task.task_id}`\n\n"
        max_text = MAX_NOTIFY_TEXT - len(prefix)
        if len(text) > max_text:
            text = text[:max_text] + "\n…(truncated)"
        await self.session.notify(prefix + text)

    async def _on_plan(self, task: Task, item: Any) -> None:
        text = (getattr(item, "text", "") or "").strip()
        if text:
            await self.session.notify(f"📋 `{task.task_id}` plan\n\n{text}")

    async def _on_command_execution(self, task: Task, item: Any) -> None:
        cmd = (getattr(item, "command", "") or "").strip()
        exit_code = getattr(item, "exitCode", None)
        output = (getattr(item, "aggregatedOutput", "") or "").strip()
        icon = "✅" if exit_code == 0 else "❌"
        lines = [f"{icon} `{cmd}` (exit {exit_code})"]
        if output:
            lines.append(f"```\n{output[:MAX_COMMAND_OUTPUT]}\n```")
        await self.session.notify("\n".join(lines))

    async def _on_file_change(self, task: Task, item: Any) -> None:
        changes = getattr(item, "changes", None) or []
        if not changes:
            return
        lines = [f"📝 `{task.task_id}` file changes"]
        for c in changes:
            path = getattr(c, "path", None) or "?"
            kind = getattr(c, "kind", None) or "modify"
            kind_value = getattr(kind, "value", kind)
            icon = {"create": "➕", "delete": "➖"}.get(kind_value, "✏️")
            lines.append(f"  {icon} `{path}`")
        await self.session.notify("\n".join(lines))

    async def _on_web_search(self, task: Task, item: Any) -> None:
        query = (getattr(item, "query", "") or "").strip()
        if query:
            await self.session.notify(f"🔍 `{task.task_id}` search: {query}")

    async def _on_entered_review_mode(self, task: Task, item: Any) -> None:
        review = (getattr(item, "review", "") or "").strip()
        await self.session.notify(f"👁️ `{task.task_id}` entered review mode: {review}")

    async def _on_exited_review_mode(self, task: Task, item: Any) -> None:
        review = (getattr(item, "review", "") or "").strip()
        msg = f"👁️ `{task.task_id}` review complete"
        if review:
            msg += f"\n\n{review}"
        await self.session.notify(msg)

    # ── turn/completed ───────────────────────────────────────────────────────

    async def _on_turn_completed(self, task: Task, params: Any) -> None:
        turn = params.turn
        status = getattr(turn.status, "value", turn.status)

        # In "off" mode: flush the buffered last item before turn/completed.
        if self.session.verbose == "off" and task.last_item is not None:
            await self._show_last_item(task)

        if status == "failed":
            error = turn.error
            msg_text = getattr(error, "message", "") or "unknown error"
            code = getattr(error, "codexErrorInfo", None)
            code_str = getattr(code, "value", code) if code else ""
            error_str = f"{msg_text} ({code_str})" if code_str else msg_text
            await self.session.notify(f"❌ Codex task `{task.task_id}` failed: {error_str}")
        elif status == "interrupted":
            await self.session.notify(f"⏱️ Codex task `{task.task_id}` interrupted")
        else:
            await self.session.notify(
                f"✅ Codex task `{task.task_id}` completed\n"
                f"Continue: `/codex reply {task.task_id} <message>`",
            )

    async def _show_last_item(self, task: Task) -> None:
        """Show the buffered last item (used by 'off' verbose level)."""
        item = task.last_item
        item_type = task.last_item_type
        task.last_item = None
        task.last_item_type = ""
        if item_type == "agentMessage":
            await self._on_agent_message(task, item)
        elif item_type == "plan":
            await self._on_plan(task, item)
        elif item_type == "commandExecution":
            await self._on_command_execution(task, item)
        elif item_type == "fileChange":
            await self._on_file_change(task, item)
        elif item_type == "webSearch":
            await self._on_web_search(task, item)
        elif item_type == "enteredReviewMode":
            await self._on_entered_review_mode(task, item)
        elif item_type == "exitedReviewMode":
            await self._on_exited_review_mode(task, item)
        elif item_type == "contextCompaction":
            await self.session.notify(f"🗜️ `{task.task_id}` context compacted")
        else:
            logger.debug("codex handler: last item type %r ignored", item_type)
