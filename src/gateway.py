"""Discord 게이트웨이: System/Organt 봇 2개를 띄우고, System 봇이 메시지를 수집·라우팅한다.

- 기능2: 두 봇을 Discord에 연결.
- 기능3: System 봇이 대상 채널의 사람 메시지를 모두 수집하고,
         Organt가 멘션된 경우에만 Organt에 라우팅한다(판정은 Router가 담당).

실제 audit 기록(JSONL)은 기능6, Organt(LLM) 처리·응답은 기능4·5에서 붙인다.
여기서는 수집/라우팅 시 콜백(기본: 콘솔 출력)을 호출하는 seam만 둔다.
"""
import asyncio
from typing import Callable, Optional

import discord

from .config import Config
from .router import Router


class Gateway:
    """두 개의 Discord 봇(System·Organt)을 구동하고 메시지를 수집·라우팅하는 런타임."""

    def __init__(
        self,
        config: Config,
        on_collect: Optional[Callable[[discord.Message], None]] = None,
        on_route: Optional[Callable[[discord.Message], None]] = None,
    ):
        self.config = config
        intents = discord.Intents.default()
        intents.message_content = True  # 메시지 내용 읽기(privileged intent)
        self.system_bot = discord.Client(intents=intents)
        self.organt_bot = discord.Client(intents=intents)
        # 수집/라우팅 시 동작은 콜백으로 주입(미지정 시 콘솔 출력).
        # 기능4·5·6에서 실제 Organt 처리/audit 기록으로 교체된다.
        self._on_collect = on_collect or self._default_collect
        self._on_route = on_route or self._default_route
        self._register_events()

    # --- 메시지 수집/라우팅 ---

    def organt_user_id(self) -> Optional[int]:
        """Organt 봇의 사용자 ID. 아직 연결 전이면 None."""
        user = self.organt_bot.user
        return user.id if user is not None else None

    def router(self) -> Router:
        """현재 Organt 사용자 ID 기준의 Router를 만든다."""
        return Router(self.config.channel_id, self.organt_user_id())

    def _default_collect(self, message: discord.Message) -> None:
        print(f"[수집] #{message.channel.id} {message.author}: {message.content}")

    def _default_route(self, message: discord.Message) -> None:
        print(f"[라우팅→Organt] {message.author}: {message.content}")

    # --- 이벤트 등록 ---

    def _register_events(self):
        @self.system_bot.event
        async def on_ready():
            print(f"[System 봇] 연결됨: {self.system_bot.user}")

        @self.system_bot.event
        async def on_message(message):
            # System 봇만 수집·라우팅을 담당한다(최대한 얇게).
            decision = self.router().decide(message)
            if decision.collect:
                self._on_collect(message)
            if decision.route_to_organt:
                self._on_route(message)

        @self.organt_bot.event
        async def on_ready():
            print(f"[Organt 봇] 연결됨: {self.organt_bot.user}")

    async def run(self):
        """두 봇을 동시에 Discord에 연결한다."""
        await asyncio.gather(
            self.system_bot.start(self.config.system_bot_token),
            self.organt_bot.start(self.config.organt_bot_token),
        )
