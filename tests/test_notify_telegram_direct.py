from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from codex_websocket_v2.core.state import TaskTarget
from codex_websocket_v2.surfaces import notify
from codex_websocket_v2.surfaces.notify import notify_user


class FakeBot:
    sent: list[dict] = []

    def __init__(self, token: str) -> None:
        self.token = token

    async def send_message(self, **kwargs):
        self.sent.append({"token": self.token, **kwargs})
        return SimpleNamespace(message_id=1)


@pytest.fixture(autouse=True)
def reset_notify_state():
    notify.set_main_loop(None)
    FakeBot.sent = []
    old_tools = sys.modules.get("tools")
    old_send_message_tool = sys.modules.get("tools.send_message_tool")
    tools_module = types.ModuleType("tools")
    send_message_tool_module = types.ModuleType("tools.send_message_tool")
    send_message_tool_module._send_to_platform = AsyncMock(return_value={"success": True})
    tools_module.send_message_tool = send_message_tool_module
    sys.modules["tools"] = tools_module
    sys.modules["tools.send_message_tool"] = send_message_tool_module
    yield
    notify.set_main_loop(None)
    FakeBot.sent = []
    if old_tools is None:
        sys.modules.pop("tools", None)
    else:
        sys.modules["tools"] = old_tools
    if old_send_message_tool is None:
        sys.modules.pop("tools.send_message_tool", None)
    else:
        sys.modules["tools.send_message_tool"] = old_send_message_tool


@pytest.mark.asyncio
async def test_notify_user_telegram_uses_direct_bot_not_live_adapter_or_tool(monkeypatch):
    notify.set_main_loop(asyncio.get_running_loop())
    send_mock = AsyncMock(return_value={"success": True})

    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: SimpleNamespace(platforms={Platform.TELEGRAM: SimpleNamespace(token="tok", extra={})}),
        raising=False,
    )
    monkeypatch.setattr("telegram.Bot", FakeBot)

    with patch("tools.send_message_tool._send_to_platform", new=send_mock):
        await notify_user(
            TaskTarget(platform="telegram", chat_id="123", thread_id="456"),
            "**bold** <proposed_plan>",
        )

    send_mock.assert_not_awaited()
    assert len(FakeBot.sent) == 1
    sent = FakeBot.sent[0]
    assert sent["token"] == "tok"
    assert sent["chat_id"] == 123
    assert sent["message_thread_id"] == 456
    assert sent["parse_mode"].value == "MarkdownV2"
    assert sent["text"] == r"*bold* <proposed\_plan\>"


@pytest.mark.asyncio
async def test_notify_user_telegram_falls_back_to_plain_text_on_markdown_parse(monkeypatch):
    notify.set_main_loop(asyncio.get_running_loop())

    class ParseFailBot(FakeBot):
        async def send_message(self, **kwargs):
            if kwargs.get("parse_mode") is not None:
                raise Exception("Markdown parse error")
            return await super().send_message(**kwargs)

    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: SimpleNamespace(platforms={Platform.TELEGRAM: SimpleNamespace(token="tok", extra={})}),
        raising=False,
    )
    monkeypatch.setattr("telegram.Bot", ParseFailBot)

    await notify_user(TaskTarget(platform="telegram", chat_id="123"), "**bold** <proposed_plan>")

    assert len(FakeBot.sent) == 1
    sent = FakeBot.sent[0]
    assert sent["parse_mode"] is None
    assert sent["text"] == "bold <proposed_plan>"


@pytest.mark.asyncio
async def test_notify_user_non_telegram_keeps_existing_path(monkeypatch):
    notify.set_main_loop(None)
    send_mock = AsyncMock(return_value={"success": True})

    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: SimpleNamespace(platforms={Platform.SLACK: SimpleNamespace(enabled=True, token="tok", extra={})}),
        raising=False,
    )

    with patch("tools.send_message_tool._send_to_platform", new=send_mock):
        await notify_user(TaskTarget(platform="slack", chat_id="123"), "hello")

    send_mock.assert_awaited_once()
