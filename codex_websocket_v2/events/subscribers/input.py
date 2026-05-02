"""Subscriber for tool requestUserInput events."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ..models import UserInputRequestedEvent

if TYPE_CHECKING:
    from ...core.session import CodexSession

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
            header = getattr(question, "header", "") or ""
            question_text = getattr(question, "question", "") or ""
            title = f"{header}: {question_text}" if header else question_text
            lines.append(f"\n{idx}. {title}")
            options = getattr(question, "options", None) or []
            if options:
                lines.append("   Options:")
                for option in options:
                    label = getattr(option, "label", "") or ""
                    description = getattr(option, "description", "") or ""
                    if description:
                        lines.append(f"   - `{label}` — {description}")
                    else:
                        lines.append(f"   - `{label}`")
            if getattr(question, "isOther", False):
                lines.append("   _(free-text answer accepted)_")
            if getattr(question, "isSecret", False):
                lines.append("   _(secret answer requested; this chat message will be forwarded)_")
        lines.append(f"\nAnswer with: `/codex answer {task.task_id} <your answer>`")
        if len(questions) > 1:
            lines.append(
                f"Multiple answers: `/codex answer {task.task_id} <a1> | <a2> | <a3>`"
            )
        lines.append(
            f"Multiple selections per question: `/codex answer {task.task_id} [<q1a>|<q1b>] [<q2a>]`"
        )
        lines.append("Use the exact option label shown above when options are listed.")

        self.session.stash_request(task, event.rpc_id, "input", {
            "questions": questions,
            "preview": questions[0].question if questions else "",
        })
        await self.session.notify("\n".join(lines))
        return True
