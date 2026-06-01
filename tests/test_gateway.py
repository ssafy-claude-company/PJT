"""gateway 모듈 검증 (실제 Discord 연결 없이 구조만 확인)."""
import asyncio
from pathlib import Path
from unittest.mock import patch

import discord

from src.config import Config
from src.gateway import Gateway


def _fake_config() -> Config:
    return Config(
        system_bot_token="sys-tok",
        organt_bot_token="org-tok",
        channel_id=1,
        model=None,
        workspace_dir=Path("/tmp"),
        audit_log_path=Path("/tmp/audit.jsonl"),
    )


def test_봇_2개_생성():
    gw = Gateway(_fake_config())
    assert isinstance(gw.system_bot, discord.Client)
    assert isinstance(gw.organt_bot, discord.Client)
    assert gw.system_bot is not gw.organt_bot


def test_메시지내용_intent_활성화():
    gw = Gateway(_fake_config())
    assert gw.system_bot.intents.message_content is True
    assert gw.organt_bot.intents.message_content is True


def test_run이_두_봇을_각_토큰으로_start():
    gw = Gateway(_fake_config())
    calls = {}

    async def fake_start(token):
        calls[token] = calls.get(token, 0) + 1

    with patch.object(gw.system_bot, "start", side_effect=fake_start), \
         patch.object(gw.organt_bot, "start", side_effect=fake_start):
        asyncio.run(gw.run())

    assert calls == {"sys-tok": 1, "org-tok": 1}
