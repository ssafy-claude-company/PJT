"""런타임 엔트리포인트 — SYS를 가동한다.

구조: User ↔ SMS(Discord) ↔ SYS ↔ Organt.
SYS는 System 봇(관리자)으로 유저 채널을 감시하다가, User가 보낸 `[Request]`(To: @담당)
가 오면 담당(리더) Organt를 깨워 흐름을 시작한다. 흐름은 항상 1명만 활성(단일흐름),
필요하면 Organt끼리 request로 동료를 부르고(중첩 베턴), 리더의 반환값이 [Response]로
유저에게 돌아가며 흐름이 종료된다.

Organt 로스터는 ORGANT_ROSTER 환경변수로 구성한다(없으면 TEST_BOT 단독 리더):
    ORGANT_ROSTER=TEST_BOT_1:담당자,TEST_OBT_2:프론트엔드,TEST_OBT_3:디자인
각 항목은 '토큰_환경변수명:역할'이며 첫 항목이 리더다(토큰 값은 각 환경변수에 둔다).
"""
import asyncio
import os
from typing import Dict, List, Tuple

import discord
from claude_agent_sdk import HookMatcher

from .audit import AuditLog, make_post_tool_use_hook
from .config import Config, load_config
from .discord_guide import DiscordGuide
from .guide_tools import LEADER_TOOLS, REQUEST_TOOL
from .organt import Organt, build_options
from .permissions import make_pre_tool_use_hook
from .protocol import Request, parse
from .sys_core import Sys


def load_roster() -> List[Tuple[str, str]]:
    """ORGANT_ROSTER → [(token, 역할설명), ...]. 첫 항목이 리더. 없으면 TEST_BOT 단독.

    형식: '토큰_환경변수명:역할설명' 을 ';' 로 구분(역할설명에 쉼표·괄호 사용 가능). 예:
      TEST_BOT_1:담당자(백엔드 직접 구현·리더); TEST_OBT_2:프론트엔드(스펙대로 CSS 구현)
    """
    roster: List[Tuple[str, str]] = []
    spec = os.environ.get("ORGANT_ROSTER", "").strip()
    if spec:
        sep = ";" if ";" in spec else ","
        for item in spec.split(sep):
            env_name, _, role = item.strip().partition(":")
            token = os.environ.get(env_name.strip(), "").strip()
            if token:
                roster.append((token, role.strip() or env_name.strip()))
    if not roster:
        token = os.environ.get("TEST_BOT", "").strip()
        if token:
            roster.append((token, "담당자"))
    if not roster:
        raise RuntimeError("Organt 로스터가 비었습니다. ORGANT_ROSTER 또는 TEST_BOT 를 설정하세요.")
    return roster


async def _connect(token: str) -> Tuple[discord.Client, asyncio.Task]:
    """봇 하나를 연결하고 on_ready까지 기다린다."""
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    ready = asyncio.Event()

    @client.event
    async def on_ready():
        ready.set()

    task = asyncio.create_task(client.start(token))
    await asyncio.wait_for(ready.wait(), 30)
    return client, task


def _make_builder(cfg: Config, audit: AuditLog):
    """role에 맞는 도구·권한·훅·State를 갖춘 Organt를 만드는 빌더를 돌려준다."""
    def organt_builder(organt_id, server, role):
        # Glob/Read는 동료 산출물 탐색(연동)을 위한 읽기 전용 도구. 쓰기는 훅이 작업공간으로 제한.
        allowed = ["Read", "Write", "Edit", "Glob", "ToolSearch", REQUEST_TOOL]
        if role == "leader":
            allowed = allowed + LEADER_TOOLS
        state_path = cfg.audit_log_path.parent / f"organt_state_{organt_id}.json"
        return Organt(cfg, build_options(
            cfg, allowed_tools=allowed, mcp_servers={"guide": server},
            hooks={
                "PreToolUse": [HookMatcher(hooks=[make_pre_tool_use_hook(audit, allowed)])],
                "PostToolUse": [HookMatcher(hooks=[make_post_tool_use_hook(audit)])],
            },
        ), state_path=str(state_path))
    return organt_builder


async def run() -> None:
    cfg = load_config()
    audit = AuditLog(cfg.audit_log_path)

    system_client, sys_task = await _connect(cfg.system_bot_token)
    tasks = [sys_task]
    organts: Dict[int, object] = {}
    bot_info: Dict[int, str] = {}
    leader_id = None
    for i, (token, role_label) in enumerate(load_roster()):
        client, task = await _connect(token)
        organts[client.user.id] = client
        bot_info[client.user.id] = role_label
        tasks.append(task)
        if i == 0:
            leader_id = client.user.id

    guide = DiscordGuide(system_client, organts)
    channel = (system_client.get_channel(cfg.channel_id)
               or await system_client.fetch_channel(cfg.channel_id))
    sysm = Sys(guide, channel.guild.id, _make_builder(cfg, audit), bot_info=bot_info)

    print(f"SYS 가동 — 리더={bot_info[leader_id]}({leader_id}), 팀={list(bot_info.values())}")
    print(f"#{channel.name} 에서 User [Request] 대기 중 (Ctrl+C 종료)")

    @system_client.event
    async def on_message(message):
        # 흐름은 User에서만 시작한다 — Organt/System 발화는 무시.
        if message.channel.id != cfg.channel_id:
            return
        if message.author.id in organts or message.author.id == system_client.user.id:
            return
        req = parse(
            message_id=str(message.id),
            author_id=message.author.id,
            mention_ids=[m.id for m in message.mentions],
            reply_to_id=(message.reference.message_id if message.reference else None),
            content=message.content,
        )
        if not isinstance(req, Request) or req.to_id is None:
            return
        audit.record("user_request", to=req.to_id, body=req.body[:200])
        await sysm.route_channel_request(cfg.channel_id, req)

    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
