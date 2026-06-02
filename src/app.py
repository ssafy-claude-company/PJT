"""Step 1 런타임 조립.

System 봇이 사람 메시지를 수집(audit)하고 Organt 멘션 시 라우팅하면,
Organt(LLM)가 작업공간에서 파일을 만들고 Discord에 답글한다.
모든 흐름(수집·라우팅·툴 호출·응답)은 audit JSONL에 남는다.
"""
import asyncio

from claude_agent_sdk import HookMatcher

from .audit import AuditLog, make_post_tool_use_hook
from .channels import resolve_channel_id
from .config import Config
from .discord_tools import DISCORD_TOOL_NAMES, DiscordIO, build_discord_server
from .gateway import Gateway
from .organt import Organt, build_options
from .permissions import make_pre_tool_use_hook, organt_allowed_tools
from .subagents import organt_subagents


class App:
    """게이트웨이 + Organt + Discord 툴 + audit 를 하나로 묶은 런타임."""

    def __init__(self, config: Config):
        self.config = config
        self.audit = AuditLog(config.audit_log_path)
        self.gateway = Gateway(
            config,
            on_collect=self._on_collect,
            on_route=self._schedule_route,
        )
        # 발신·읽기는 Organt 봇으로.
        self.io = DiscordIO(self.gateway.organt_bot, config.channel_id)
        # 권한: 허용 도구만 통과(PreToolUse 차단), 모든 툴 호출은 PostToolUse가 audit 기록.
        allowed = organt_allowed_tools(DISCORD_TOOL_NAMES)
        pre_hook = make_pre_tool_use_hook(self.audit, allowed)
        post_hook = make_post_tool_use_hook(self.audit)
        self.organt = Organt(config, build_options(
            config,
            mcp_servers={"discord": build_discord_server(self.io)},
            agents=organt_subagents(),   # 실작업은 서브에이전트에 위임
            allowed_tools=allowed,
            hooks={
                "PreToolUse": [HookMatcher(hooks=[pre_hook])],
                "PostToolUse": [HookMatcher(hooks=[post_hook])],
            },
        ))
        self._route_tasks = set()

    # --- 수집/라우팅 콜백 (System 봇 on_message에서 호출) ---

    def _on_collect(self, message):
        self.audit.record(
            "collect",
            author=str(message.author),
            message_id=getattr(message, "id", None),
            content=message.content,
        )

    def _schedule_route(self, message):
        # on_message는 동기 콜백 경로 → Organt(LLM) 처리는 백그라운드 태스크로 돌린다.
        task = asyncio.create_task(self._route(message))
        self._route_tasks.add(task)
        task.add_done_callback(self._route_tasks.discard)

    async def _route(self, message):
        self.audit.record(
            "route",
            author=str(message.author),
            message_id=getattr(message, "id", None),
            content=message.content,
        )
        prompt = (
            f"Discord 사용자 '{message.author}'가 당신을 멘션했습니다.\n"
            f"메시지: {message.content}\n\n"
            f"요청을 처리하세요. 파일은 현재 작업 디렉터리에 상대경로로 Write 툴을 써서 "
            f"만드세요(절대경로 금지). 끝나면 reply_message 툴로 답글을 보내세요 — "
            f"message_id 는 문자열 \"{message.id}\" 를 그대로 사용하세요. "
            f"불필요한 탐색 없이 최소한의 단계로 처리하세요."
        )
        resp = await self.organt.handle(prompt)
        self.audit.record("organt_reply", text=resp)
        return resp

    async def drain(self):
        """진행 중인 라우팅 작업이 끝날 때까지 기다린다(테스트/데모용)."""
        if self._route_tasks:
            await asyncio.gather(*list(self._route_tasks))

    async def resolve_channel(self):
        """봇 연결 후 실제 대상 텍스트 채널을 해석해 게이트웨이/IO에 반영한다.

        env CHANNEL_ID가 길드 ID여도 그 길드의 텍스트 채널로 자동 해석된다.
        """
        resolved = await resolve_channel_id(self.gateway.system_bot, self.config.channel_id)
        self.gateway.target_channel_id = resolved
        self.io.channel_id = resolved
        self.audit.record("channel_resolved", configured=self.config.channel_id, resolved=resolved)
        return resolved

    async def run(self):
        # on_ready 이벤트로 두 봇의 준비를 기다린다(wait_until_ready는 start 직후엔
        # 'Client not initialised'가 나므로 사용하지 않는다).
        sys_ready, org_ready = asyncio.Event(), asyncio.Event()

        @self.gateway.system_bot.event
        async def on_ready():
            print(f"[System 봇] 연결됨: {self.gateway.system_bot.user}", flush=True)
            sys_ready.set()

        @self.gateway.organt_bot.event
        async def on_ready():  # noqa: F811
            print(f"[Organt 봇] 연결됨: {self.gateway.organt_bot.user}", flush=True)
            org_ready.set()

        async def _post_ready():
            await sys_ready.wait()
            await org_ready.wait()
            await self.resolve_channel()
            print(f"[App] 대상 채널 해석됨: {self.gateway.target_channel_id}", flush=True)

        await asyncio.gather(
            self.gateway.system_bot.start(self.config.system_bot_token),
            self.gateway.organt_bot.start(self.config.organt_bot_token),
            _post_ready(),
        )
