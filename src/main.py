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


# 봇의 '이름'은 직군이 아니라 사람 이름으로 둔다(직군은 Discord 역할로 부여). 회사 직원처럼 보이게.
KOREAN_NAMES = ["김민준", "이서연", "박지호", "최예은", "정우진", "강하린", "조성민", "윤지아",
                "장도현", "임수아", "한건우", "오유진", "신예준", "권나윤", "황시윤", "송하영",
                "배준영", "노아름", "문태경", "심유빈"]


def assign_korean_names(ids) -> Dict[int, str]:
    """봇 id들에 사람 이름(닉네임)을 차례로 배정 — 직군과 무관한 고정 정체성."""
    return {uid: KOREAN_NAMES[i % len(KOREAN_NAMES)] for i, uid in enumerate(ids)}


async def _connect(token: str, message_content: bool = False) -> Tuple[discord.Client, asyncio.Task]:
    """봇 하나를 연결하고 on_ready까지 기다린다. 일시적 TLS/클럭 스큐 블립엔 재시도.

    message_content(특권 인텐트)는 '메시지 내용을 읽어야 하는' System 봇만 True. Organt 봇은
    게시·타이핑만 하므로 불필요 → 새 봇이 개발자 포털에서 이 인텐트를 안 켜도 연결된다(봇 추가 간소화).
    """
    intents = discord.Intents.default()
    intents.message_content = message_content
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
        turns = 60          # 동료가 한 산출물을 한 번의 위임으로 끝내도록 여유(턴한도 미완 반환 줄임)
        if role == "leader":
            allowed = allowed + LEADER_TOOLS
            turns = 220         # 대부분 빌드가 한 세그먼트로 끝나 '10분마다 continue 재호출' 경계가 드물게(전원기획+분배+조율 감안)
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

    system_client, sys_task = await _connect(cfg.system_bot_token, message_content=True)
    log.info("System 봇(관리자/라우터) 연결: %s (%s)", system_client.user, system_client.user.id)
    tasks = [sys_task]
    organts: Dict[int, object] = {}
    bot_info: Dict[int, str] = {}
    leader_id = None
    for token, role_label in load_roster():
        # 한 봇의 토큰이 만료/오타여도 리스너 전체가 죽지(→래퍼 무한 재시작) 않게 — 그 봇만 건너뛰고
        # 나머지로 가동한다(예비 토큰을 점진적으로 늘리는 자동화의 안전장치).
        try:
            client, task = await _connect(token)
        except Exception as e:
            log.error("봇 연결 실패(건너뜀) role=%s: %s", role_label, e)
            continue
        organts[client.user.id] = client
        bot_info[client.user.id] = role_label
        tasks.append(task)
        log.info("워커 연결: %s (%s) ← 직군 '%s'", client.user, client.user.id, role_label)
        if leader_id is None:        # 첫 '연결 성공' 봇이 기본 담당자(To 없을 때의 폴백)
            leader_id = client.user.id
    if leader_id is None:
        raise RuntimeError("연결된 Organt 봇이 없습니다(토큰 확인). 로스터의 모든 봇이 연결 실패.")

    guide = DiscordGuide(system_client, organts)
    channel = (system_client.get_channel(cfg.channel_id)
               or await system_client.fetch_channel(cfg.channel_id))
    # 직업 기억 복원(Discord 역할 = 영속 진실원): 이전 실행에서 '예비'가 recruit로 받은 직군(예: 게임 기획자)은
    # Discord 역할로 남아 있다(서버에 영속 — 컨테이너 재시작/리클레임으로 디스크가 사라져도 견딤). 시작 시 '예비'
    # 봇의 커스텀 역할을 읽어 그 직군을 되살린다 → '게임 기획자'로 매번 다른 봇이 뽑히던 churn 차단(1봇 1직군).
    # (디스크 jobs.json은 Sys가 추가로 덮어쓴다 — 둘 다 영속 경로; 디스크가 우선, 없으면 Discord 역할로 유추.)
    try:
        spare_ids = [u for u in organts if str(bot_info.get(u, "")).startswith("예비")]
        recovered = await guide.get_member_jobs(channel.guild.id, spare_ids) if spare_ids else {}
        for uid, job in (recovered or {}).items():
            if uid in bot_info and job and not str(job).startswith("예비"):
                bot_info[uid] = job
        if recovered:
            log.info("직업 기억 복원(Discord 역할) %d명: %s", len(recovered),
                     {u: bot_info[u] for u in recovered if u in bot_info})
    except Exception:
        log.warning("직업 기억 복원(Discord 역할) 실패 — 건너뜀", exc_info=True)
    # 이름은 '사람 이름'(닉네임, 고정 정체성), 직군은 'Discord 역할(권한)'로 부여한다 — 한 봇이 직군을
    # 여러 개 가질 수 있고, 직군이 바뀌어도 이름은 안 바뀐다(사용자 요청). 둘 다 best-effort.
    names = assign_korean_names(list(organts))
    try:
        n_name = await guide.set_nicks(channel.guild.id, names)                 # 닉네임 = 사람 이름
        jobs = {u: r for u, r in bot_info.items() if not str(r).startswith("예비")}  # 예비는 직군 역할 없음
        n_role = await guide.assign_job_roles(channel.guild.id, jobs)           # 직군 = 역할(권한)
        log.info("이름 설정 %d/%d · 직군 역할 부여 %d/%d", n_name, len(names), n_role, len(jobs))
    except Exception:
        pass
    # 원터치 초대: 토큰은 있는데 아직 '서버에 없는' 봇은 클릭 한 번이면 합류하는 초대 링크를 띄운다
    # (봇 생성만 사람이 하고 초대는 링크 클릭으로 최소화). 이미 서버에 있으면 아무것도 안 띄움.
    try:
        miss = await guide.not_in_guild(channel.guild.id, list(organts))
        if miss:
            lines = "\n".join(f"• {bot_info.get(u, '?')}: {guide.invite_url(u)}" for u in miss)
            log.warning("서버 미초대 봇 %d명 — 초대 링크 안내", len(miss))
            await guide.post(cfg.channel_id, system_client.user.id,
                             f"[원터치 초대 필요] 아래 봇이 서버에 없습니다. 각 링크 클릭 한 번으로 합류시키세요:\n{lines}")
    except Exception:
        pass
    sysm = Sys(guide, channel.guild.id, _make_builder(cfg, audit, bot_info), bot_info=bot_info,
               workspace=cfg.workspace_dir,
               projects_path=str(cfg.audit_log_path.parent / "projects.json"),
               session_dir=str(cfg.audit_log_path.parent),
               jobs_path=str(cfg.audit_log_path.parent / "jobs.json"))
    sysm._save_jobs()   # Discord 역할에서 복원한 직군을 디스크 jobs.json에도 캐시(다음 시작은 디스크 빠른 경로)

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
            is_project = ch in sysm.projects        # 등록된 프로젝트 채널(전용 워크스페이스 보유)
            is_main = (ch == cfg.channel_id)
            # 도착 가시화(요청 미수신 진단): 봇이 본 모든 비-봇 메시지를 채널 정보와 함께 남긴다.
            log.info("메시지 도착: ch=%s (main=%s, project=%s) author=%s content=%r",
                     ch, is_main, is_project, message.author.id, (message.content or "")[:80])
            # 모든 채널을 연다(봇이 들어가 있는 채널이면 어디서든). 시작 조건: 메인·임의 채널은 '[Request] To: @봇'
            # 형식만, 등록된 '프로젝트 채널'은 평문도 '개입(이어서/수정)'으로 받는다(그 채널은 그 프로젝트 전용이라
            # 거기서 말하는 건 곧 그 프로젝트를 진행하는 것). 봇이 없는 채널은 on_message가 애초에 안 온다.
            req = parse(
                message_id=str(message.id),
                author_id=message.author.id,
                mention_ids=[m.id for m in message.mentions],
                reply_to_id=(message.reference.message_id if message.reference else None),
                content=message.content,
            )
            # 등록된 '프로젝트 채널'에선 평문도 '개입(이어서/수정)'으로 받는다(그 채널 = 그 프로젝트 전용 작업공간 —
            # 자연스러운 진행). 그 외(메인·임의 채널)는 '[Request] To: @봇' 형식만 흐름을 시작한다 — 봇이 들어가 있는
            # 아무 채널의 잡담이 작업을 트리거하지 않게(안전). 평문 트리거를 '등록 프로젝트 채널'로만 한정한 게 핵심.
            if not isinstance(req, Request):
                if is_project and (message.content or "").strip():
                    req = Request(to_id=None, kind=Kind.WORK, body=message.content.strip(),
                                  from_id=message.author.id, message_id=str(message.id))
                else:
                    log.info("  → 무시(메인·임의 채널은 '[Request] To: @봇' 형식만; 평문 개입은 등록 프로젝트 채널에서만). 받은 형식이 아님.")
                    return
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

    # 핫리로드: 실행 중 .env를 주기적으로 다시 읽어 '새로 떨군 토큰'을 자동 연결·합류시킨다(재시작 불필요).
    # 사람은 봇 생성+토큰을 .env에 넣기만 하면 되고, 연결·직군 닉네임·풀 합류·미초대 시 초대링크까지 자동.
    async def _watch_new_tokens():
        try:
            from dotenv import load_dotenv
        except Exception:
            return
        from .config import ROOT
        known = set()
        try:
            known = {tok for tok, _ in load_roster()}   # 시작 시 이미 연결된 토큰
        except Exception:
            pass
        while True:
            await asyncio.sleep(25)
            try:
                load_dotenv(ROOT / ".env", override=True)   # .env 다시 읽기(새 ORGANT_BOT_N 반영)
                for token, role in load_roster():
                    if token in known:
                        continue
                    known.add(token)
                    try:
                        client, task = await _connect(token)   # 워커: message_content 불필요
                    except Exception as e:
                        log.error("핫리로드 연결 실패 role=%s: %s", role, e)
                        continue
                    uid = client.user.id
                    organts[uid] = client
                    bot_info[uid] = role
                    sysm._roster_labels[uid] = role   # 흐름 시작 라벨 원복 대상에 포함(새 흐름에서 유지)
                    guide.register_organt(uid, client)
                    tasks.append(task)
                    try:
                        await guide.set_nick(channel.guild.id, uid,           # 이름=사람 이름
                                             KOREAN_NAMES[(len(organts) - 1) % len(KOREAN_NAMES)])
                        if not str(role).startswith("예비"):                  # 예비면 직군 역할 없음
                            await guide.assign_job_role(channel.guild.id, uid, role)
                    except Exception:
                        pass
                    if await guide.not_in_guild(channel.guild.id, [uid]):
                        await guide.post(cfg.channel_id, system_client.user.id,
                                         f"[원터치 초대] 새 봇 '{role}'을 서버에 추가하려면 클릭: "
                                         f"{guide.invite_url(uid)}")
                    log.info("핫리로드: %s(%s) 합류 — 현재 워커 %d명", role, uid, len(organts))
            except Exception:
                log.error("핫리로드 오류:\n%s", traceback.format_exc())

    tasks.append(asyncio.create_task(_watch_new_tokens()))
    await asyncio.gather(*tasks)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
