"""Subscriber and response helpers for approval requests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from pydantic import BaseModel

from ..models import ApprovalRequestedEvent

from ...core.state import Result, Task, err, ok

if TYPE_CHECKING:
    from ...core.session import CodexSession


MODERN_COMMAND_APPROVAL = "commandExecution"
FILE_CHANGE_APPROVAL = "fileChange"
PERMISSIONS_APPROVAL = "permissions"
LEGACY_EXEC_APPROVAL = "legacyExecCommand"
LEGACY_APPLY_PATCH_APPROVAL = "legacyApplyPatch"
MAX_APPROVAL_CMD_PREVIEW = 200


class ApprovalRequestSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    async def __call__(self, event: ApprovalRequestedEvent) -> bool:
        if event.approval_kind in (MODERN_COMMAND_APPROVAL, LEGACY_EXEC_APPROVAL):
            await self._handle_command_approval(event.params, event.rpc_id, event.approval_kind)
        elif event.approval_kind == LEGACY_APPLY_PATCH_APPROVAL:
            await self._handle_apply_patch_approval(event.params, event.rpc_id)
        elif event.approval_kind == FILE_CHANGE_APPROVAL:
            await self._handle_file_change_approval(event.params, event.rpc_id)
        elif event.approval_kind == PERMISSIONS_APPROVAL:
            await self._handle_permissions_approval(event.params, event.rpc_id)
        else:
            return False
        return True

    def approval_meta(self, params: Any) -> tuple[Optional[Task], str]:
        thread_id = getattr(params, "threadId", None) or getattr(params, "conversationId", None)
        task = self.session.task_for_thread(thread_id) if thread_id else None
        task_id = task.task_id if task else "?"
        return task, task_id

    @staticmethod
    def approval_footer(task_id: str, *, accept_label: str = "Approve", decline_label: str = "Deny") -> str:
        return (
            f"{accept_label}: `/codex approve {task_id}`\n"
            f"{decline_label}: `/codex deny {task_id}`"
        )

    async def _handle_command_approval(self, params: Any, rpc_id: Any, approval_kind: str) -> None:
        task, task_id = self.approval_meta(params)
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
            self.approval_footer(task_id),
        ])
        self.session.stash_request(task, rpc_id, "command",
                                   {"preview": cmd_preview, "reason": reason, "cmd_type": approval_kind})
        await self.session.notify(notification)

    async def _handle_file_change_approval(self, params: Any, rpc_id: Any) -> None:
        task, task_id = self.approval_meta(params)
        reason = (getattr(params, "reason", "") or "").strip() or "Codex file change"
        grant_root = getattr(params, "grantRoot", None)

        lines = [f"⚠️ Codex task `{task_id}` requests write permission:"]
        if grant_root:
            lines.append(f"📂 Persistent write access to: `{grant_root}` (rest of session)")
        lines += [f"Reason: {reason}", "", self.approval_footer(task_id)]

        preview = f"write permission — {grant_root or 'cwd'}"
        self.session.stash_request(task, rpc_id, "command",
                                   {"preview": preview, "reason": reason, "cmd_type": FILE_CHANGE_APPROVAL,
                                    "grant_root": grant_root})
        await self.session.notify("\n".join(lines))

    async def _handle_apply_patch_approval(self, params: Any, rpc_id: Any) -> None:
        task, task_id = self.approval_meta(params)
        reason = (getattr(params, "reason", "") or "").strip() or "Codex file patch"
        grant_root = getattr(params, "grantRoot", None)
        file_changes: dict = getattr(params, "fileChanges", None) or {}

        lines = [f"⚠️ Codex task `{task_id}` requests to apply file changes:"]
        if grant_root:
            lines.append(f"📂 Persistent write access to: `{grant_root}` (rest of session)")

        preview_paths = []
        for path, change in list(file_changes.items())[:10]:
            change_obj = change.root if hasattr(change, "root") else change
            change_type = getattr(getattr(change_obj, "type", None), "value", None) or "modify"
            icon = {"add": "➕", "delete": "➖"}.get(change_type, "✏️")
            diff = getattr(change_obj, "unified_diff", None)
            diff_preview = f"\n```\n{diff[:200]}\n```" if diff else ""
            lines.append(f"  {icon} `{path}`{diff_preview}")
            preview_paths.append(path)

        if len(file_changes) > 10:
            lines.append(f"  … and {len(file_changes) - 10} more files")

        lines += [f"Reason: {reason}", "", self.approval_footer(task_id)]
        preview = ", ".join(preview_paths[:3]) or "(patch)"

        self.session.stash_request(task, rpc_id, "command",
                                   {"preview": preview, "reason": reason, "cmd_type": LEGACY_APPLY_PATCH_APPROVAL,
                                    "grant_root": grant_root})
        await self.session.notify("\n".join(lines))

    async def _handle_permissions_approval(self, params: Any, rpc_id: Any) -> None:
        task, task_id = self.approval_meta(params)
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
            self.approval_footer(task_id),
        ])
        self.session.stash_request(task, rpc_id, "command",
                                   {"preview": preview, "reason": reason, "cmd_type": PERMISSIONS_APPROVAL,
                                    "permissions": jsonable(perms)})
        await self.session.notify(notification)


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude_none=True, mode="json")
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "root"):
        return jsonable(value.root)
    return value


def build_approval_response(
    request_payload: Dict[str, Any] | None,
    decision: str,
    *,
    for_session: bool = False,
) -> Result:
    payload = request_payload or {}
    cmd_type = payload.get("cmd_type", MODERN_COMMAND_APPROVAL)

    if cmd_type in (MODERN_COMMAND_APPROVAL, FILE_CHANGE_APPROVAL, "exec"):
        return _modern_decision(cmd_type, decision, for_session=for_session)
    if cmd_type == PERMISSIONS_APPROVAL:
        return _permissions_decision(payload, decision, for_session=for_session)
    if cmd_type in (LEGACY_EXEC_APPROVAL, LEGACY_APPLY_PATCH_APPROVAL):
        return _legacy_review_decision(decision, for_session=for_session)
    return err(f"unknown approval request type {cmd_type!r}")


def _modern_decision(cmd_type: str, decision: str, *, for_session: bool) -> Result:
    if decision == "accept":
        return ok(payload={"decision": "acceptForSession" if for_session else "accept"})
    if decision == "decline":
        return ok(payload={"decision": "decline"})
    if decision == "cancel":
        return ok(payload={"decision": "cancel"})
    return err(f"unsupported decision {decision!r} for {cmd_type!r}")


def _permissions_decision(
    request_payload: Dict[str, Any],
    decision: str,
    *,
    for_session: bool,
) -> Result:
    if for_session:
        return err("acceptForSession is not supported for permissions approvals")
    if decision == "accept":
        permissions = request_payload.get("permissions") or {}
        return ok(payload={"permissions": permissions, "scope": "turn"})
    if decision in ("decline", "cancel"):
        return ok(payload={"permissions": {}, "scope": "turn"})
    return err(f"unsupported decision {decision!r} for permissions approval")


def _legacy_review_decision(decision: str, *, for_session: bool) -> Result:
    if decision == "accept":
        return ok(payload={"decision": "approved_for_session" if for_session else "approved"})
    if decision == "decline":
        return ok(payload={"decision": "denied"})
    if decision == "cancel":
        return ok(payload={"decision": "abort"})
    if decision == "timed_out":
        return ok(payload={"decision": "timed_out"})
    return err(f"unsupported legacy approval decision {decision!r}")
