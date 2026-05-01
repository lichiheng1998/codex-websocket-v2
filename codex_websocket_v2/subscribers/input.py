"""Subscriber for tool requestUserInput events."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ..events import UserInputRequestedEvent

if TYPE_CHECKING:
    from ..session import CodexSession

logger = logging.getLogger(__name__)


class UserInputRequestSubscriber:
    def __init__(self, session: "CodexSession") -> None:
        self.session = session

    async def __call__(self, event: UserInputRequestedEvent) -> bool:
        params = event.params
        questions = getattr(params, "questions", None) or []
        task = event.task

        if task is None:
            logger.warning(
                "codex handler: requestUserInput for unknown thread %s — denying", event.thread_id)
            await self.session.bridge.ws_send(json.dumps({
                "jsonrpc": "2.0", "id": event.rpc_id,
                "error": {"code": -32000, "message": "no active task for thread"},
            }))
            return True

        lines = [f"🤔 Codex task `{task.task_id}` needs you to answer:"]
        for idx, question in enumerate(questions, 1):
            question_text = getattr(question, "question", "") or ""
            lines.append(f"\n{idx}. {question_text}")
            if getattr(question, "isOther", False):
                lines.append("   _(free-text reply accepted)_")
        lines.append(f"\nReply with: `/codex reply {task.task_id} <your answer>`")

        self.session.stash_request(task, event.rpc_id, "input", {
            "questions": questions,
            "preview": questions[0].question if questions else "",
        })
        await self.session.notify("\n".join(lines))
        return True
