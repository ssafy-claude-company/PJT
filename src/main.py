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
import logging
import os
import traceback
from typing import Dict, List, Tuple

import discord
from claude_agent_sdk import HookMatcher

from .audit import AuditLog, make_post_tool_use_hook
from .config import Config, load_config
from .discord_guide import DiscordGuide
from .guide_tools import COORD_TOOLS, FLOW_TOOLS, LEADER_TOOLS
from .organt import Organt, build_options
from .permissions import make_pre_tool_use_hook
from .protocol import Kind, Request, Response, parse
from .sys_core import Sys


def load_roster() -> List[Tuple[str, str]]:
    """ORGANT_ROSTER → [(token, 역할), ...]. 첫 항목이 리더. 없으면 TEST_BOT 단독.

    형식: '토큰_환경변수명:역할' 을 ';' 로 구분. 역할은 '맨 도메인 정체성'만 적는다(누가 무엇을
    어떻게 할지·인터페이스·분배는 라벨에 박지 말 것 — 런타임 협의로 정해짐). 예:
      TEST_BOT_1:담당자; TEST_OBT_2:백엔드; TEST_OBT_3:프론트엔드; TEST_OBT_4:디자이너; TEST_OBT_5:QA
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
    """봇 하나를 연결하고 on_ready까지 기다린다. 일시적 TLS/클럭 스큐 블립엔 재시도."""
    intents = discord.Intents.default()
    intents.message_content = True
    last = None
    for attempt in range(4):
        client = discord.Client(intents=intents)
        ready = asyncio.Event()

        @client.event
        async def on_ready():
            ready.set()

        try:
            task = asyncio.create_task(client.start(token))
            await asyncio.wait_for(ready.wait(), 30)
            return client, task
        except Exception as e:
            last = e
            try:
                await client.close()
            except Exception:
                pass
            await asyncio.sleep(3 * (attempt + 1))
    raise last


def _make_builder(cfg: Config, audit: AuditLog, bot_info=None):
    """role에 맞는 도구·권한·훅·State를 갖춘 Organt를 만드는 빌더를 돌려준다."""
    bot_info = bot_info or {}
    def organt_builder(organt_id, server, role, flow=None):
        # 리더도 한 명의 직원 — 구현 도구(Write/Edit)를 그대로 갖는다. 차이는 권한이 아니라
        # 역할: 목표는 팀 합의로 정하고(set_goal), Work 위임 본문은 '스펙'이 아니라
        # '측정가능한 목표'이며, 받은 owner가 구현·검증까지 끝까지 책임진다.
        allowed = ["Read", "Write", "Edit", "Glob", "Grep", "ToolSearch", *FLOW_TOOLS]
        turns = 45
        if role == "leader":
            allowed = allowed + LEADER_TOOLS
            turns = 110         # Task마다 빈 껍데기→팀 회의+분배+조율로 턴이 더 필요(기본 16은 부족)
        state_path = cfg.audit_log_path.parent / f"organt_state_{organt_id}.json"
        label = bot_info.get(organt_id, role)   # 협업 관찰성: 로그에 '누가' 남기기
        return Organt(cfg, build_options(
            cfg, allowed_tools=allowed, mcp_servers={"guide": server}, max_turns=turns,
            hooks={
                "PreToolUse": [HookMatcher(hooks=[make_pre_tool_use_hook(audit, allowed, actor=organt_id, role=label, flow=flow)])],
                "PostToolUse": [HookMatcher(hooks=[make_post_tool_use_hook(audit, actor=organt_id, role=label)])],
            },
        ), state_path=str(state_path))
    return organt_builder


async def run() -> None:
    cfg = load_config()
    audit = AuditLog(cfg.audit_log_path)
    # 진단 로깅: discord 게이트웨이·asyncio·SDK 경고/오류를 stderr로 흘려 listener.log에 남긴다
    # (리스너가 '조용히' 죽던 원인을 보기 위함). asyncio 미처리 예외도 잡아 기록.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("organt.listener")
    try:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda lp, ctx: log.error("asyncio 미처리 예외: %s", ctx.get("message") or ctx,
                                      exc_info=ctx.get("exception")))
    except Exception:
        pass

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
    sysm = Sys(guide, channel.guild.id, _make_builder(cfg, audit, bot_info), bot_info=bot_info,
               workspace=cfg.workspace_dir,
               projects_path=str(cfg.audit_log_path.parent / "projects.json"),
               session_dir=str(cfg.audit_log_path.parent))

    print(f"SYS 가동 — 리더={bot_info[leader_id]}({leader_id}), 팀={list(bot_info.values())}")
    print(f"#{channel.name} 에서 User 입력 대기 중 — 그냥 말 걸어도 됩니다(Ctrl+C 종료)")

    # 같은 메시지를 이 세션에서 두 번 처리하지 않는 가드(디스코드 재전달 등). 재시작 간 '완료 여부'는
    # 채널에 [Response]가 달렸는지로 판단한다(아래 부팅 복구) — 그래서 영속 dedup 파일은 쓰지 않는다.
    seen = set()

    @system_client.event
    async def on_message(message):
        try:
            # 흐름은 User에서만 시작 — Organt/System 발화는 무시.
            if message.author.id in organts or message.author.id == system_client.user.id:
                return
            ch = message.channel.id
            is_project = ch in sysm.projects        # 등록된 프로젝트 채널이면 '개입'
            if ch != cfg.channel_id and not is_project:
                return
            req = parse(
                message_id=str(message.id),
                author_id=message.author.id,
                mention_ids=[m.id for m in message.mentions],
                reply_to_id=(message.reference.message_id if message.reference else None),
                content=message.content,
            )
            if not isinstance(req, Request):
                if is_project and (message.content or "").strip():
                    req = Request(to_id=None, kind=Kind.WORK, body=message.content.strip(),
                                  from_id=message.author.id, message_id=str(message.id))
                else:
                    return                   # 메인 채널은 구조적 [Request]만 시작
            if str(message.id) in seen:      # 같은 메시지 두 번 처리 금지(세션 내 재전달 가드)
                return
            seen.add(str(message.id))
            if req.to_id is None:
                req.to_id = sysm.projects[ch]["leader"] if is_project else leader_id
            audit.record("user_request", to=req.to_id, body=req.body[:200])
            log.info("요청 수신: to=%s body=%r", req.to_id, (req.body or '')[:60])
            await sysm.route_channel_request(ch, req)   # 실제 채널 id로 라우팅
            log.info("요청 처리 완료: to=%s", req.to_id)
        except Exception:
            # 흐름 처리 중 어떤 예외도 리스너를 죽이지 않게 삼키고 전체 트레이스를 남긴다(조용한 죽음 방지).
            log.error("on_message 처리 중 예외:\n%s", traceback.format_exc())

    # 부팅 복구: 응답이 안 달린 [Request](중단됐거나 연결 직전 도착)는 다시 처리한다 — 리스너가 흐름
    # 도중 죽어도 재시작 시 그 요청을 마저 완료한다([Response]가 달린 요청은 완료로 보고 건너뜀).
    # ORGANT_SKIP_RECOVERY=1 이면 복구를 건너뛴다(깨끗한 슬레이트로 시작 — 이전 미응답 요청 재실행 안 함).
    try:
        recent = await guide.read_thread(cfg.channel_id, limit=30)
    except Exception:
        recent = []
    known = set(organts) | {system_client.user.id}
    pending = None
    for m in recent:
        if isinstance(m, Request) and m.from_id not in known:
            pending = m                  # User가 올린 미처리 Request 후보(응답이 뒤따르면 아래에서 해제)
        elif isinstance(m, Response):
            pending = None
    if os.environ.get("ORGANT_SKIP_RECOVERY"):
        if pending is not None:
            seen.add(str(pending.message_id))   # 재실행 안 하되, 이후 on_message 중복도 막게 seen 처리
            log.info("부팅 복구 건너뜀(ORGANT_SKIP_RECOVERY) — 미응답 요청 재실행 안 함")
        pending = None
    if pending is not None and str(pending.message_id) not in seen:
        seen.add(str(pending.message_id))
        if pending.to_id is None:
            pending.to_id = leader_id
        log.info("부팅 복구: 미응답 [Request] 재처리: %r", (pending.body or '')[:60])
        audit.record("user_request", to=pending.to_id, body=(pending.body or '')[:200])
        asyncio.create_task(sysm.route_channel_request(cfg.channel_id, pending))

    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
