# Codex Approval Request Handling 实现计划

## Summary
- 当前项目已在 `codex_websocket_v2/handlers.py:85` 接收 5 类 approval request，但 `CodexSession.approve_task` 目前把多数请求统一回成 `{"decision": "accept|decline"}`，不符合部分 schema。
- 目标是按 `codex-app-server-schema` 为每类 approval 保存明确 subtype，并在 approve/deny 时构造正确 JSON-RPC `result`。
- 保持现有用户入口不变：`/codex approve <task_id>`、`/codex approve --all <task_id>`、`/codex deny <task_id>`、`codex_tasks approve/deny`。

## Approval Request 与 Response 构成
- `item/commandExecution/requestApproval`
  - Params 关键字段：`threadId`、`turnId`、`itemId`、`approvalId?`、`command?`、`cwd?`、`reason?`、`commandActions?`、`proposedExecpolicyAmendment?`、`networkApprovalContext?`、`proposedNetworkPolicyAmendments?`。
  - Approve response：`{"decision": "accept"}`。
  - Approve for session：`{"decision": "acceptForSession"}`。
  - Deny response：`{"decision": "decline"}`。
  - Cancel/internal abort：`{"decision": "cancel"}`。
  - 后续可扩展：`{"decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": [...]}}}`、`{"decision": {"applyNetworkPolicyAmendment": {"network_policy_amendment": {"action": "allow|deny", "host": "..."}}}}`。
- `item/fileChange/requestApproval`
  - Params 关键字段：`threadId`、`turnId`、`itemId`、`grantRoot?`、`reason?`。
  - Approve response：`{"decision": "accept"}`。
  - Approve for session：`{"decision": "acceptForSession"}`。
  - Deny response：`{"decision": "decline"}`。
  - Cancel/internal abort：`{"decision": "cancel"}`。
- `item/permissions/requestApproval`
  - Params 关键字段：`threadId`、`turnId`、`itemId`、`cwd`、`permissions`、`reason?`。
  - Approve response：`{"permissions": <requested permissions>, "scope": "turn"}`。
  - Approve for session：不通过现有 `--all` 支持；若未来开放，应回 `scope: "session"`。
  - Deny response：`{"permissions": {}, "scope": "turn"}`。
  - 可选字段：仅在明确需要时带 `strictAutoReview`。
- `applyPatchApproval`
  - Params 关键字段：`conversationId`、`callId`、`fileChanges`、`grantRoot?`、`reason?`。
  - Approve response：`{"decision": "approved"}`。
  - Approve for session：`{"decision": "approved_for_session"}`。
  - Deny response：`{"decision": "denied"}`。
  - Cancel/internal abort：`{"decision": "abort"}`。
  - Timeout/internal timeout：`{"decision": "timed_out"}`。
- `execCommandApproval`
  - Params 关键字段：`conversationId`、`callId`、`approvalId?`、`command`、`cwd`、`parsedCmd`、`reason?`。
  - Approve response：`{"decision": "approved"}`。
  - Approve for session：`{"decision": "approved_for_session"}`。
  - Deny response：`{"decision": "denied"}`。
  - Cancel/internal abort：`{"decision": "abort"}`。
  - Timeout/internal timeout：`{"decision": "timed_out"}`。

## Key Changes
- 在 `codex_websocket_v2/handlers.py:85` 的分发中为每个 approval handler 存入明确 `cmd_type`：
  - `commandExecution`、`fileChange`、`permissions`、`legacyExecCommand`、`legacyApplyPatch`。
- 在 `codex_websocket_v2/session.py:354` 重构 `approve_task`：
  - 根据 `task.request_payload["cmd_type"]` 调用专门的 payload builder。
  - 现代 command/fileChange 使用 `accept|acceptForSession|decline|cancel`。
  - legacy exec/applyPatch 使用 `approved|approved_for_session|denied|abort`。
  - permissions approve 回传原始 requested `permissions`，deny 回传空权限 profile。
- 在 `_handle_permissions_approval` 保存 schema 原始 `permissions` 的 JSON 可序列化版本，避免 approve 时丢失授权内容。
- 在 `_handle_apply_patch_approval` 不再伪装成 `fileChange`，改为 legacy applyPatch subtype，确保 response decision 使用 `approved/denied` 系列。
- 保持 `tools.py:136` 和 `commands.py:286` 的外部接口不变，只依赖 `approve_task` 内部适配不同 response schema。

## Slash Command 到 Response 映射
- `item/commandExecution/requestApproval`
  - `/codex approve <task_id>` → `{"decision": "accept"}`。
  - `/codex approve --all <task_id>` → `{"decision": "acceptForSession"}`。
  - `/codex deny <task_id>` → `{"decision": "decline"}`。
- `item/fileChange/requestApproval`
  - `/codex approve <task_id>` → `{"decision": "accept"}`。
  - `/codex approve --all <task_id>` → `{"decision": "acceptForSession"}`。
  - `/codex deny <task_id>` → `{"decision": "decline"}`。
- `item/permissions/requestApproval`
  - `/codex approve <task_id>` → `{"permissions": <requested permissions>, "scope": "turn"}`。
  - `/codex approve --all <task_id>` → 返回用户可见错误，不发送 WS response；原因是现有 CLI 不支持 permissions session scope。
  - `/codex deny <task_id>` → `{"permissions": {}, "scope": "turn"}`。
- `applyPatchApproval`
  - `/codex approve <task_id>` → `{"decision": "approved"}`。
  - `/codex approve --all <task_id>` → `{"decision": "approved_for_session"}`。
  - `/codex deny <task_id>` → `{"decision": "denied"}`。
- `execCommandApproval`
  - `/codex approve <task_id>` → `{"decision": "approved"}`。
  - `/codex approve --all <task_id>` → `{"decision": "approved_for_session"}`。
  - `/codex deny <task_id>` → `{"decision": "denied"}`。

## Test Plan
- 静态验证：为每类 schema 构造最小 params，调用对应 handler 后检查 `task.request_payload["cmd_type"]` 和保存的 preview/reason。
- 单元验证 response builder：
  - commandExecution approve/deny/for_session 分别生成 `accept`、`decline`、`acceptForSession`。
  - fileChange approve/deny/for_session 分别生成 `accept`、`decline`、`acceptForSession`。
  - permissions approve 生成 requested permissions + `scope: "turn"`；deny 生成空 permissions + `scope: "turn"`；`for_session` 返回错误。
  - legacy exec/applyPatch approve/deny/for_session 分别生成 `approved`、`denied`、`approved_for_session`。
- 集成验证：mock `bridge.ws_send`，确认 JSON-RPC response 为 `{"jsonrpc":"2.0","id":rpc_id,"result":payload}` 且 pending request 被清空。
- 回归验证：`/codex approve`、`/codex approve --all`、`/codex deny` 的用户可见结果保持兼容。

## Assumptions
- `permissions` deny 没有 schema-level `decision` 字段，因此拒绝通过“授予空权限”表达。
- `--all` 继续只对 commandExecution、fileChange、legacy exec/applyPatch 生效；permissions 默认不支持。
- 暂不实现 execpolicy/network policy amendment 的 CLI 参数，只保留内部 builder 扩展点。
- 不修改生成的 `codex-app-server-schema` 文件，只在业务层适配 schema。
