"""Discord 게이트웨이: System/Organt 봇 2개를 띄운다.

이 단계(기능2)에서는 두 봇을 Discord에 연결하는 것까지만 담당한다.
메시지 수집·라우팅은 다음 기능에서 붙인다.
"""
import asyncio

import discord

from .config import Config


class Gateway:
    """두 개의 Discord 봇(System·Organt)을 구동하는 런타임."""

    def __init__(self, config: Config):
        self.config = config
        intents = discord.Intents.default()
        intents.message_content = True  # 메시지 내용 읽기(privileged intent)
        self.system_bot = discord.Client(intents=intents)
        self.organt_bot = discord.Client(intents=intents)
        self._register_events()

    def _register_events(self):
        @self.system_bot.event
        async def on_ready():
            print(f"[System 봇] 연결됨: {self.system_bot.user}")

        @self.organt_bot.event
        async def on_ready():
            print(f"[Organt 봇] 연결됨: {self.organt_bot.user}")

    async def run(self):
        """두 봇을 동시에 Discord에 연결한다."""
        await asyncio.gather(
            self.system_bot.start(self.config.system_bot_token),
            self.organt_bot.start(self.config.organt_bot_token),
        )
