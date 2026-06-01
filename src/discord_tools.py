"""Organt의 Discord 소통 툴 (커스텀 MCP).

Organt(LLM)가 Discord에서 읽고/보내고/답글하도록 MCP 툴 3개를 제공한다.
발신은 Organt 봇 클라이언트로 한다.

실제 Discord 호출은 DiscordIO가 담당하고, 각 툴은 그 위의 얇은 래퍼다.
(테스트는 가짜 DiscordIO로 네트워크 없이 검증한다.)
"""
import json

import discord
from claude_agent_sdk import create_sdk_mcp_server, tool

# Organt가 이 툴들을 쓰려면 allowed_tools에 아래 이름을 추가한다.
DISCORD_TOOL_NAMES = [
    "mcp__discord__send_message",
    "mcp__discord__reply_message",
    "mcp__discord__read_thread",
]


class DiscordIO:
    """Organt 봇 클라이언트로 대상 채널을 읽고 쓰는 어댑터."""

    def __init__(self, client: discord.Client, channel_id: int):
        self.client = client
        self.channel_id = channel_id

    async def _channel(self):
        # 캐시에 없으면(봇이 막 연결된 경우 등) API로 직접 조회한다.
        ch = self.client.get_channel(self.channel_id)
        if ch is None:
            ch = await self.client.fetch_channel(self.channel_id)
        if ch is None:
            raise RuntimeError(f"채널 {self.channel_id} 를 찾을 수 없습니다(봇 미연결?).")
        return ch

    async def send(self, content: str) -> int:
        ch = await self._channel()
        msg = await ch.send(content)
        return msg.id

    async def reply(self, message_id: int, content: str) -> int:
        ch = await self._channel()
        ref = await ch.fetch_message(message_id)
        msg = await ref.reply(content)
        return msg.id

    async def read(self, limit: int = 20) -> list:
        out = []
        ch = await self._channel()
        async for m in ch.history(limit=limit):
            out.append({"id": m.id, "author": str(m.author), "content": m.content})
        return out


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def make_discord_tools(io):
    """DiscordIO(또는 동일 인터페이스)를 받아 MCP 툴 3개를 만든다."""

    @tool("send_message", "대상 채널에 메시지를 보낸다", {"content": str})
    async def send_message(args):
        mid = await io.send(args["content"])
        return _ok(f"sent message_id={mid}")

    @tool("reply_message", "특정 메시지에 답글을 단다", {"message_id": int, "content": str})
    async def reply_message(args):
        mid = await io.reply(int(args["message_id"]), args["content"])
        return _ok(f"replied message_id={mid}")

    @tool("read_thread", "대상 채널의 최근 메시지를 읽는다", {"limit": int})
    async def read_thread(args):
        msgs = await io.read(int(args.get("limit", 20)))
        return _ok(json.dumps(msgs, ensure_ascii=False))

    return [send_message, reply_message, read_thread]


def build_discord_server(io):
    """Organt 옵션의 mcp_servers={"discord": ...} 에 넣을 MCP 서버를 만든다."""
    return create_sdk_mcp_server("discord", "1.0.0", make_discord_tools(io))
