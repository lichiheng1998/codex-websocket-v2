"""Approval, elicitation, and input response operations for ``CodexSession``."""

from __future__ import annotations

import json

from ..events.subscribers.approval import build_approval_response
from .policies import SHORT_RPC_TIMEOUT
from .state import Result, err, ok


class RequestResolutionMixin:
    async def approve_task(self, task_id: str, decision: str, *, for_session: bool = False) -> Result:
        """Resolve a pending command approval or simple elicitation response."""
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
        send = await self.bridge.ws_send(json.dumps({
            "jsonrpc": "2.0", "id": rpc_id, "result": payload,
        }))
        if not send["ok"]:
            return send

        if task.request_payload is not None:
            task.request_payload["response_sent"] = True
            task.request_payload["decision"] = decision
        return ok(decision=decision)

    async def respond_task(self, task_id: str, content: "dict | None" = None) -> Result:
        """Resolve a pending elicitation request by sending schema data."""
        task = self.tasks.get(task_id)
        if task is None or task.request_rpc_id is None:
            return err(f"no pending request for task `{task_id}`")
        if task.request_type != "elicitation":
            return err(f"task `{task_id}` has a {task.request_type!r} request, not an elicitation")

        payload = {"action": "accept", "content": content}

        rpc_id = task.request_rpc_id
        send = await self.bridge.ws_send(json.dumps({
            "jsonrpc": "2.0", "id": rpc_id, "result": payload,
        }))
        if not send["ok"]:
            return send

        if task.request_payload is not None:
            task.request_payload["response_sent"] = True
            task.request_payload["decision"] = "respond"
        return ok(task_id=task_id, decision="respond")

    async def decline_task(self, task_id: str) -> Result:
        """Decline a pending elicitation request."""
        task = self.tasks.get(task_id)
        if task is None or task.request_rpc_id is None:
            return err(f"no pending request for task `{task_id}`")
        if task.request_type != "elicitation":
            return err(f"task `{task_id}` has a {task.request_type!r} request, not an elicitation")

        payload = {"action": "decline", "content": {}}

        rpc_id = task.request_rpc_id
        send = await self.bridge.ws_send(json.dumps({
            "jsonrpc": "2.0", "id": rpc_id, "result": payload,
        }))
        if not send["ok"]:
            return send

        if task.request_payload is not None:
            task.request_payload["response_sent"] = True
            task.request_payload["decision"] = "decline"
        return ok(task_id=task_id, decision="decline")

    async def input_task(
        self,
        task_id: str,
        answer: str = "",
        *,
        responses: "list[str] | None" = None,
        answers: "list[list[str]] | None" = None,
    ) -> Result:
        """Resolve a pending input request by sending the user's answer(s)."""
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
            pad = list(answers[-1]) if answers else []
            answer_groups = [list(group) for group in answers]
            answer_groups = (answer_groups + [pad] * n)[:n]
        elif responses is not None:
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
        send = await self.bridge.ws_send(json.dumps({
            "jsonrpc": "2.0", "id": rpc_id,
            "result": {"answers": response_payload},
        }))
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
