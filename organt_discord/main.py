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
import signal
import time
import traceback
from typing import Dict, List, Optional, Tuple

import discord
from claude_agent_sdk import HookMatcher

from organt_core.audit import AuditLog, make_post_tool_use_hook
from organt_core.builder import _make_builder
from organt_core.config import Config, load_config
from .discord_guide import DiscordGuide
from organt_core.guide_tools import COORD_TOOLS, FLOW_TOOLS, LEADER_TOOLS
from organt_core.organt import Organt, build_options, load_persona, pinned_cwd
from organt_core.permissions import make_pre_tool_use_hook
from organt_core.protocol import Kind, Request, Response, parse
from organt_core.sys_core import Sys

# 워커 공통 기본 도구: 파일(Read/Write/Edit)·탐색(Glob/Grep/ToolSearch)에 더해 WebSearch/WebFetch —
# '같은 종류의 훌륭한 예'를 상상이 아니라 실제로 찾아 대조하는 현실 기준 도구(RFC-011 M1). LLM은 자기
# 산출을 기준 삼아 '평범=충분'으로 수렴하므로(취향 천장 ~0.5), 외부 레퍼런스 검색이 '상용 수준'의 기준이 된다.


def load_roster() -> List[Tuple[str, str]]:
    """ORGANT_ROSTER → [(token, 역할), ...]. 첫 항목이 리더. 없으면 TEST_BOT 단독.

    형식: '토큰_환경변수명:역할' 을 ';' 로 구분. 역할은 '맨 도메인 정체성'만 적는다(누가 무엇을
    어떻게 할지·인터페이스·분배는 라벨에 박지 말 것 — 런타임 협의로 정해짐). 예:
      TEST_BOT_1:담당자; TEST_OBT_2:백엔드; TEST_OBT_3:프론트엔드; TEST_OBT_4:디자이너; TEST_OBT_5:QA
    """
    roster: List[Tuple[str, str]] = []
    seen_tokens = set()
    spec = os.environ.get("ORGANT_ROSTER", "").strip()
    if spec:
        sep = ";" if ";" in spec else ","
        for item in spec.split(sep):
            env_name, _, role = item.strip().partition(":")
            token = os.environ.get(env_name.strip(), "").strip()
            # 같은 토큰(=같은 봇)이 두 슬롯에 들어오면 첫 슬롯만 쓴다(로스터 순서=우선순위) — 예:
            # ORGANT_BOT_3와 TEST_OBT_2 폴백이 같은 봇일 때 이중 연결(유령 세션)·라벨 덮어쓰기 방지.
            if token and token not in seen_tokens:
                seen_tokens.add(token)
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


def assign_stable_names(ids, existing=None) -> Dict[int, str]:
    """봇 id들에 사람 이름(닉네임)을 배정하되, 서버에 '이미 닉네임이 있는 봇은 그 이름을 유지'한다.
    닉네임은 서버에 영속되므로(역할과 동일) 재시작·리클레임·로스터 변동에도 같은 봇은 같은 이름 —
    '연결 순서 인덱스' 배정이 주던 재시작마다 개명(정체성 흔들림)을 제거한다. 이름이 없는 봇만
    아직 안 쓰인 이름을 차례로 받는다."""
    existing = dict(existing or {})
    used = set(existing.values())
    fresh = (n for n in KOREAN_NAMES if n not in used)
    out: Dict[int, str] = {}
    for uid in ids:
        name = existing.get(uid) or next(fresh, None)
        if name is None:                       # 이름 풀(20명) 소진 — 번호를 붙여 충돌 없이 확장
            name = f"{KOREAN_NAMES[len(used) % len(KOREAN_NAMES)]}{len(used) // len(KOREAN_NAMES) + 1}"
        out[uid] = name
        used.add(name)
    return out


def find_pending_request(messages, known_ids) -> Optional[Request]:
    """채널 메시지(시간순)에서 '아직 [Response]가 안 달린 마지막 사용자 [Request]'를 찾는다.
    봇(known_ids)이 보낸 Request는 무시하고, Response가 하나라도 뒤따르면 해제(완료로 간주).
    부팅 복구가 메인·프로젝트 채널에 같은 판정을 쓰도록 분리한 순수 함수."""
    pending = None
    for m in messages:
        if isinstance(m, Request) and m.from_id not in known_ids:
            pending = m
        elif isinstance(m, Response):
            pending = None
    return pending


def graduated_project(projects: dict, message_id) -> Optional[dict]:
    """미응답 원요청(message_id)이 이미 '등록 프로젝트로 졸업'했으면 그 프로젝트를 돌려준다.
    부팅 복구의 라우팅 판정: 졸업한 원요청은 새 흐름으로 재발사하면 안 된다 — 그 흐름의 진행
    (전용 채널·작업공간·팀·미완 Task)이 이미 프로젝트에 영속돼 있으므로, 프로젝트 채널 '개입'
    (스코프 resume = 기억 유지)으로 이어가는 게 옳다(라이브: 동면 복구가 P-009 원요청을
    재발사해 새 스코프·새 임시 작업공간으로 처음부터 다시 시작 — 사용자 지적으로 교정)."""
    sid = str(message_id)
    for p in projects.values():
        if str(p.get("origin_msg") or "") == sid:
            return p
    return None


def projects_to_resume(projects: dict, already_channels: set, main_channel) -> list:
    """[복구 갭 보완 — 사용자 지적 2026-06-13] 미완 Task(open_task)가 남은 등록 프로젝트를 '이어서
    재개' 대상으로 돌려준다. 프로젝트 채널 평문 개입이 부분 처리된 채(마지막 메시지에 봇 응답이 달려
    find_pending_request가 '완료'로 보고 못 잡음) 동면하면, 졸업 라우팅(main 출신만 커버)에도 안 걸려
    영영 재개되지 않던 구멍 — 라이브: '게임성 고도화' 개입이 부팅 복구에서 누락(사용자가 수동 재전송).
    open_task는 Task 완료 시 None으로 비워지므로(체크포인트), 남아 있으면 '미완 작업 존재'의 신뢰
    신호다. 이미 복구 큐에 든 채널(졸업 라우팅 등 중복)·메인 채널은 제외한다(유령·이중 발사 방지)."""
    out = []
    for ch, p in (projects or {}).items():
        try:
            if int(ch) == int(main_channel) or int(ch) in already_channels:
                continue
        except (TypeError, ValueError):
            continue
        if isinstance(p, dict) and p.get("open_task"):
            # [좀비 부활 차단 — 라이브 P-019] 부팅마다 *모든* 미완 프로젝트를 되살리면, 사용자가 *돌아오지
            # 않은* 버려진 프로젝트(P-013: 후속 0건인데 미완 Task 존재)가 매 동면해동마다 되살아나 공유
            # 전문가(유일 AI 엔지니어)를 점유 → 정작 사용자가 지금 낸 요청(P-019)을 굶긴다('하나만 돌렸는데
            # 막힘'의 정체). '같은 미완 Task를 이미 한 번 자동 재개했는데 그 뒤로도 미완이면' 더는 자동
            # 재개하지 않는다 — 상태는 보존(체크포인트)되므로 사용자가 그 채널로 돌아오면 그때 재개된다.
            # 사용자가 그 프로젝트에 활동하면(피드백) recovery_attempted가 해제돼 다시 자동 재개 대상이
            # 된다(능동 반복 작업은 안 막고, 버려진 좀비만 멈춤 — 시간 임계값·우선순위 없음, 활동 신호로만).
            # [컨테이너 죽음 보존(2026-06-23, 사용자: P-031 정지)] 가드는 *유휴* 박제 좀비만 멈춘다 —
            # *진행 중 위임 체인(active_chain depth>0)*이 살아 있으면 컨테이너에 죽은 활성 작업이므로
            # 가드 무시하고 재개(한 부팅에 한 프로젝트만 깨우고 다른 활성 프로젝트가 정지하던 것 교정).
            ot = p.get("open_task") or {}
            # [수렴 경보 = 파킹(2026-06-23 S1a)] 사람 판정 대기(loop_escalated) Task는 자동 재개 안 함 —
            # 진행 중 체인(active_chain)이 있어도 무시하고 파킹(라이브 P-031: 워커가 15GB로 머신 전역 OOM →
            # 자동 재개가 스택 전체 위협). 사람이 채널에 글 쓰면(개입) 경보가 풀리고 정상 재개된다.
            if ot.get("loop_escalated"):
                continue
            if p.get("recovery_attempted") == ot.get("task_id") and not ot.get("active_chain"):
                continue
            out.append(p)
    return out


def resume_continue_body(body, last_work="") -> str:
    """[복구 이어가기 본문 — 사용자 지적 2026-06-14] 미완 Task(open_task)가 복원되는 복구에서 원요청을
    '새 요청'처럼 재처리하면, 리더가 복원된 미완 Task를 **섣불리 complete하고 새 Task를 여는** 사고가 난다
    (라이브: 054013-1 조기완료→074010-1 신설, "기존 안 끝났는데 새로 열림"). 그래서 재발사 본문 앞에
    '복원 Task를 이어서 완성, 새 Task·조기 complete 금지'를 명시한다. 원요청은 그대로 뒤에 보존(시스템이
    말을 지어내지 않되, 이어가기 맥락만 앞에 붙인다).
    [정밀 복구 2026-06-22] last_work(직전 owner 위임 원문)이 있으면 동봉 — 리더가 위임을 *재작문*(드리프트:
    라이브 5:13≠5:47)하지 말고 그 원문 그대로 다시 맡기게 한다. 완료잠금은 구조(owner_incomplete)가 강제하고,
    이 본문은 '재작문 금지·원문 replay'만 안내한다(말 지어내기 아님 — 원문 보존)."""
    replay = ""
    if (last_work or "").strip():
        replay = ("\n[직전 담당자에게 보냈던 위임 원문 — **새로 작문하지 말고** 같은 담당자에게 이 내용 그대로"
                  "(남은 부분 중심으로) 다시 Work로 맡기세요. 시스템도 이 원문으로 그 담당자를 자동 이어가기 "
                  f"합니다]\n{(last_work or '')[:1200]}\n")
    return ("[부팅 복구 — 이어가기] 직전 세션에서 이 요청으로 시작한 **미완 Task가 복원**됐습니다. **새 "
            "Task를 열지 말고, 복원된 그 Task를 이어서 완성**하세요 — 미완인데 섣불리 complete_task 하거나 "
            "새 create_task 하지 마세요(라이브 사고: 미완을 조기 완료하고 새로 엶). **단 '완성'을 당신이 혼자 "
            "떠안지 마세요 — 당신은 *오케스트레이터*입니다(라이브 P-024 교훈: 리더가 run 98회로 전체를 혼자 "
            "검증·디버깅하고 같은 Task를 7번 재마감한 독점)**: 남은 구현·수정은 그 도메인 owner에게 Work로 "
            "맡기고, 검증은 만든 사람 아닌 동료/QA에게 위임하세요 — 거의 다 됐어도 각자 자기 부분을 검증·인도하게 "
            "하고, 당신이 전체를 혼자 run으로 반복 검증·재마감하지 마세요. 원요청: " + (body or "") + replay)


async def _connect(token: str, message_content: bool = False,
                   members: bool = False) -> Tuple[discord.Client, asyncio.Task]:
    """봇 하나를 연결하고 on_ready까지 기다린다. 일시적 TLS/클럭 스큐 블립엔 재시도.

    message_content/members(특권 인텐트)는 System 봇만 True — 메시지 내용을 읽고, 길드 멤버
    (닉네임 풀·직군 역할)를 fetch_members로 조회해야 한다(포털에 둘 다 켜져 있음). Organt 봇은
    게시·타이핑만 하므로 불필요 → 새 봇이 개발자 포털에서 인텐트를 안 켜도 연결된다(봇 추가 간소화).
    """
    intents = discord.Intents.default()
    intents.message_content = message_content
    intents.members = members
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

    system_client, sys_task = await _connect(cfg.system_bot_token, message_content=True, members=True)
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
    # 닉네임도 역할처럼 '서버 영속 진실원': 기존 닉을 먼저 읽어 유지하고, 이름 없는 봇에만 새 이름을
    # 준다(연결 순서 인덱스 배정이 재시작·로스터 변동마다 같은 봇을 개명시키던 문제의 근본 해결).
    # 충돌 풀은 '길드 전체 봇'이다 — 로스터에 연결된 봇만 보면 오프라인/토큰 유실로 안 뜬 봇이
    # 이미 쓰는 이름을 새 봇에 중복 배정한다(예: testtest4의 '박지호'를 testtest에 또 주려던 버그).
    try:
        guild_nicks = await guide.get_guild_bot_nicks(channel.guild.id)
    except Exception:
        guild_nicks = None
    if guild_nicks is None:
        # 조회 '실패'를 '전원 무명'으로 오인하면 전면 개명으로 이름이 뒤섞인다(실측 사고) —
        # 이름은 서버에 이미 영속이므로, 풀을 못 읽은 부팅은 개명을 통째로 건너뛰는 게 안전.
        log.warning("길드 닉네임 풀 조회 실패 — 이번 부팅은 이름 배정을 건너뜀(기존 닉 유지)")
    else:
        names = assign_stable_names(list(organts), guild_nicks)
        try:
            to_set = {u: n for u, n in names.items() if guild_nicks.get(u) != n}   # 이미 맞는 닉은 안 건드림
            n_name = await guide.set_nicks(channel.guild.id, to_set)
            log.info("이름 설정 %d/%d(기존 유지 %d)", n_name, len(to_set), len(names) - len(to_set))
        except Exception:
            pass
    try:
        jobs = {u: r for u, r in bot_info.items() if not str(r).startswith("예비")}  # 예비는 직군 역할 없음
        n_role = await guide.assign_job_roles(channel.guild.id, jobs)           # 직군 = 역할(권한)
        log.info("직군 역할 부여 %d/%d", n_role, len(jobs))
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
    from organt_core.config import ROOT
    sysm = Sys(guide, channel.guild.id, _make_builder(cfg, audit, bot_info), bot_info=bot_info,
               workspace=cfg.workspace_dir,
               projects_path=str(cfg.audit_log_path.parent / "projects.json"),
               session_dir=str(cfg.audit_log_path.parent),
               jobs_path=str(cfg.audit_log_path.parent / "jobs.json"),
               seed_path=str(ROOT / "organt" / "projects.seed.json"))
    sysm._save_jobs()   # Discord 역할에서 복원한 직군을 디스크 jobs.json에도 캐시(다음 시작은 디스크 빠른 경로)
    # 레지스트리 reconcile(디스크 > 채널 토픽 > 시드): 리클레임으로 logs/가 사라져도 채널 토픽에서
    # 등록·리더 재지정을 복원한다(시드로 옛 리더가 원복되던 한계 해소). 직군의 Discord 역할 복원과 같은 원리.
    try:
        await sysm.reconcile_projects_from_discord()
    except Exception:
        log.warning("프로젝트 레지스트리 토픽 복원 실패 — 건너뜀", exc_info=True)
    if sysm.role_profiles:   # 직무 기준은 디스크(role_profiles.json)에서 Sys가 로드(리클레임 시 자가 재생)
        log.info("직무 기준 로드: %d개 직군", len(sysm.role_profiles))

    # [SIGTERM 우아한 종료 — 컨테이너 회수(~40분) 전 상태 flush(2026-06-23 전수감사)] 종전엔 SIGTERM 핸들러가
    # *전혀 없어* 컨테이너 킬(exit 143)이 라이브 flow의 미체크포인트 상태(act_by·진행)를 통째로 잃었고, 복구가
    # 그걸 0에서 재구성하던 churn(~30회/프로젝트)의 근원이었다. 죽기 직전 모든 라이브 flow를 즉시 checkpoint하면
    # 복구가 *최신* 상태에서 이어가 staleness·재작업이 사라진다. flush는 동기·빠름(projects.json 1회 쓰기)이라
    # 컨테이너 grace 안에 끝난다. 핵심 상태가 이미 영속됐으니 즉시 종료(run_listener/supervisor가 respawn).
    _shutting_down = {"on": False}

    def _graceful_shutdown(signame: str):
        if _shutting_down["on"]:
            return
        _shutting_down["on"] = True
        log.warning("%s 수신 — 라이브 flow %d개 상태 flush 후 종료", signame, len(sysm.active_flows))
        try:
            for scope, f in list(sysm.active_flows.items()):
                try:
                    sysm._checkpoint_open_task(f)
                except Exception:
                    log.error("종료 flush 실패(%s):\n%s", scope, traceback.format_exc())
            sysm._save_projects()
        except Exception:
            log.error("종료 flush 오류:\n%s", traceback.format_exc())
        os._exit(0)

    try:
        _sig_loop = asyncio.get_running_loop()
        for _sig in (signal.SIGTERM, signal.SIGINT):
            _sig_loop.add_signal_handler(_sig, lambda s=_sig: _graceful_shutdown(s.name))
    except (NotImplementedError, RuntimeError):
        log.warning("시그널 핸들러 설치 실패(플랫폼 미지원) — SIGTERM flush 불가")

    print(f"SYS 가동 — 리더={bot_info[leader_id]}({leader_id}), 팀={list(bot_info.values())}")
    print(f"#{channel.name} 에서 User 입력 대기 중 — 메인 채널은 '[Request] To: @봇' 형식, "
          f"등록 프로젝트 채널은 평문도 개입으로 받습니다(Ctrl+C 종료)")

    # 같은 메시지를 이 세션에서 두 번 처리하지 않는 가드(디스코드 재전달 등). 재시작 간 '완료 여부'는
    # 채널에 [Response]가 달렸는지로 판단한다(아래 부팅 복구) — 그래서 영속 dedup 파일은 쓰지 않는다.
    seen = set()

    # 게이트웨이 수신 카나리아(앵커 편집형): RESUME 후 '프로세스는 살았는데 수신만 죽는' 좀비 세션
    # 감지(라이브 관측: 사용자 메시지 유실). 하트비트/latency는 좀비에서도 정상이라 dispatch 수신만이
    # 유효한 신호다. 채널을 메시지로 채우지 않도록 **고정 앵커 메시지 1개를 주기적으로 edit**하고
    # 그 MESSAGE_UPDATE 수신(on_raw_message_edit)으로 생존을 확인한다 — 채널엔 안내문 1개만 영구
    # 존재(증가 0). 2주기 연속 미수신이면 자가 재기동(래퍼 부활 + 부팅 복구가 유실 요청 구조).
    _CANARY_TEXT = "[수신 점검] 이 채널은 시스템 수신 자가점검용입니다 — 메시지가 늘어나지 않습니다."
    canary = {"last_recv": time.monotonic(), "misses": 0, "ch": None, "anchor": None, "flip": False}
    try:
        canary["ch"] = await guide.get_or_create_channel(channel.guild.id, "sys-canary")
        await guide.hide_channel(channel.guild.id, canary["ch"])   # 사람 눈에서 숨김(@everyone 차단)
        anchor_ch = await guide._resolve(system_client, canary["ch"])
        async for _m in anchor_ch.history(limit=20):
            if _m.author.id == system_client.user.id and (_m.content or "").startswith("[수신 점검]"):
                canary["anchor"] = _m.id
                break
        if canary["anchor"] is None:
            canary["anchor"] = int(await guide.post(canary["ch"], system_client.user.id, _CANARY_TEXT))
    except Exception:
        log.warning("카나리아 채널 준비 실패 — 수신 감시 없이 가동")

    @system_client.event
    async def on_raw_message_edit(payload):
        if canary["ch"] is not None and getattr(payload, "channel_id", None) == canary["ch"]:
            canary["last_recv"] = time.monotonic()   # 앵커 edit 수신 = 게이트웨이 생존

    @system_client.event
    async def on_message(message):
        try:
            if canary["ch"] is not None and message.channel.id == canary["ch"]:
                return
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
                # 평문 트리거는 '등록 프로젝트 채널'만 — 메인(#test)·임의 채널은 [Request] 형식만.
                # (메인 평문 허용을 검토했으나 사용자 결정으로 제외: 잡담이 흐름을 오발사할 위험이 큼.)
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
            # [파일 전송 — 인바운드] 사용자가 첨부한 파일을 받아 흐름에 싣는다(작업공간 inbox/로 staging돼 봇이
            # Read/run으로 사용). 25MB 초과는 건너뛴다(Discord 한도). 첨부 없으면 no-op.
            for att in (getattr(message, "attachments", None) or []):
                try:
                    if int(getattr(att, "size", 0) or 0) > 25 * 1024 * 1024:
                        log.info("첨부 건너뜀(25MB 초과): %s (%sB)", att.filename, att.size)
                        continue
                    req.attachments.append((att.filename, await att.read()))
                    log.info("첨부 수신: %s (%sB)", att.filename, getattr(att, "size", "?"))
                except Exception as e:
                    log.error("첨부 다운로드 실패 %s: %s", getattr(att, "filename", "?"), e)
            audit.record("user_request", to=req.to_id, body=req.body[:200])
            log.info("요청 수신: to=%s body=%r", req.to_id, (req.body or '')[:60])
            # [RFC-011 M3 — 취향 축적] 등록 프로젝트 채널의 사용자 발화를 그 프로젝트에 누적(라우팅 전에 —
            # 이번 흐름의 set_goal도 최신 비평을 품질 기준으로 보게). 반복되는 불만이 곧 '상용 수준'의 앵커.
            if is_project:
                try:
                    sysm.record_user_feedback(ch, req.body)
                except Exception:
                    pass
            await sysm.route_channel_request(ch, req)   # 실제 채널 id로 라우팅
            log.info("요청 처리 완료: to=%s", req.to_id)
        except Exception:
            # 흐름 처리 중 어떤 예외도 리스너를 죽이지 않게 삼키고 전체 트레이스를 남긴다(조용한 죽음 방지).
            log.error("on_message 처리 중 예외:\n%s", traceback.format_exc())

    # 부팅 복구: 응답이 안 달린 [Request](중단됐거나 연결 직전 도착)는 다시 처리한다 — 리스너가 흐름
    # 도중 죽어도 재시작 시 그 요청을 마저 완료한다([Response]가 달린 요청은 완료로 보고 건너뜀).
    # 메인 채널만이 아니라 '등록된 프로젝트 채널'도 같은 판정으로 스캔한다 — 개입(프로젝트 채널 평문/
    # [Request]) 도중 재시작하면 그 요청이 통째로 유실되던 구멍을 메운다(라이브 관측: 사용자가 직접
    # 재전송해야 했음). 복수 채널의 미응답 요청은 순차로 처리(단일흐름 — SYS가 두 번째부터 큐잉).
    # ORGANT_SKIP_RECOVERY=1 이면 복구를 건너뛴다(깨끗한 슬레이트로 시작 — 이전 미응답 요청 재실행 안 함).
    known = set(organts) | {system_client.user.id}
    # 불리언 env 함정 방지: "0"/"false"/빈 값은 '복구 실행'으로 해석한다(문자열 "0"은 truthy).
    skip_recovery = os.environ.get("ORGANT_SKIP_RECOVERY", "") not in ("", "0", "false", "no")
    recover_channels = [cfg.channel_id] + [ch for ch in sysm.projects if ch != cfg.channel_id]
    pendings = []
    for ch in recover_channels:
        try:
            # 등록 프로젝트 채널은 '평문도 개입'이므로 평문까지 복구 후보로 읽는다(on_message와 동일 규칙).
            recent = await guide.read_thread(ch, limit=30, include_plain=(ch in sysm.projects))
        except Exception:
            continue                     # 사라진/접근 불가 채널은 건너뜀
        pending = find_pending_request(recent, known)
        if pending is None or str(pending.message_id) in seen:
            continue
        seen.add(str(pending.message_id))   # 재실행하든 안 하든 이후 on_message 중복은 막는다
        if skip_recovery:
            log.info("부팅 복구 건너뜀(ORGANT_SKIP_RECOVERY) ch=%s — 미응답 요청 재실행 안 함", ch)
            continue
        # [졸업 라우팅] 메인 채널의 미응답 원요청이 이미 등록 프로젝트로 졸업했으면, 원요청을
        # 새 흐름으로 재발사하지 않는다(진행을 버리고 처음부터 다시 — 라이브 P-009 사고).
        # 미완 Task가 영속돼 있으면 그 프로젝트 채널 '개입'(스코프 resume)으로 이어 마무리하고,
        # 미완 Task가 없으면(직전 개입이 마감까지 갔음) 아무것도 안 한다 — 자연 종결(재발사 유령 차단).
        if ch == cfg.channel_id:
            grad = graduated_project(sysm.projects, pending.message_id)
            if grad is not None:
                ot = grad.get("open_task") or {}
                # [좀비 재부활 차단 — 졸업 경로에도 동일 적용(라이브 P-021 '자꾸 부팅 복구')] 과거엔 이 경로가
                # recovery_attempted를 보지도, 박지도 않아서, 미완 Task가 완료 불가(예: 외부 배포 차단)일 때
                # *매 부팅마다 무한 재발사*됐다 — 그 흐름이 베턴/주의를 점유해 사용자의 새 메시지가 묻힌다.
                # projects_to_resume와 동일하게: ① 이미 자동 1회 재개됐고 사용자 활동이 없으면 재발사 안 함,
                # ② 처음이면 재발사하고 '자동 1회 재개됨'으로 표시(record_user_feedback이 사용자 활동 시 해제).
                # [컨테이너 죽음 보존(2026-06-23, 사용자: P-031 정지)] '이미 1회 재개됨' 가드는 *유휴*(active_chain
                # 없는) 박제 좀비만 멈춘다. *진행 중인 위임 체인(depth>0)*이 살아 있으면 — 이건 사용자가 버린 게
                # 아니라 컨테이너에 죽은 활성 작업이므로 — 가드를 무시하고 재개한다(한 부팅에 한 프로젝트만 깨우고
                # 다른 활성 프로젝트가 영영 정지하던 라이브 P-030/P-031 핑퐁 교정).
                if (ot and grad.get("recovery_attempted") == ot.get("task_id")
                        and not (ot.get("active_chain"))):
                    log.info("부팅 복구: 원요청이 %s로 졸업했으나 이미 자동 1회 재개됨(유휴) → 재발사 안 함(사용자 활동 시 재무장)",
                             grad.get("id"))
                elif ot:
                    log.info("부팅 복구: 원요청이 %s로 졸업 + 미완 Task 존재 → 프로젝트 채널 개입으로 이어가기",
                             grad.get("id"))
                    # 개입 본문은 사용자 원문을 보존하되(시스템이 말 지어내기 금지) 앞에 '이어가기'를 명시한다
                    # — 복원 노트만으론 리더가 원요청을 새 요청처럼 보고 복원 Task를 조기 완료·새 Task 신설하던
                    # 사고(라이브 054013-1→074010-1)를 막기 위해(resume_continue_body).
                    pendings.append((int(grad["channel"]), Request(
                        to_id=grad.get("leader"), kind=pending.kind or Kind.WORK,
                        body=resume_continue_body(pending.body),
                        from_id=pending.from_id, message_id=pending.message_id)))
                    grad["recovery_attempted"] = ot.get("task_id")
                    sysm._save_projects()
                else:
                    log.info("부팅 복구: 원요청이 %s로 졸업(미완 Task 없음) → 재발사 안 함", grad.get("id"))
                continue
        if pending.to_id is None:        # 프로젝트 채널이면 그 프로젝트의 등록 리더가 기본 담당
            pending.to_id = sysm.projects[ch]["leader"] if ch in sysm.projects else leader_id
        # [복구 충돌 교정 — 사용자 지적] 이 프로젝트에 미완 Task가 있으면 원요청을 '새 요청'으로 재처리하면
        # 리더가 복원 Task를 조기 완료하고 새 Task를 연다(라이브: 054013-1 조기완료→074010-1 신설). 본문에
        # '이어가기'를 명시해 그 사고를 막는다(원요청은 보존).
        if ch in sysm.projects and sysm.projects[ch].get("open_task"):
            _ot = sysm.projects[ch]["open_task"]
            # [정밀 복구] 가장 깊은 활성 위임(active_chain 끝)을 replay 원문으로 — 없으면 레벨1 owner 원문(fallback)
            _chain = _ot.get("active_chain") or []
            _replay = ((_chain[-1].get("body") if _chain else _ot.get("last_work_body", "")) or "")
            pending.body = resume_continue_body(pending.body, _replay)
        pendings.append((ch, pending))
    # [복구 갭 보완 — 사용자 지적] 위 스캔은 '미응답 마지막 [Request]'만 잡는다 — 프로젝트 채널 평문
    # 개입이 부분 처리(봇 응답 후 동면)되면 '완료'로 보여 누락된다. open_task가 남은 등록 프로젝트는
    # 그 채널 개입으로 이어 재개(졸업 라우팅의 open_task 이어가기를 프로젝트 채널 개입에도 동일 적용).
    if not skip_recovery:
        already = {int(c) for c, _ in pendings}
        for p in projects_to_resume(sysm.projects, already, cfg.channel_id):
            # 본문은 시스템 작문이 아니라 '프로젝트 존재 이유'(=사용자 원문, 등록 시 보존)로 — 이어가기
            # 맥락(새 Task 금지·복원 Task 잇기)은 open_task 복원이 담당(사용자: 시스템이 말 짓지 말 것).
            body = (p.get("purpose") or "").strip() or "이어서 미완 작업을 마저 진행"
            pendings.append((int(p["channel"]), Request(
                to_id=p.get("leader") or leader_id, kind=Kind.WORK, body=body,
                from_id=system_client.user.id,
                message_id="recover-open-%s" % (p.get("id") or p["channel"]))))
            log.info("부팅 복구: %s 미완 Task(open_task) 존재 → 프로젝트 채널 개입으로 이어가기", p.get("id"))
            # 이 미완 Task는 '자동 1회 재개됨'으로 표시 — 사용자 활동(피드백) 전엔 다음 부팅에서 재부활
            # 안 함(버려진 좀비가 공유 전문가를 영구 점유해 활성 요청을 굶기는 것 차단). 활동 시 해제.
            p["recovery_attempted"] = (p.get("open_task") or {}).get("task_id")
            sysm._save_projects()
    # [수렴 경보 = 파킹 — 자동 재개 금지(2026-06-23 S1a)] 사람 판정 대기(loop_escalated) Task는 컨테이너
    # 리클레임마다 워커를 다시 띄우지 않는다 — 사람이 ①수용/②방향제시할 때까지 영속 파킹한다. 라이브 P-031:
    # 워커가 15GB로 부풀어 머신을 전역 OOM(OOM킬러가 리스너·이웃 프로세스까지 위협)시키는 메모리폭탄이라,
    # 자동 재개가 곧 스택 전체 위협이었다(컨테이너죽음 보존 경로가 active_chain 핑계로 매 부팅 재발사). 모든
    # 복구 경로가 프로젝트 채널을 pendings에 싣으므로 여기 단일 지점에서 거른다. 사람이 채널에 글을 쓰면
    # (개입) handle_user_input이 경보를 풀고 정상 재개한다(자동 부팅복구만 막고 사람 개입은 막지 않는다).
    def _proj_of(_c):
        pj = sysm.projects
        return (pj.get(_c) or pj.get(str(_c))
                or (pj.get(int(_c)) if str(_c).lstrip("-").isdigit() else None))
    if pendings:
        _kept = []
        for _ch, _req in pendings:
            _p = _proj_of(_ch)
            if _p and (_p.get("open_task") or {}).get("loop_escalated"):
                log.info("부팅 복구 건너뜀(수렴 경보 파킹) ch=%s — 사람 판정 대기 Task는 자동 재개 안 함(사용자 개입 시 재개)", _ch)
                continue
            _kept.append((_ch, _req))
        pendings = _kept
    if pendings:
        async def _recover_one(ch, req):
            log.info("부팅 복구: 미응답 [Request] 재처리 ch=%s: %r", ch, (req.body or '')[:60])
            audit.record("user_request", to=req.to_id, body=(req.body or '')[:200])
            try:
                await sysm.route_channel_request(ch, req)
            except Exception:
                log.error("부팅 복구 처리 중 예외 ch=%s:\n%s", ch, traceback.format_exc())
        # [병렬 복구(2026-06-23, 사용자)] 종전 순차 await는 한 프로젝트 흐름(route_channel_request가
        # 리더+이어가기 루프를 끝까지 도는, 길 수 있는 작업)이 끝날 때까지 다른 미완 프로젝트의 재개를
        # 막아, 한 부팅에 한 프로젝트만 살아나고 나머지는 영영 정지했다(라이브 P-030/P-031: 번갈아 정지).
        # 각 프로젝트를 독립 태스크로 동시에 재개한다 — 정상 다중 프로젝트 운영과 동일(전역 점유 장부가
        # 봇 충돌을 조율). 한 프로젝트의 긴 흐름이 다른 프로젝트를 굶기지 않는다.
        async def _recover_all():
            await asyncio.gather(*[_recover_one(ch, req) for ch, req in pendings])
        asyncio.create_task(_recover_all())

    # 핫리로드: 실행 중 .env를 주기적으로 다시 읽어 '새로 떨군 토큰'을 자동 연결·합류시킨다(재시작 불필요).
    # 사람은 봇 생성+토큰을 .env에 넣기만 하면 되고, 연결·직군 닉네임·풀 합류·미초대 시 초대링크까지 자동.
    async def _watch_new_tokens():
        try:
            from dotenv import load_dotenv
        except Exception:
            return
        from organt_core.config import ROOT
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
                        nicks = await guide.get_guild_bot_nicks(channel.guild.id)   # 충돌 풀=길드 전체 봇
                        # 풀 조회 실패(None)면 배정 스킵(이름 뒤섞기 방지) — 닉이 이미 있으면 유지
                        if nicks is not None and uid not in nicks:
                            await guide.set_nick(channel.guild.id, uid,
                                                 assign_stable_names([uid], nicks)[uid])
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

    async def _gateway_canary():
        if canary["ch"] is None or canary["anchor"] is None:
            return
        period = int(os.environ.get("ORGANT_CANARY_PERIOD", "300"))
        while True:
            wall0 = time.time()
            await asyncio.sleep(period)
            # 컨테이너 일시정지(suspend) 감지: sleep 한 번에 벽시계가 주기의 3배 이상 점프했으면
            # 프로세스가 통째로 얼었다 깨어난 것 — 소켓·게이트웨이가 전부 죽어 있으므로 즉시 자가
            # 재기동한다(래퍼 부활 + 부팅 복구). 라이브 관측: 유휴 6시간 정지로 봇 전체가 침묵.
            if time.time() - wall0 > period * 3:
                log.error("컨테이너 일시정지 후 재개 감지(시계 점프 %.0fs) — 자가 재기동", time.time() - wall0)
                os._exit(43)
            try:
                # 앵커 1개를 edit(제로폭 토글로 내용 변화 보장) — 새 메시지를 만들지 않는다.
                canary["flip"] = not canary["flip"]
                await guide.edit_message(canary["ch"], canary["anchor"],
                                         _CANARY_TEXT + ("\u200b" if canary["flip"] else ""))
            except Exception:
                continue                      # 전송 실패 = 일반 네트워크 문제 — 수신 판정과 별개
            await asyncio.sleep(25)           # 게이트웨이 수신 전파 여유
            if time.monotonic() - canary["last_recv"] > period:
                canary["misses"] += 1
                log.error("게이트웨이 카나리아 미수신 %d회 — 수신 좀비 의심", canary["misses"])
                if canary["misses"] >= 2:
                    log.error("수신 좀비 확정 — 자가 재기동(래퍼가 되살리고 부팅 복구가 유실 요청을 구조)")
                    os._exit(43)
            else:
                canary["misses"] = 0

    async def _sleep_cycle():
        """수면(기억 증류): 경험이 충분히 쌓인 직군의 전문가를 깨워 '경험→직무 기준'으로 압축한다
        (자기계발 시간 보강 — Feature.md). [병렬] '시스템 전체 유휴'가 아니라 **그 전문가 봇이
        유휴**일 때 — 회사가 일하는 중에도 흐름에 묶이지 않은 직원은 자기계발한다(겹침은 전역
        점유 장부가 차단; 전체-유휴 조건이면 장기 프로젝트 중 증류가 영영 굶는다). 시도는 주기당
        한 직군만(비용 제어)."""
        period = int(os.environ.get("ORGANT_SLEEP_PERIOD", "600"))
        # [수면 사이클 비활성 토글(2026-06-23)] period<=0이면 증류 사이클을 아예 돌리지 않는다. 라이브:
        # 증류 워커가 *작업공간 루트*(cwd=cfg.workspace_dir, 32개 프로젝트·node_modules 누적 507MB)에서
        # 떠 CLI 시동 시 그 거대한 트리를 스캔→RSS 11GB로 부풀어 머신 전역 OOM(리스너 동반사망 위협)을
        # 매 주기 반복했다. 근본 교정(증류 워커에 빈 cwd 부여)이 들어가기 전까지는 끄는 게 안전하다.
        if period <= 0:
            log.info("수면(증류) 사이클 비활성(ORGANT_SLEEP_PERIOD<=0) — 증류 워커 루트 스캔 OOM 방지")
            return
        while True:
            await asyncio.sleep(period)
            try:
                for job in sysm.pick_distill_jobs():
                    mid = sysm._bot_of_job(job)
                    if mid is None or sysm.engaged.holder(mid) is not None:
                        continue                     # 그 전문가는 흐름 참여 중 → 다음 후보
                    await sysm.distill_role(job)
                    break
            except Exception:
                log.error("수면(증류) 사이클 오류:\n%s", traceback.format_exc())

    tasks.append(asyncio.create_task(_sleep_cycle()))
    tasks.append(asyncio.create_task(_gateway_canary()))
    tasks.append(asyncio.create_task(_watch_new_tokens()))
    # [cascade 사망 차단(2026-06-23 전수감사)] 종전 bare gather는 *한* 백그라운드 루프/봇 태스크가 던지면
    # 즉시 전파돼 run()이 끝나고 리스너 전체가 죽었다(supervisor와 동반사망의 한 경로). return_exceptions로
    # 한 태스크 실패가 나머지를 안 죽이게 하고, 무엇이 죽었는지 로깅한다.
    _results = await asyncio.gather(*tasks, return_exceptions=True)
    for _r in _results:
        if isinstance(_r, BaseException) and not isinstance(_r, asyncio.CancelledError):
            log.error("백그라운드 태스크 종료(예외):\n%s",
                      "".join(traceback.format_exception(type(_r), _r, _r.__traceback__)))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
