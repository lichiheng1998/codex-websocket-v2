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
        item_id = getattr(params, "itemId", None)
        approval_id = getattr(params, "approvalId", None)
        started_item = (getattr(task, "started_items", {}) or {}).get(item_id) if task and item_id else None
        command = (
            getattr(started_item, "command", None)
            or getattr(params, "command", None)
            or getattr(params, "commandText", None)
            or ""
        )
        if isinstance(command, list):
            command = " ".join(str(x) for x in command)
        cmd_str = str(command) or "(codex command)"
        cmd_preview = cmd_str[:MAX_APPROVAL_CMD_PREVIEW]

        lines = [
            f"⚠️ Codex task `{task_id}` requests to run a command:",
            f"```\n{cmd_preview}\n```",
        ]
        cwd = getattr(started_item, "cwd", None) or getattr(params, "cwd", None)
        if cwd:
            lines.append(f"cwd: `{_root_value(cwd)}`")
        actions = getattr(started_item, "commandActions", None) or getattr(params, "commandActions", None) or []
        self._append_command_actions(lines, actions)
        self._append_command_permissions(lines, params)
        if item_id:
            lines.append(f"Item: `{item_id}`")
        if approval_id:
            lines.append(f"Approval: `{approval_id}`")
        lines += [f"Reason: {reason}", "", self.approval_footer(task_id)]

        self.session.stash_request(task, rpc_id, "command",
                                   {"preview": cmd_preview, "reason": reason, "cmd_type": approval_kind,
                                    "item_id": item_id, "approval_id": approval_id,
                                    "cwd": _root_value(cwd) if cwd else None,
                                    "command_actions": jsonable(actions),
                                    "additional_permissions": jsonable(getattr(params, "additionalPermissions", None)),
                                    "network_approval_context": jsonable(getattr(params, "networkApprovalContext", None)),
                                    "available_decisions": jsonable(getattr(params, "availableDecisions", None)),
                                    "started_item": jsonable(started_item)})
        await self.session.notify("\n".join(lines))

    async def _handle_file_change_approval(self, params: Any, rpc_id: Any) -> None:
        task, task_id = self.approval_meta(params)
        reason = (getattr(params, "reason", "") or "").strip() or "Codex file change"
        grant_root = getattr(params, "grantRoot", None)
        item_id = getattr(params, "itemId", None)
        started_item = (getattr(task, "started_items", {}) or {}).get(item_id) if task and item_id else None
        started_changes = getattr(started_item, "changes", None) or []

        lines = [f"⚠️ Codex task `{task_id}` requests write permission:"]
        preview_paths = self._append_started_file_changes(lines, started_changes)
        if grant_root:
            lines.append(f"📂 Persistent write access to: `{grant_root}` (rest of session)")
        elif not preview_paths:
            lines.append("Files: not found in the preceding item/started fileChange item")
        if item_id:
            lines.append(f"Item: `{item_id}`")
        lines += [f"Reason: {reason}", "", self.approval_footer(task_id)]

        preview = ", ".join(preview_paths[:3]) or f"write permission — {grant_root or item_id or 'cwd'}"
        self.session.stash_request(task, rpc_id, "command",
                                   {"preview": preview, "reason": reason, "cmd_type": FILE_CHANGE_APPROVAL,
                                    "grant_root": grant_root, "item_id": item_id,
                                    "started_item": jsonable(started_item)})
        await self.session.notify("\n".join(lines))

    async def _handle_apply_patch_approval(self, params: Any, rpc_id: Any) -> None:
        task, task_id = self.approval_meta(params)
        reason = (getattr(params, "reason", "") or "").strip() or "Codex file patch"
        grant_root = getattr(params, "grantRoot", None)
        file_changes: dict = getattr(params, "fileChanges", None) or {}

        lines = [f"⚠️ Codex task `{task_id}` requests to apply file changes:"]
        if grant_root:
            lines.append(f"📂 Persistent write access to: `{grant_root}` (rest of session)")

        preview_paths = self._append_file_changes(lines, file_changes)

        lines += [f"Reason: {reason}", "", self.approval_footer(task_id)]
        preview = ", ".join(preview_paths[:3]) or "(patch)"

        self.session.stash_request(task, rpc_id, "command",
                                   {"preview": preview, "reason": reason, "cmd_type": LEGACY_APPLY_PATCH_APPROVAL,
                                    "grant_root": grant_root})
        await self.session.notify("\n".join(lines))

    @staticmethod
    def _append_started_file_changes(lines: list[str], changes: list) -> list[str]:
        preview_paths = []
        for change in changes[:10]:
            path = getattr(change, "path", None) or "?"
            kind = getattr(change, "kind", None) or "modify"
            kind_value = getattr(kind, "value", kind)
            icon = {"create": "➕", "delete": "➖"}.get(kind_value, "✏️")
            diff = getattr(change, "diff", None)
            diff_preview = f"\n```\n{diff[:200]}\n```" if diff else ""
            lines.append(f"  {icon} `{path}`{diff_preview}")
            preview_paths.append(path)

        if len(changes) > 10:
            lines.append(f"  … and {len(changes) - 10} more files")
        return preview_paths

    @staticmethod
    def _append_file_changes(lines: list[str], file_changes: dict) -> list[str]:
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
        return preview_paths

    @staticmethod
    def _append_command_actions(lines: list[str], actions: list) -> None:
        if not actions:
            return
        lines.append("Actions:")
        for action in actions[:5]:
            action_obj = action.root if hasattr(action, "root") else action
            action_type = getattr(getattr(action_obj, "type", None), "value", None) or "unknown"
            path = getattr(action_obj, "path", None)
            query = getattr(action_obj, "query", None)
            detail = _root_value(path) if path else query
            suffix = f" `{detail}`" if detail else ""
            lines.append(f"  - {action_type}{suffix}")
        if len(actions) > 5:
            lines.append(f"  - … and {len(actions) - 5} more actions")

    @staticmethod
    def _append_command_permissions(lines: list[str], params: Any) -> None:
        additional = getattr(params, "additionalPermissions", None)
        if additional:
            parts = []
            fs = getattr(additional, "fileSystem", None)
            if fs:
                writes = getattr(fs, "write", None) or []
                reads = getattr(fs, "read", None) or []
                if writes:
                    parts.append("write " + ", ".join(f"`{_root_value(path)}`" for path in writes[:5]))
                if reads:
                    parts.append("read " + ", ".join(f"`{_root_value(path)}`" for path in reads[:5]))
                entries = getattr(fs, "entries", None) or []
                for entry in entries[:5]:
                    access = getattr(getattr(entry, "access", None), "value", None) or getattr(entry, "access", "")
                    entry_path = getattr(entry, "path", None)
                    parts.append(f"{access} `{_root_value(entry_path)}`")
            network = getattr(additional, "network", None)
            if network and getattr(network, "enabled", False):
                parts.append("network access")
            if parts:
                lines.append("Additional permissions: " + "; ".join(parts))

        network_context = getattr(params, "networkApprovalContext", None)
        if network_context:
            protocol = getattr(getattr(network_context, "protocol", None), "value", None) or getattr(network_context, "protocol", "")
            host = getattr(network_context, "host", "")
            lines.append(f"Network request: `{protocol}://{host}`" if protocol else f"Network request: `{host}`")

        exec_amendment = getattr(params, "proposedExecpolicyAmendment", None)
        if exec_amendment:
            lines.append("Proposed exec policy: " + ", ".join(f"`{item}`" for item in exec_amendment[:5]))

        network_amendments = getattr(params, "proposedNetworkPolicyAmendments", None) or []
        if network_amendments:
            formatted = []
            for amendment in network_amendments[:5]:
                action = getattr(getattr(amendment, "action", None), "value", None) or getattr(amendment, "action", "")
                host = getattr(amendment, "host", "")
                formatted.append(f"{action} `{host}`")
            lines.append("Proposed network policy: " + ", ".join(formatted))

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
    if isinstance(value, tuple):
        return [jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "root"):
        return jsonable(value.root)
    if hasattr(value, "__dict__"):
        return {
            key: jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _root_value(value: Any) -> Any:
    while hasattr(value, "root"):
        value = value.root
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
