"""기능5 검증: Discord 소통 툴 (가짜 IO로 네트워크 없이 검증)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src.discord_tools import (
    DISCORD_TOOL_NAMES,
    DiscordIO,
    build_discord_server,
    make_discord_tools,
)


class FakeIO:
    def __init__(self):
        self.sent = []
        self.replied = []
        self._history = [{"id": 10, "author": "사람", "content": "@Organt 보고서 만들어줘"}]

    async def send(self, content):
        self.sent.append(content)
        return 111

    async def reply(self, message_id, content):
        self.replied.append((message_id, content))
        return 222

    async def read(self, limit=20):
        return self._history[:limit]


def _tools(io):
    return {t.name: t for t in make_discord_tools(io)}


# --- MCP 툴 동작 ---

def test_send_message_툴이_io로_전송():
    io = FakeIO()
    res = asyncio.run(_tools(io)["send_message"].handler({"content": "안녕"}))
    assert io.sent == ["안녕"]
    assert "111" in res["content"][0]["text"]


def test_reply_message_툴이_id와_내용으로_답글():
    io = FakeIO()
    res = asyncio.run(_tools(io)["reply_message"].handler({"message_id": 10, "content": "넵"}))
    assert io.replied == [(10, "넵")]
    assert "222" in res["content"][0]["text"]


def test_read_thread_툴이_최근메시지_JSON():
    io = FakeIO()
    res = asyncio.run(_tools(io)["read_thread"].handler({"limit": 5}))
    data = json.loads(res["content"][0]["text"])
    assert data[0]["content"] == "@Organt 보고서 만들어줘"


def test_서버_3개툴_이름():
    server = build_discord_server(FakeIO())
    assert server["name"] == "discord"
    assert {t.name for t in make_discord_tools(FakeIO())} == {
        "send_message", "reply_message", "read_thread",
    }
    assert DISCORD_TOOL_NAMES == [
        "mcp__discord__send_message",
        "mcp__discord__reply_message",
        "mcp__discord__read_thread",
    ]


# --- DiscordIO 어댑터 ---

def test_discordio_send가_채널의_send를_호출():
    sent = []
    async def _send(content):
        sent.append(content)
        return SimpleNamespace(id=999)
    ch = SimpleNamespace(send=_send)
    io = DiscordIO(SimpleNamespace(get_channel=lambda cid: ch), 5)
    assert asyncio.run(io.send("x")) == 999
    assert sent == ["x"]


def test_discordio_채널없으면_에러():
    async def _fetch(cid):
        return None  # API 조회도 실패(채널 없음)
    client = SimpleNamespace(get_channel=lambda cid: None, fetch_channel=_fetch)
    io = DiscordIO(client, 5)
    with pytest.raises(RuntimeError):
        asyncio.run(io.send("x"))
