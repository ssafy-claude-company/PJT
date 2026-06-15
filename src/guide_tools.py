"""Organt가 쓰는 Guide 도구셋 (P2P Communication + 다중 Task + 팀 배정 모델).

회사식 인력 구조: **채용 풀(전체 로스터) → 프로젝트 팀(규모 산정해 배정) → Task 팀(필요 인원)**.
- 깨어난 Organt는 `request`로 *현재 Task 팀의 동료*에게 요청한다(Info=질문/Work=작업).
- 인원이 부족하면 `recruit`로 풀에서 현재 Task에 합류시킨다("더 필요하면 더 가져온다").
SYS가 대상 동료를 중첩 베턴으로 깨워(flow.wake) 응답을 돌려준다 → 항상 1명만 활성(단일흐름).

리더(첫 Organt)는 추가로:
- create_project(name, team): 규모를 산정해 프로젝트 팀 배정 + 전용 채널 생성
- create_task(purpose, goal, members): Task에 필요한 인원 배정 + 상태블록/Thread 생성(반복 가능)
- complete_task(result): 현재 Task를 완료로 마감
대화는 '현재 Task' 스레드에서. 보고는 별도 툴이 아니라 반환값(=Response)이 origin까지 unwind.
"""
import asyncio
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional

import anyio

from claude_agent_sdk import create_sdk_mcp_server, tool

from .communication import BusyInOtherFlow, CommError, CommunicationManager, RedoLimitExceeded
from .protocol import Kind, TaskStatus

_DEBUG = bool(os.environ.get("ORGANT_DEBUG"))


def _dbg(msg):
    """진단 로그(기본 off). ORGANT_DEBUG 설정 시에만 stdout으로."""
    if _DEBUG:
        print(msg, flush=True)


ORIGIN = 0
REQUEST_TOOL = "mcp__guide__request"
RECRUIT_TOOL = "mcp__guide__recruit"
RUN_TOOL = "mcp__guide__run"
# 모든 Organt 공통 흐름 도구(요청/채용/실행검증). 리더 전용 셋업 도구는 LEADER_TOOLS.
FLOW_TOOLS = [REQUEST_TOOL, RECRUIT_TOOL, RUN_TOOL]
# 리더(코디네이터) 흐름 도구: 조율만(run 없음) — 구현·실행은 owner/QA가 한다.
COORD_TOOLS = [REQUEST_TOOL, RECRUIT_TOOL]
LEADER_TOOLS = [f"mcp__guide__{n}" for n in
                ("create_project", "create_task", "set_goal", "complete_task", "deploy", "vote", "meet", "parallel_work")]

# run 툴 안전 차단: 파괴/탈출/저장소·시스템 경로/네트워크 외 명령은 막는다(npm·node·curl·python은 허용).
_RUN_DENY = ("rm -rf", "rm -r ", "sudo", "shutdown", "reboot", "mkfs", "dd if=", ":(){",
             "git ", "/home/user/pjt", "/etc/", "/usr/", "/root", "> /", "chmod ", "chown ",
             "pkill", "kill -9 1 ", "wget ", "ssh ", "scp ", "npm publish", "history")
# run으로 '파일 작성'(heredoc·cat>·tee)을 막는다 — 산출물 작성/수정은 Write/Edit로 해야 권한·협의
# 게이트(협의 중 선구현 금지)가 적용되고 '누가 무엇을 만들었나'가 기록된다. run은 실행·빌드·검증 전용.
# (이 백도어로 리더가 위임 없이 전부 혼자 작성해 독점하거나, 협의 단계 동료가 선구현하는 걸 차단.)
_RUN_AUTHOR = ("<<", "cat >", "cat>", "tee ", "tee\t")


def _resolve_members(spec, flow, allowed) -> List[int]:
    """'12, 백엔드A' 처럼 id 또는 역할명으로 동료를 지정 → allowed 안의 id 리스트(중복 제거)."""
    out: List[int] = []
    for tok in str(spec or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.lstrip("-").isdigit():
            v = int(tok)
            if v in allowed and v not in out:
                out.append(v)
        else:  # 역할명(부분일치)로도 지정 가능
            for i in allowed:
                if i not in out and tok.lower() in (flow._info(i) or "").lower():
                    out.append(i)
                    break
    return out


def _uniq(xs) -> List[int]:
    seen: List[int] = []
    for x in xs:
        if x not in seen:
            seen.append(x)
    return seen


def _looks_transient(text: str) -> bool:
    """동료 응답이 일시적 API 오류로 보이는지 — 그렇다면 답으로 취급하지 말고 재시도."""
    t = (text or "").strip().lower()
    return t.startswith("api error") or t.startswith("(동료 처리 중 오류")


# 채용 대기 인력(직군 미배정). recruit(role=…)로 런타임에 '게임 기획자·UX 디자이너' 등 필요한 직군으로
# 채용해 합류시킨다. 로스터에서 라벨이 '예비'인 봇들이며, 첫 '전원 기획'엔 안 들어가고 필요할 때 합류한다.
_SPARE_LABEL = "예비"


def _is_spare(flow, oid) -> bool:
    return (flow._info(oid) or "").strip().startswith(_SPARE_LABEL)


def _norm_job(name: str) -> str:
    return " ".join((name or "").split()).casefold()


# 겸직 라벨 구분자: '백엔드·QA' = 주직군 + 부직군. 겸직은 예외(예비 0명 또는 유사 직무)에서만,
# 봇당 최대 2개 — 더하기만 하던 시절의 '직군 5~6개 스택'(라이브 관측)으로 회귀하지 않기 위한 한도.
_JOB_SEP = "·"


def _jobs_of(label) -> List[str]:
    """라벨 → 보유 직군 목록('백엔드·QA' → ['백엔드','QA']). 단일 직군이면 1개짜리 리스트."""
    return [j.strip() for j in str(label or "").split(_JOB_SEP) if j.strip()]


def _job_tokens(name: str):
    return {t.casefold() for t in (name or "").split() if t}


def _free_alternatives(flow, me_id, to) -> str:
    """[전역 점유] 타 흐름에 점유된 to 대신 '지금 가용한 같은 직군 동료'와 채용 옵션을 안내문으로.
    재시도(폴링) 대신 구조적 선택지를 줘서, 점유 거부가 막다른 길이 아니라 분기점이 되게 한다."""
    eng, scope = flow.comm.engagement, flow.comm.scope
    jobs = {_norm_job(j) for j in _jobs_of(flow._info(to) or "")} - {""}
    alts = []
    for b in flow.pool:
        if b in (to, me_id) or _is_spare(flow, b):
            continue
        if jobs and not (jobs & {_norm_job(j) for j in _jobs_of(flow._info(b) or "")}):
            continue
        if eng is not None and scope is not None and eng.busy_elsewhere(b, scope):
            continue
        alts.append(f"{flow._info(b)}(id {b})")
    spares = [s for s in flow.pool if _is_spare(flow, s)]
    parts = []
    if alts:
        parts.append("지금 가용한 같은 직군 동료: " + ", ".join(alts[:4]))
    if spares:
        parts.append(f"또는 recruit(role=…)로 예비 {len(spares)}명 중 채용")
    return ("; ".join(parts) if parts else
            "지금은 같은 직군의 가용 동료가 없습니다 — 다른 직군 동료로 진행 가능한 부분을 먼저 하거나, "
            "불가하면 그 사정을 보고에 남기세요")


def _ckpt(flow):
    """[크래시-세이프 Task 체크포인트] Task 전이(생성·목표확정·owner 확정·마감)마다 미완 Task를
    레지스트리에 영속한다 — 종전엔 흐름 '종료'에만 써서, 동면·강제종료처럼 마감 코드가 못 도는
    죽음이면 진행 중 Task의 정체(블록·스레드·owner·Goal)가 유실돼 복구가 '같은 Task 이어가기'가
    아니라 '새 Task'로 시작했다(라이브 관측 — 사용자 지적). 콜백은 SYS가 주입(미주입이면 무해)."""
    fn = getattr(flow, "checkpoint_task", None)
    if fn:
        try:
            fn()
        except Exception:
            pass


async def _fork_collect(flow, me_id, members, body_of, kind=Kind.INFO):
    """[병렬 Info fork-join] '독립 의견 수집'(표결·회의 1라운드)을 동시에 돈다 — Communication.md
    13–14행("여럿(병렬)은 이 제약을 완화하는 Feature로 둔다")의 구현. 완화는 정확히 이 구간뿐:
    - 가지(branch)는 comm 프레임을 열지 않는다 → 가지 봇은 '활성'이 아니므로 request가 규약
      에러로 자연 차단된다(가지의 중첩 요청 금지가 프롬프트가 아니라 구조로 강제 — 답만 한다).
    - 회사 풀 관점은 전역 점유로 일관: 수집 동안 가지 봇은 점유돼 타 흐름이 못 집어가고, 끝나면
      즉시 풀로 돌아간다. 타 흐름 점유/이 흐름에서 위임 보유 중인 멤버는 건너뛴다(부분 조인 —
      일부 멤버 때문에 수집 전체가 막히지 않는다).
    - 행 안전: 각 가지는 워커 침묵 워치독이 종결을 보장 → 조인이 영원히 안 닫히는 일이 구조적으로
      없다. 동시 폭은 ORGANT_FORK_FAN(기본 3)으로 묶는다(토큰 속도 운영 노브, 1이면 직렬과 동일).
    kind: 가지의 작업 종류 — Info(의견 수집, 기본)면 훅이 가지의 선구현(Write/Edit)을 종전대로
    차단한다(flow.fork_kind로 프레임 없는 가지에 게이트 연결; Work 가지는 휴면 — 호출부 없음).
    수집 동안 flow.fork_active를 올려 신규 요청/중첩 수집을 [대기]로 막는다 — CLI가 같은 턴에
    병렬 도구 호출을 내도(vote+request 등) 가지와 같은 동료를 이중으로 깨우는 일이 구조적으로 없다.
    반환: 멤버 순서 보존 [(member, res|None, 제외/실패 사유)]."""
    eng, scope = flow.comm.engagement, flow.comm.scope
    sem = asyncio.Semaphore(max(1, int(os.environ.get("ORGANT_FORK_FAN", "3"))))

    async def _branch(m):
        if flow.comm.is_busy(m):
            return (m, None, "(이 흐름에서 진행 중인 위임 보유 — 이번 수집에서 제외)")
        if eng is not None and scope is not None and eng.busy_elsewhere(m, scope):
            return (m, None, f"(타 흐름({eng.holder(m)}) 참여 중 — 이번 수집에서 제외)")
        if eng is not None and scope is not None:
            eng.engage(m, scope)
        flow.fork_kind[m] = kind
        try:
            async with sem:
                return (m, await flow.wake(m, body_of(m), kind), "")
        except Exception as e:
            return (m, None, f"(수집 실패: {e})")
        finally:
            flow.fork_kind.pop(m, None)
            if eng is not None and scope is not None and not flow.comm.is_busy(m):
                eng.release(m, scope)

    flow.fork_active = getattr(flow, "fork_active", 0) + 1
    try:
        return list(await asyncio.gather(*(_branch(m) for m in members)))
    finally:
        flow.fork_active -= 1


def _find_variant_job(name: str, existing) -> Optional[str]:
    """기존 직군과 '이름은 다른데 토큰을 공유'하면 변형(중복 생성) 의심으로 그 기존 직군을 돌려준다.
    recruit가 자유 텍스트 직군명을 받다 보니 흐름마다 'VFX 전문가'/'VFX 아티스트' 같은 변형이 새 역할로
    계속 불어났다(중복 생성 오류의 뿌리). 무엇이 '정답 이름'인지는 시스템이 정하지 않는다(하드코딩 금지)
    — 같은 이름(공백·대소문자 무시)은 기존 역할 재사용이라 통과시키고, 변형만 멈춰 세워 에이전트가
    '재사용'인지 '진짜 새 직군'인지 명시하게 한다."""
    mine_n, mine_t = _norm_job(name), _job_tokens(name)
    if not mine_t:
        return None
    if any(_norm_job(ex) == mine_n for ex in existing):
        return None                        # 같은 이름이 이미 있음 → 그대로 재사용(변형 아님), 즉시 통과
    for ex in sorted(existing):            # 정렬: 같은 입력엔 같은 안내(메시지 결정성)
        if mine_t & _job_tokens(ex):
            return ex
    return None


# 협의로 '인정되는' Info인지 — 순수 응답확인 핑('응답 가능하신가요?')은 합의로 치지 않는다(빈 핑 차단).
# 짧은데 핑 문구가 거의 전부일 때만 비실질(긴 메시지는 핑 문구가 섞여도 실질로 본다).
_HOLLOW_PING = ("응답 가능", "응답가능", "응답 되시", "응답되시", "계신가요", "준비되셨", "들리시",
                "확인 가능하신", "ready?", "available?", "are you there", "are you available")


def _is_substantive(body: str) -> bool:
    b = (body or "").strip()
    if not b:
        return False
    low = b.lower()
    return not (len(b) <= 30 and any(h in low for h in _HOLLOW_PING))


def _reap_pgroup(pgid: int):
    """프로세스그룹 pgid에 남은 프로세스를 모두 종료한다(백그라운드 서버 누수 차단).
    셸을 self-session으로 띄우면 모든 자손이 pgid==셸pid를 공유한다. 다만 리더(셸)가
    먼저 끝나 reap되면 '고아 프로세스그룹'이 돼 killpg가 안 먹으므로, /proc를 훑어
    pgid가 같은 잔여 프로세스를 PID로 직접 SIGKILL한다(이게 run 간 포트충돌의 구조적 해결)."""
    try:
        os.killpg(pgid, signal.SIGKILL)   # 리더 생존 시 빠른 경로
    except (ProcessLookupError, PermissionError, OSError):
        pass
    me = os.getpid()
    try:
        entries = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return
    for d in entries:
        pid = int(d)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                data = f.read()
            # stat: 'pid (comm) state ppid pgrp ...' → comm의 마지막 ')' 뒤 3번째가 pgrp
            if int(data[data.rindex(b")") + 1:].split()[2]) == pgid:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError, IndexError):
            continue


@dataclass
class TaskRef:
    """채널에 누적되는 Task 하나 (상태블록 + 대화 Thread + 배정 팀 + 단일 책임자)."""
    task_id: str
    thread_id: str
    block_id: str
    status: TaskStatus
    team: List[int] = field(default_factory=list)   # 이 Task에 배정된 Organt들
    owner: int = 0                                   # 이 산출물의 단일 책임자(accountable)
    participated: set = field(default_factory=set)   # 이 Task 정의에 '실질 협의'로 참여한 동료(보낸/받은 쪽 모두)
    owner_incomplete: bool = False                   # owner가 '턴 한도'로 미완 반환 → 완료 차단(이어서 끝내야)
    owner_delivered: bool = False                    # owner가 '검증된 실작업 산출물'을 위임 도중 실제로 내고 응답이 돌아왔나
                                                     #   → 거짓이면 complete_task 거부(owner 미응답·착수전인데 리더가 대신 허위완료 차단)
    verified: bool = False                           # run으로 한 번이라도 실행됐나(실행 0회 완료 차단)
    work_delegated: int = 0                          # 리더가 이 Task에서 보낸 Work 위임 수(0이면 '자문만 받고 독식' 의심)
    collab_notes: str = ""                           # 회의·표결 합의 기록 — Work 위임에 자동 동봉(스펙이 회의에서 증발하던 결함 방지)
    cross_checks: int = 0                            # owner 인도 후 '다른 멤버'의 검증 참여 수(0이면 complete 1회 보류 — 품질 판정 독점 방지)
    complete_retry: bool = False                     # (구) 1회 보류 시절 잔재 — 교차 검증 의무 하드화(Rule/Task 6)로 미사용, 호환 위해 유지
    leader_writes: int = 0                           # 리더가 이 Task에서 직접 쓴 파일 수(위임 없이 독식하면 차단)
    contrib_checked: bool = False                    # 팀 기여 의무 게이트(RFC-009) 1회 통과 여부 — 부른 직군이 실작업·검증 0(회의 발언만)이면 1회 보류 후 재호출 통과
    run_count: int = 0                               # 이 Task의 run 실행 횟수(체리픽 노출용)
    evidence: str = ""                               # 시스템이 직접 캡처한 마지막 run 영수증(허위보고 차단)


class Flow:
    """하나의 활성 흐름(단일흐름 보존). 풀→프로젝트 팀→Task 팀으로 인력을 구조적으로 배정."""

    def __init__(self, guide, channel_id, guild_id, leader_id, bot_info=None):
        self.guide = guide
        self.user_channel = channel_id
        self.guild_id = guild_id
        self.leader = leader_id
        self.bot_info = bot_info or {}
        self.comm = CommunicationManager(ORIGIN)
        self.pool = list((bot_info or {}).keys()) or [leader_id]   # 채용 가능 전체(로스터)
        if leader_id not in self.pool:
            self.pool.insert(0, leader_id)
        # 기본 프로젝트 팀 = 직군 보유자(예비 제외) — 예비는 '채용 대기'라 기본 팀에 안 넣는다(recruit로만 합류).
        # 리더는 예비여도 포함. 담당자가 create_project/create_task로 더 좁히거나 recruit로 직군을 채운다.
        self.project_team: List[int] = [m for m in self.pool if m == leader_id
                                        or not str((bot_info or {}).get(m, "")).startswith("예비")]
        self.project_channel: Optional[int] = None
        self.tasks: List[TaskRef] = []
        self.current: Optional[TaskRef] = None
        self._base = time.strftime("%H%M%S")
        self._n = 0
        self.done = False
        self.final: Optional[str] = None
        self.root_id: Optional[str] = None
        self.advice = []
        self.workspace = None   # run 툴 cwd(작업공간 경로). SYS가 주입.
        self.wake = None   # async (to_id, body, kind) -> result text  (SYS가 주입)
        self.register_project = None   # (channel_id, name) -> project_id (SYS 주입)
        self.project_id = None         # [Project-XXXX] 식별번호
        self.intervention = None       # 기존 프로젝트 개입이면 그 정보(dict)
        self.deployed = None           # deploy 툴이 불리면 결과 문자열(배포 강제용 추적)
        self.pending_clarify = None    # 위임자에게 되묻기(확인요청 반환) 임시 보관
        self.leader_segment = 0        # 리더 턴 세그먼트 번호(시작=1, continue마다 +1) — 관측용
        self.req_results = {}          # (seg,from,to,kind,body)->응답: 같은 턴 병렬 중복요청 합치기용 캐시
        self.act_count = 0             # 작업공간 변경(run/Write/Edit) 누계 — 훅이 +1. '위임 도중 owner가 실제로
                                       #   일했나'를 wake 전후 스냅샷 차이로 판정(허위완료/독점 차단)
        self.act_by = {}               # 행위자별 작업 누계(actor→count) — 요청자 자신의 활동을 빼고 재기 위함
        self.consec_fail = 0           # 연속 '응답 실패(무응답/타임아웃)' 횟수 — 시스템 일시불안정 판별(충원 루프 차단)
        self.inflight_tasks = set()    # 진행 중 위임의 '완주 태스크'들 — CLI가 도구 호출을 포기해도 위임은
                                       #   계속 완주하며(중첩 가능), SYS가 이어가기 전에 이들의 완주를 기다린다
        self.detached_results = []     # 포기당한(detached) 위임의 완주 결과 — 이어가기 리더에게 전달
        self.write_lease = {}          # 행위자→샌드박스(쓰기 리스, 휴면 인프라): 훅이 리스 밖 Write/Edit 거부
        self.fork_kind = {}            # [fork 수집] 행위자→Kind: 프레임 없는 가지에도 선구현 게이트 적용
        self.fork_active = 0           # [fork 동시성 가드] 수집 진행 수 — 수집 중 신규 요청/수집은 [대기]
        self.log = None                # (event, **fields) 콜백 — SYS가 주입(flow.jsonl 영속)

    def start_root(self, root_id):
        self.root_id = str(root_id)
        self.comm.request(ORIGIN, self.leader, root_id, Kind.WORK)

    def next_task_id(self) -> str:
        self._n += 1
        return f"{self._base}-{self._n}"

    async def refresh(self, task: Optional[TaskRef] = None):
        t = task or self.current
        if t and self.project_channel and t.block_id:
            await self.guide.update_status(self.project_channel, t.block_id, t.status)

    def _info(self, oid):
        return self.bot_info.get(oid, "")

    def _names(self, ids):
        return [self._info(i) or str(i) for i in ids]


def deploy_service_name(flow, arg_name: str = "") -> str:
    """배포 서비스명 결정 — [멀티 프로젝트] 등록 프로젝트는 식별번호(P-번호)로 **결정적으로**
    정한다: 같은 프로젝트는 늘 같은 서비스, 다른 프로젝트는 다른 서비스. 미등록 흐름은 슬롯이
    **없다**("") — 배포 신원은 프로젝트가 보증한다(사용자 설계 확인 2026-06-12: 배포는
    프로젝트마다. 과거의 DEPLOY_NAME env 폴백은 미등록 배포를 공유 슬롯(P-002 라이브 겸용
    todo-organt-demo)으로 보내 덮어쓰기 위험을 남겼었다). 에이전트 임의 명명(arg_name)은
    등록·미등록 어디서도 슬롯이 되지 못한다(작명 사고 차단)."""
    pname = getattr(flow, "project_name", None)
    pid = getattr(flow, "project_id", None)
    if pid:
        # [신원=번호] 등록 프로젝트의 슬롯은 무조건 식별번호 — 이름 슬러그를 쓰면 일반명사 이름이
        # 충돌할 때 다른 작품이 같은 슬롯을 덮어쓴다(라이브: 지진·모션이 대기질 슬롯을 연쇄 점유).
        return f"organt-{str(pid).lower()}"
    if pname:
        slug = re.sub(r"[^a-z0-9-]", "-", str(pname).lower()).strip("-")[:40]
        if slug:
            return f"organt-{slug}"
    return ""


def _ok(text):
    return {"content": [{"type": "text", "text": text}]}


def _group_of(flow, team):
    return [(f"<@{i}>", flow._info(i)) for i in team]


async def _react(g, channel_id, message_id, emoji):
    """이모지 반응(상태 표시). Guide에 react가 없으면(테스트 등) 조용히 건너뜀."""
    fn = getattr(g, "react", None)
    if fn:
        await fn(channel_id, message_id, emoji)


async def _add_members(g, thread_id, member_ids):
    """Task 스레드에 팀원 추가(멤버십=팀). Guide에 메서드 없으면 건너뜀."""
    fn = getattr(g, "add_thread_members", None)
    if fn:
        await fn(thread_id, member_ids)


def _speech_clip(s, n=1500) -> str:
    """발언 안전망: 폭주만 막고 **침묵 절단하지 않는다** — 잘리면 잘렸다고 표기한다.
    종전의 하드컷([:300]/[:400])은 '3~5줄' 지시를 지킨 발언(한국어 200~400자+)까지 단어
    중간에서 잘랐다(라이브: 회의 발언 전원이 307~308자로 박제, "…프론트엔"에서 끊김 — 사용자
    관측). 더 나쁜 건 회의록도 잘려 **다음 발언자들이 서로의 잘린 주장을 보고 토론**한 것 —
    분량 통제는 지시(프롬프트)와 모델 판단의 몫이고, 시스템은 안전망만 친다."""
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + f" …(발언 {len(s)}자 — {n}자 안전망에서 잘림)"


def make_guide_tools(flow: Flow, me_id: int, role: str):
    g = flow.guide
    tools = []

    async def _say(who, text):
        """회의·표결 발언을 '그 봇 본인 명의'로 스레드에 남긴다 — 4명의 독립 의견이 리더 명의
        [안내] 묶음으로 게시돼 '중앙 공지'처럼 보이던 착시(사용자 관측) 제거. 협업의 실체와
        가시성을 일치시킨다. 실패는 조용히(가시화는 best-effort, 흐름은 안 멈춤)."""
        try:
            if flow.current:
                await g.post(int(flow.current.thread_id), who, text)
        except Exception:
            pass

    @tool("request", "현재 Task 팀의 동료 한 명에게 요청(kind: Info=질문 / Work=작업, to_id 문자열)",
          {"to_id": str, "kind": str, "body": str})
    async def request(args):
        to = int(args["to_id"])
        kind = Kind.WORK if str(args["kind"]).strip().lower().startswith("w") else Kind.INFO
        body = args["body"]
        tag = f"[REQ] {me_id}({flow._info(me_id)})→{to}({flow._info(to)}) {getattr(kind, 'value', kind)}"
        if flow.current is None:
            _dbg(f"{tag} ✗거부:Task없음")
            return _ok("오류: 진행 중인 Task가 없습니다. (리더가 create_task 먼저 여세요.)")
        # 직군 미배정(예비) 봇에게는 위임/질의 불가 — 말로 '너는 X야' 하고 일을 시키는 걸 구조적으로 막는다.
        # 먼저 recruit(role='직군')로 실제 직군을 부여해야 그 봇이 일할 수 있다(말로만 배정 차단).
        if _is_spare(flow, to):
            _dbg(f"{tag} ✗거부:직군 미배정(예비)")
            return _ok(f"요청 거부: {flow._info(to) or to}는 아직 직군 미배정('예비')입니다 — 말로 직군을 정하지 말고 "
                       f"recruit(member='{to}', role='직군명')으로 직군을 실제로 부여한 뒤 요청하세요(직군이 부여돼야 일을 맡길 수 있음).")
        # 위임자에게 되묻기(확인요청 반환): 직속 위임자에게 Info로 물으면 '재진입 불가' 에러 대신
        # 베턴을 위임자에게 질문과 함께 돌려준다 — 위임자가 답하고 그 일을 다시 맡긴다(협업 가능).
        if kind == Kind.INFO and to == flow.comm.direct_delegator(me_id) and to != me_id:
            flow.pending_clarify = {"from": me_id, "to": to, "q": body}
            flow.comm.history.append(("clarify", me_id, to, "pending", Kind.INFO))
            _dbg(f"{tag} ↩확인요청→위임자")
            return _ok(f"확인요청을 직속 위임자({flow._info(to)})에게 전달했습니다. 지금 이 턴을 즉시 "
                       f"마치고(추가 도구 호출·추측 진행 금지) 짧게 반환하세요 — 위임자가 답한 뒤 이 작업을 "
                       f"당신에게 다시 맡깁니다.")
        if to not in flow.current.team:
            if to in flow.project_team:
                # 프로젝트 팀원이면 이 Task에 자동 합류 — Task 내 관련 인원을 최소화할 이유는 없다.
                flow.current.team.append(to)
                flow.current.status.group = _group_of(flow, flow.current.team)
                await flow.refresh()
                _dbg(f"{tag} +Task자동합류(프로젝트팀원)")
            elif to in flow.pool:
                # [원인 교정 — 정보가 있는 거부] 리더가 회사 풀(전체 로스터)과 프로젝트 팀을 혼동해
                # 팀 밖 동료를 반복 호출하던 라이브 관측(7회 우회, SIGTERM 기억구멍이 증폭)의 뿌리:
                # 거부가 '안 된다'만 말하고 '그 직군이 팀에 누구인지'를 안 알려줘 같은 실수가 반복됐다.
                # 올바른 대안(팀 내 같은 직군)과 현재 팀 명단을 동봉해 첫 거부에서 바로 교정되게 한다.
                same = [m for m in flow.project_team
                        if m != me_id and not _is_spare(flow, m)
                        and ({_norm_job(j) for j in _jobs_of(flow._info(to) or "")}
                             & {_norm_job(j) for j in _jobs_of(flow._info(m) or "")})]
                alt = (" 같은 직군의 **팀 내 동료**: "
                       + ", ".join(f"{flow._info(m)}(id {m})" for m in same)
                       + " — 이들에게 요청하세요(재시도 금지)." if same else
                       " 팀에 그 직군이 없습니다 — 정말 필요하면 recruit(member=…, role=…)로 합류시킨 뒤 요청하세요.")
                _dbg(f"{tag} ✗거부:프로젝트밖")
                return _ok(f"요청 거부: {to}({flow._info(to)})는 이 프로젝트 팀이 아닙니다 — 회사 풀에는 "
                           f"있지만 이 프로젝트 구성원이 아닙니다(팀은 create_project 때 당신이 구성했습니다)."
                           f"{alt} 현재 프로젝트 팀: {flow._names(flow.project_team)}")
            else:
                return _ok(f"요청 거부: {to}는 채용 풀에 없습니다. 풀: {flow._names(flow.pool)}")
        if flow.wake is None:
            return _ok("오류: 시스템 준비 안 됨")
        # 직렬화: 베턴이 내 차례가 될 때까지 대기(거부 아님). 서로 다른 동료로의 병렬 요청은 순차 처리되며,
        # 첫 요청이 길게(중첩 협의·긴 구현) 걸려도 베턴은 결국 돌아오므로 위임이 끊기지 않는다. 데드라인은
        # 교착 안전장치 — 게임처럼 한 동료가 10분+ 작업하는 경우까지 넉넉히(1시간) 둬 '활성=동료' 반려가
        # 안 뜨게 한다(이전 600초는 긴 작업 중 병렬요청이 타임아웃돼 무서운 '거부' 노이즈를 냈다).
        # 직전 위임이 detach 상태로 완주 중이면(도구 호출은 포기됐지만 위임은 계속) 새 요청을 길게
        # 재우지 않고 즉시 안내한다 — 리더가 '보류' 헛돌이 대신 턴을 마치게(시스템이 완주 후 다시 깨움).
        if (any(not t.done() for t in getattr(flow, "inflight_tasks", ()))
                and flow.comm.alive != me_id and not flow.comm.done):
            return _ok("[대기] 직전 위임이 아직 진행 중입니다 — 추가 요청을 보내지 말고 이 턴을 간결히 "
                       "마치세요. 위임이 완료되면 시스템이 그 결과와 함께 당신을 다시 깨웁니다.")
        # [fork 동시성 가드] 의견 수집(표결·회의 1R)이 도는 동안엔 새 요청을 보내지 않는다 — fork 중엔
        # 베턴(alive)이 리더에 머물러, CLI가 같은 턴에 병렬 도구 호출(vote+request)을 내면 수집 가지와
        # 같은 동료를 이중으로 깨워 '같은 봇 두 턴'(세션 충돌)이 될 수 있다(직렬 vote 시절엔 alive 이동이
        # 자연 차단). 수집은 조인이 보장돼 짧으므로 대기 안내가 정답.
        if getattr(flow, "fork_active", 0) > 0:
            return _ok("[대기] 의견 수집(표결/회의)이 진행 중입니다 — 수집 결과를 받은 뒤 요청하세요.")
        deadline = time.monotonic() + 3600
        while flow.comm.alive != me_id and not flow.comm.done and time.monotonic() < deadline:
            await anyio.sleep(0.05)
        # 같은 턴에 '같은 동료에게 같은 요청'을 다발로 보낸 병렬 중복은 합친다(idempotent): 동료를 다시
        # 깨우지 않고 직전 응답을 그대로 재사용한다 → 반사적 중복 wake 차단(직렬화는 유지, 중복만 제거).
        dupkey = (flow.leader_segment, me_id, to, str(getattr(kind, "value", kind)), body)
        if dupkey in flow.req_results:
            if flow.log:
                flow.log("dup_parallel_merged", frm=me_id, to=to,
                         kind=str(getattr(kind, "value", kind)), seg=flow.leader_segment)
            _dbg(f"{tag} ⇉병렬중복 합침(동료 재호출 없이 같은 응답 재사용)")
            return _ok(f"[{to} 응답] {_speech_clip(flow.req_results[dupkey], 4000)}\n"
                       f"(같은 턴에 이미 보낸 동일 요청 — 동료를 다시 호출하지 않고 같은 응답을 재사용)")
        # 대기 한도까지 베턴이 안 돌아옴(동료가 비정상적으로 오래 작업) — 규약위반이 아니므로 무서운 '거부'
        # 안내를 사용자에게 띄우지 않고 조용히 '보류'로 소프트 반환(리더는 응답 받은 뒤 다시 시도).
        if flow.comm.alive != me_id and not flow.comm.done:
            _dbg(f"{tag} ⏸보류:대기 한도 초과(활성={flow.comm.alive})")
            return _ok(f"[보류] {flow._info(to) or to}가 아직 작업 중이라 지금은 보내지 않았습니다 — 그 동료의 "
                       f"응답을 받은 뒤 다시 요청하세요(오류 아님).")
        # 검증→점유는 await 없이 인접 실행 → 형제 요청과 경합하지 않음(원자적).
        try:
            flow.comm.check_request(me_id, to, kind)
        except BusyInOtherFlow as e:
            # [전역 점유] 규약 위반이 아니라 '그 동료가 지금 다른 흐름에서 일하는 중' — 무서운 '거부'
            # 대신 가용 대안(같은 직군 동료·채용)을 안내한다. 같은 동료 재시도(폴링)는 금지 문구로 차단.
            if flow.log:
                flow.log("req_busy_elsewhere", frm=me_id, to=to, holder=str(e.holder_scope or ""),
                         kind=str(getattr(kind, "value", kind)), seg=flow.leader_segment)
            _dbg(f"{tag} ⏸점유:타 흐름({e.holder_scope})")
            return _ok(f"[동료 점유] {flow._info(to) or to}는 지금 다른 흐름({e.holder_scope})에서 일하는 "
                       f"중입니다 — 같은 동료에게 재시도하며 기다리지 마세요(폴링 금지). "
                       f"{_free_alternatives(flow, me_id, to)}.")
        except CommError as e:
            if flow.log:   # 관측: 거부 시점의 베턴 상태(alive)·요청자를 영속 기록 → 원인 규명
                flow.log("req_rejected", frm=me_id, to=to, kind=str(getattr(kind, "value", kind)),
                         alive=flow.comm.alive, seg=flow.leader_segment, reason=str(e)[:70])
            _dbg(f"{tag} ✗거부:규약 ({e})")
            return _ok(f"요청 거부(규약): {e}")
        # Work 위임은 Goal 확정 뒤에만 — '목표 합의(set_goal) → 분배' 순서를 구조적으로 강제(선분배 금지).
        # Info(합의용)는 언제든 허용 → Goal을 정하는 논의 자체는 막지 않는다.
        goal = (flow.current.status.goal or "").strip()
        if kind == Kind.WORK and not goal:
            _dbg(f"{tag} ✗거부:Goal미확정")
            return _ok("Work 위임 거부: 이 Task의 Goal이 아직 확정되지 않았습니다. 먼저 동료와 request(Info)로 "
                       "목표를 합의하고 set_goal로 확정한 뒤 Work로 맡기세요(목표는 팀 합의의 산물 — 선분배 금지).")
        # Work Response → Accept/Redo (docs Communication.md §5). 이미 이 owner가 '완료 응답'까지 낸
        # 산출물을 같은 위임자가 또 Work로 보내면, 그건 '새 위임'이 아니라 직전 산출물의 Redo다.
        # → 새 프레임이 아니라 redo()로 처리한다(한계까지만, 초과 시 반복 위임 거부). 이로써 '되풀이
        #   위임'이 구조적으로 '직전 결함을 고치는 보완'으로만 성립한다(반사적 중복요청 차단·정당한 보완 허용).
        is_redo = kind == Kind.WORK and flow.comm.delivered_work(me_id, to)
        owner_body = body
        if is_redo:
            try:
                frame = flow.comm.redo(me_id, to, "pending")    # 베턴 점유 + Redo 카운트(한계 시 RedoLimitExceeded)
            except RedoLimitExceeded:
                _dbg(f"{tag} ✗재위임 한도초과")
                # [품질>토큰 — 리더 셀프 마무리 권유 제거] 종전 안내("직접 Write/Edit로 마무리")는
                # Redo 실패의 끝에서 중앙집권·비전문 마감을 권하는 셈이었다(탈중앙·전문화 역행).
                return _ok(f"재위임 거부(Redo 한도 초과): {to}({flow._info(to)})는 이미 이 산출물을 여러 번 "
                           f"보완했습니다. 같은 사람에게 같은 식으로 또 떠넘기지 마세요 — 품질 경로는: "
                           f"① 검증자(타 멤버)의 결함 보고로 **무엇이 왜 미달인지 정밀화**해 마지막 1회를 명확히 맡기거나 "
                           f"② 같은 직군의 **다른 전문가**(없으면 recruit)에게 결함 보고와 함께 맡기거나 "
                           f"③ goal이 이미 충족이면 complete_task, 끝내 미달이면 사용자에게 정직하게 보고하세요"
                           f"(리더가 비전문 직접 마무리로 덮지 말 것).")
            owner_body = (f"[보완 요청(Redo) — 직전 산출물이 목표에 못 미쳐 되돌아왔습니다] 고칠 구체적 결함: {body}\n"
                          f"[이 Task의 Goal] {goal}\n결함만 정확히 고치고 run으로 재검증해 그 증거와 함께 보고하세요.")
        else:
            frame = flow.comm.request(me_id, to, "pending", kind)   # 베턴 점유(alive→to)
            if kind == Kind.WORK:
                # 위임의 '계약'은 리더가 매번 새로 쓰는 스펙이 아니라 팀 합의로 확정된 Goal이다(스펙 리파인
                # 루프=재요청의 뿌리를 끊는다). owner가 그 목표를 끝까지(구현+검증) 책임진다.
                owner_body = (f"[위임 — 이 목표를 끝까지 책임지는 owner는 당신입니다] 이 Task의 Goal: {goal}\n"
                              f"직접 구현하고 run으로 '목표가 충족됨'을 검증한 뒤(리더에게 되넘기지 말 것), "
                              f"그 실행 증거와 함께 간결히 보고하세요.\n"
                              f"큰 목표는 **수직 슬라이스 우선**: '끝까지 관통하는 최소 동작 버전'을 먼저 만들어 "
                              f"검증하고 그 위에 살을 붙이세요 — 마지막 통합 몰빵 금지(오차를 일찍 드러내는 것이 "
                              f"빠른 길입니다. RFC-005: 검증 신호는 연속적이어야 한다).\n"
                              f"보고는 다음 골격으로(보고 계약 — 받은 쪽이 산출물을 재탐색하지 않아도 되게): "
                              f"[결과] 한 줄 결론(완료/부분/실패) / [변경] 파일·핵심 변경 목록 / "
                              f"[검증] 방법→결과 / [리스크] 남은 것·주의점.\n"
                              f"단, 이 Goal에 **당신 직군의 전문성으로 만드는 게 아닌 범주**가 섞여 있으면 — "
                              f"코드로 흉내낼 수 있다고 당신 일인 게 아닙니다('할 수 있다'와 '그 분야 전문성으로 "
                              f"잘한다'는 다릅니다 — 비전문 자급은 placeholder일 뿐) — 어설프게 떠안지 말고 보고 "
                              f"**첫 줄**에 `[직군밖] 필요직군명` 을 적어 반려하세요. 리더가 그 직군을 채용하거나 "
                              f"실제 제작 자원으로 충족합니다(전문화 원칙: '구현 가능'이 아니라 '전문성 정합'으로 판단).\n"
                              f"[요청 맥락] {body}")
                notes = getattr(flow.current, "collab_notes", "")
                if notes:
                    # [스펙 증발 방지] 회의·표결의 합의는 리더 머릿속이 아니라 위임 계약에 실린다 —
                    # 라이브 P-009: 9직군이 회의로 정한 스펙(상태머신·SLA·타이밍 계약)이 구현자에게
                    # 전달되지 않아(스코프 단절·리더 요약 의존) 결과물 품질로 이어지지 못함.
                    owner_body += f"\n[팀 협의 기록(회의·표결) — 구현·검증 시 이 합의를 준수]\n{_speech_clip(notes, 6000)}"   # 저장 한도(6000)와 일치 — 전달에서 합의가 또 잘리지 않게(품질>토큰)
                # [RFC-008 P0 — 검증 위임에 루브릭 자동 주입] owner 인도 후 '다른 멤버'에게 가는 Work =
                # 검증 위임 → owner 산출물 도메인의 직무 기준을 루브릭으로 동봉. 라이브 P-010 1차에서 루브릭이
                # complete_task 거부 메시지에만 있어 0회 발동(검증이 카운트되면 게이트를 안 탐) — 검증자에게
                # 직접 주입해야 'owner 도메인 기준 채점'이 실제로 일어난다. '돌아가는가'가 아니라 '충분한가'.
                if (getattr(flow.current, "owner_delivered", False) and flow.current.owner
                        and to != flow.current.owner and callable(getattr(flow, "craft_of", None))):
                    owner_job = (flow._info(flow.current.owner) or "").strip()
                    rub = [flow.craft_of(j) for j in owner_job.split("·") if j.strip()]
                    rub = [r for r in rub if r]
                    if rub:
                        # [발견2 완화] owner 인도 후 타 멤버 Work가 '검증'인지 '후속 구현'인지 구조로 완벽히
                        # 구분 불가(의도의 문제) — 메시지가 양쪽을 다 커버해 오발동을 무해화한다: 검증 위임이면
                        # 채점, 후속 구현이면 같은 기준을 '참고'(통합 시 품질 인식). 어느 쪽이든 owner 도메인
                        # 기준이 주입되는 건 손해가 아니다('충분한가'의 눈을 공유).
                        owner_body += (f"\n[산출물 품질 기준 — '{owner_job}' 도메인. 이 요청이 **검증**이면 산출물을 "
                                       f"'사용자처럼 실제로 사용·플레이'하며 아래 각 항목을 충족/미달로 채점하고 미달은 "
                                       f"구체적 결함으로 보고하세요(돌아가는가 아니라 '충분한가'). 이 요청이 **후속 "
                                       f"구현/통합**이면 아래 기준을 참고해 같은 품질 수준을 맞추세요:\n"
                                       + _speech_clip("\n---\n".join(rub), 2500))
        thread_id = flow.current.thread_id
        # Owner = 그 일을 Work로 받은 동료(수신=소유). 선배정이 아니라 요청으로 owner가 떠오른다 —
        # 이 Task에 아직 owner가 없을 때 첫 Work-request 수신자가 책임자가 된다(중앙집권 방지).
        if kind == Kind.WORK and not flow.current.owner:
            flow.current.owner = to
            flow.current.status.owner = flow._info(to) or f"<@{to}>"
            await flow.refresh(flow.current)
            _ckpt(flow)                       # 크래시-세이프: owner 확정 영속(복구 때 같은 담당이 잇게)
        req = await g.send_request(thread_id, me_id, to, kind, body)
        frame.request_id = str(req)                              # 실제 메시지 id로 기록 갱신
        if kind == Kind.WORK and me_id == flow.leader and flow.current:
            flow.current.work_delegated += 1   # 리더의 구현 위임 카운트 — 0이면 '자문만 받고 독식'(권한 훅이 차단)
        _dbg(f"{tag} ✓전송 req={req}{' (Redo)' if is_redo else ''}")
        if flow.log:   # 관측: 모든 요청을 '보낸 순서'대로 영속 기록(중첩 PostToolUse 타이밍에 안 묻힘)
            flow.log("req_sent", frm=me_id, to=to, kind=str(getattr(kind, "value", kind)),
                     seg=flow.leader_segment, redo=is_redo, body=body[:60])
        # Task 정의 '실질 협의' 참여 기록 — 보낸 쪽·받은 쪽 모두(누가 물었든: 리더든 peer든). 빈 핑은 제외.
        # → set_goal 게이트가 'peer 협의도 합의로 인정'하고 '빈 핑은 불인정'하게 만든다(허브 완화·실질 강제).
        if kind == Kind.INFO and flow.current and _is_substantive(body):
            for x in (me_id, to):
                if x in flow.current.team and x != flow.leader:
                    flow.current.participated.add(x)
        # ── 위임 완주 보장(detach-safe) ─────────────────────────────────────────
        # 여기서부터의 '깨우기→응답 처리→프레임 close'는 별도 태스크(_deliver)로 돌고, 도구 호출
        # 자체는 shield로 감싼다. CLI가 (자체 한도 등으로) 이 도구 호출을 포기·취소해도 위임은
        # 끝까지 완주하고 규약(베턴·게이트·기록)이 일관되게 닫힌다 — 라이브 관측: 위임 포기가
        # '이중 활성'(리더+사슬 동시 작업)과 리더의 '비동기 작업 중' 오인을 만들던 결함의 차단.
        # 완주 결과는 flow.detached_results로 남아 SYS가 이어가기 리더에게 전달한다.
        detached = {"on": False}

        async def _deliver():
            runs_before = flow.current.run_count if flow.current else 0
            acts_before = flow.act_count   # 위임 도중 owner(단일흐름이라 깨운 동료만 활성)가 실제로 일했는지 측정
            mine_before = flow.act_by.get(me_id, 0) if getattr(flow, "act_by", None) is not None else 0
            try:
                result = await flow.wake(to, owner_body, kind)      # 동료 깨워 응답(중첩 베턴)
                if _looks_transient(result):                        # 일시 오류면 한 번 더(답으로 취급 X)
                    result = await flow.wake(to, owner_body, kind)
            except Exception as e:
                result = f"(동료 처리 중 오류: {e})"
            # 깨운 동료가 '나(위임자)에게 확인요청'을 남기고 턴을 마쳤으면, 그 질문을 응답으로 표면화 →
            # 내가 답을 정해 다시 맡긴다(되묻기가 에러가 아니라 협업으로 흐름). 이는 '완료'가 아니므로
            # delivered로 기록하지 않는다(되묻기 후 재위임은 Redo가 아니라 '첫 구현').
            was_clarify = False
            if (flow.pending_clarify and flow.pending_clarify.get("to") == me_id
                    and flow.pending_clarify.get("from") == to):
                q = flow.pending_clarify["q"]
                flow.pending_clarify = None
                was_clarify = True
                result = (f"[확인요청 from {flow._info(to)}] {q}\n"
                          f"(→ 답을 정한 뒤, 이 작업을 {flow._info(to)}에게 request(Work)로 다시 맡기세요)")
            failed = _looks_transient(result)
            # [직군밖 반려 — 전문화의 구조 채널] 도메인 적합성은 시스템이 키워드로 판정하지 않는다 —
            # 그 분야 전문가(수신 owner)가 판정한다(자기정의 원칙). owner가 첫 줄에 '[직군밖] 필요직군'
            # 을 적으면: 실패도 미완도 아닌 '올바른 반려'로 분류하고, 소유를 해제하며, 리더에게 채용을
            # 구조적으로 지시한다 — 관계없는 직군이 일을 흡수해 어설픈 산출물을 내던 경로(라이브:
            # ML이 백엔드에 묶여 감)의 차단.
            refused_m = re.match(r"^\s*\[직군밖\]\s*([^\n]*)", result or "")
            refused = bool(kind == Kind.WORK and not was_clarify and not failed and refused_m)
            if refused and flow.current is not None and flow.current.owner == to:
                flow.current.owner = 0                 # 소유 해제 — 채용된 전문가가 새 owner가 되게
                flow.current.status.owner = ""
                flow.current.owner_incomplete = False
                _ckpt(flow)
            # owner가 '위임 도중 실제로 일했나' — 단일흐름이라 깨운 동료(+그 하위)만 활성이므로 wake 전후
            # act_count(run/Write/Edit) 증가 = owner 작업. 거짓이면 owner는 깨어났지만 착수 전/계획만 하고
            # 곧장 반환한 것(허위완료의 씨앗). 이걸로 '검증된 인도'와 '빈 응답'을 가른다.
            # '요청자 자신'의 활동(detach 뒤 리더가 모델 쪽에서 돌린 폴링 run 등)은 빼고 잰다 —
            # 위임 측정창의 인도 신호(owner_acted)가 이중 활성 잔재로 오염되지 않게(허위완료 차단 정확성).
            mine_delta = (flow.act_by.get(me_id, 0) - mine_before) if getattr(flow, "act_by", None) is not None else 0
            owner_acted = (flow.act_count - acts_before) > mine_delta
            # 진짜 행(무활동)으로 끊긴 인프라 타임아웃인데 owner가 그 전에 실제로 작업을 했다면, 한 작업은
            # 작업공간에 남아 있다 → '실패'로 끝내 유실시키지 말고 '이어가기'(미완)로 처리한다. (하트비트
            # 타임아웃이 일하는 워커는 안 자르므로 드문 경우지만, 안전망으로 작업 유실·허위완료를 막는다.)
            infra_timeout = (kind == Kind.WORK and not was_clarify
                             and "api error: timeout" in (result or "").lower())
            resumable_timeout = infra_timeout and owner_acted
            # 동료가 'turn 한도'로 미완 반환했나(Work) — 그러면 이 Task는 완료로 못 닫고(complete_task 거부),
            # 같은 owner에게 '이어서(continuation)' 재위임해 끝내야 한다(허위완료→다음 Task churn 차단). 미완은
            # delivered(accept)로 안 쳐서 respond 마커를 'incomplete'로 두면, 재위임이 Redo 한도에 안 걸린다
            # (이어가기는 '직전 결함 보완'이 아니라 '남은 작업 마저 하기'이므로 횟수 제한 없이 계속 가능).
            incomplete = (kind == Kind.WORK and not was_clarify and not failed and not refused
                          and "턴 한도 도달" in (result or "")) or resumable_timeout
            # 미완 게이트(owner_incomplete)는 '의미 있는 신호'로만 갱신한다: 미완 신호면 True, owner가
            # '실작업을 담은 정상 응답'으로 마무리하면 False(이어가기 완료 = 게이트 자동 해제). 크래시(failed)
            # ·실작업 없는 응답은 완료의 증거가 아니므로 직전 상태를 유지한다 — 타임아웃 미완이 후속 크래시/
            # 빈 응답으로 풀려 미완인 채 complete가 통과되는 구멍 차단.
            if kind == Kind.WORK and not was_clarify and flow.current:
                if incomplete:
                    flow.current.owner_incomplete = True
                elif not failed and owner_acted:
                    flow.current.owner_incomplete = False
            is_owner_work = (kind == Kind.WORK and not was_clarify and not failed and not incomplete
                             and not refused
                             and flow.current is not None and to == flow.current.owner)
            # owner가 Work를 받고도 실작업(run/Write) 0회로 곧장 반환 = 착수 전/계획만 = '인도 아님'.
            premature = is_owner_work and not owner_acted
            if premature and flow.current is not None:
                # 미착수도 '구조적 미완'이다 — 마커를 세워 complete를 막고, 리더 세그먼트가 여기서
                # 끝나도 SYS 자동 이어가기가 같은 owner를 다시 깨운다(판단이 아니라 기계적 행동).
                flow.current.owner_incomplete = True
            if is_owner_work and owner_acted and _is_substantive(result):
                flow.current.owner_delivered = True   # 이 owner가 실작업+응답을 냈다 → complete_task 허용 근거
            try:
                await g.send_response(thread_id, to, req, result)
                await _react(g, thread_id, req, "⚠️" if failed else "✅")  # 상태=이모지(해소/실패)
                _dbg(f"{tag} {'⚠실패' if failed else ('…미완' if (incomplete or premature) else '✓응답')} len={len(result)}")
            finally:
                # 프레임 close = 베턴 복귀(누수 방지). 정상이면 alive==to 라 그대로 닫힌다. 미완·미착수(premature)는
                # 'accept'로 안 쳐서 delivered로 기록 안 함 → 같은 owner 재위임이 Redo 한도에 안 걸리고 '실제 첫 인도'로 성립.
                # 크래시(failed)도 'accept'가 아니다 — 인프라 실패가 '완료 인도'로 기록되면 직후 재요청이
                # Redo(보완)로 둔갑해 한도를 태우고 owner에게 '직전 산출물 결함' 프레임으로 잘못 전달된다.
                try:
                    flow.comm.respond(to, "clarify" if was_clarify else
                                      ("refused" if refused else
                                       "incomplete" if (incomplete or premature) else
                                       "failed" if failed else "accept"), result)
                except CommError:
                    # to의 중첩 하위요청이 응답 없이 끝나(크래시/이탈) 베턴이 to에 '굳은' 비정상 상황 →
                    # me_id(요청자)가 다시 alive 될 때까지 위 프레임을 강제 close. 흐름 교착(굳음) 방지.
                    if flow.log:
                        flow.log("baton_recover", me=me_id, stuck_alive=flow.comm.alive, to=to)
                    guard = 0
                    # origin 프레임(스택 마지막 1장)은 여기서 닫지 않는다 — 핸들러 레벨 복구가
                    # 흐름 자체를 종료시키면 안 됨(origin 마감은 SYS의 _close_flow 책임). detach로
                    # 프레임 순서가 어긋난 최악 타이밍에 흐름이 통째로 드레인되던 위험 차단.
                    while (not flow.comm.done and flow.comm.alive != me_id
                           and len(flow.comm.open_requests) > 1 and guard < 30):
                        flow.comm.escalate("베턴 굳음 안전복구")
                        guard += 1
            if failed:
                if resumable_timeout:
                    # owner가 작업을 진행하다 '무활동'으로 끊긴 경우 — 한 작업은 작업공간에 보존돼 있다.
                    # 실패로 끝내지 말고 같은 owner에게 '이어서' 재위임(연속). owner_incomplete=True라 complete는
                    # 막히고, 프레임 마커가 incomplete라 redo 한도와 무관하게 계속 이어갈 수 있다(유실·허위완료 동시 차단).
                    if flow.log:
                        flow.log("owner_resumable_timeout", to=to, seg=getattr(flow, "leader_segment", 0))
                    return _ok(f"[{flow._info(to)}] 작업을 진행하던 중 일시 무응답으로 끊겼습니다 — 한 작업은 "
                               f"작업공간에 보존돼 있습니다. **같은 담당자에게 request(Work)로 '이어서 남은 부분을 "
                               f"마저 끝내라'**고 다시 맡기세요(이어가기 — 횟수 제한 없음). 다른 사람으로 바꾸거나 "
                               f"새로 뽑지 마세요(같은 환경이라 같은 문제).")
                # 구조적 사실: 단일흐름은 한 번에 한 명만 일한다 → 요청자는 그 동료가 끝날 때까지 '블록'된다.
                # 따라서 여기서의 '실패'는 그 동료가 느리거나 불응한 게 아니라 그 동료의 LLM 서브프로세스가
                # '크래시'(SIGTERM/143·연결끊김·과부하)한 것 — 즉 인프라/환경 문제다. 새 사람으로 바꾸거나
                # 충원하면 '같은 환경'에서 똑같이 크래시한다(이게 '백엔드 6명' 루프의 뿌리). 그래서 실패엔
                # '재배정·채용'을 절대 권하지 않는다 — 같은 동료 1회 재시도(블립 회복용) 또는 사용자 보고만.
                flow.consec_fail = getattr(flow, "consec_fail", 0) + 1
                if flow.log:
                    flow.log("req_failed", to=to, consec=flow.consec_fail, seg=flow.leader_segment)
                if flow.consec_fail >= 2:
                    return _ok(f"[{to}] 또 실패 — **연속 {flow.consec_fail}회**. 이건 그 동료가 아니라 **환경(인프라) 일시 "
                               f"불안정**입니다(단일흐름이라 한 명만 도는데 그 서브프로세스가 크래시한 것). **새로 뽑거나 "
                               f"다른 사람으로 바꾸지 마세요 — 같은 환경이라 똑같이 실패합니다.** 진행 상황을 사용자에게 "
                               f"'환경 불안정으로 일시 중단'이라 보고하고 멈추세요(무한 재시도·충원 금지).")
                return _ok(f"[{to}] 응답 실패. 단일흐름에선 한 명만 일하므로 이건 그 동료 탓이 아니라 거의 항상 **인프라/일시 "
                           f"오류(서브프로세스 크래시)**입니다 — **다른 사람으로 바꾸거나 새로 뽑지 마세요(같은 환경이라 똑같이 "
                           f"실패).** 같은 동료에게 한 번만 다시 요청해보고(블립이면 회복), 또 실패하면 사용자에게 보고하고 멈추세요.")
            flow.consec_fail = 0   # 정상 응답 → 연속 실패 카운터 리셋(일시 블립 회복)
            if refused:
                need = (refused_m.group(1) or "").strip() or "해당 전문 직군"
                if flow.log:
                    flow.log("work_refused_offdomain", to=to, need=need[:30], seg=flow.leader_segment)
                return _ok(f"[직군밖 반려] {flow._info(to) or to}가 이 일을 **자기 직군 밖**으로 판정했습니다 — "
                           f"필요 직군: {need}.\n**recruit(role='{need}')로 예비를 채용해 그 전문가에게 Work로 "
                           f"맡기세요** — 같은 동료나 관계없는 직군에 다시 떠넘기지 마세요(이 반려는 실패가 아니라 "
                           f"올바른 전문화 신호입니다. 소유는 해제됐고, 채용된 전문가가 새 owner가 됩니다).\n"
                           f"--- 반려 보고 원문 ---\n{_speech_clip(result, 1500)}")
            # owner가 깨어났지만 '실작업 없이'(run/Write/Edit 0회) 곧장 반환 = 아직 착수 전/계획만. 리더가 대신
            # 구현·완료하지 말 것(독점·허위완료의 정확한 진입점). 같은 owner에게 다시 맡겨 '검증된 산출물'을 받게
            # 안내한다. 이 응답은 캐시하지 않는다 → 같은 턴에 재위임해도 합쳐지지 않고 실제로 다시 깨운다.
            if premature:
                _dbg(f"{tag} ⚠owner 미착수(실작업 0)")
                if flow.log:
                    flow.log("owner_no_work", to=to, seg=flow.leader_segment)
                return _ok(f"[{to} 응답] {_speech_clip(result, 1500)}\n\n[중요] {flow._info(to) or to}가 아직 산출물을 만들지 "
                           f"않았습니다(run/파일작성 0회 — 착수 전이거나 계획만). **당신이 대신 구현하거나 이 Task를 "
                           f"완료하지 마세요(독점·허위완료 금지).** 같은 owner에게 request(Work)로 다시 맡겨 'run으로 "
                           f"검증한 실제 산출물'을 받은 뒤 진행하세요. 정말 끝까지 무응답이면 recruit/재배정으로.")
            # 위임 응답엔 owner가 '직접 돌린 실행 증거(시스템 캡처)'를 붙여 돌려준다 — 위임자가 말이 아니라
            # 증거로 '검증 후 수락'할 수 있게(반사적 재요청 대신). owner가 이번에 run을 돌렸을 때만.
            receipt = ""
            if (kind == Kind.WORK and not was_clarify and flow.current
                    and flow.current.run_count > runs_before and flow.current.evidence):
                receipt = f"\n[owner 실행 증거(시스템 캡처)] {_speech_clip(flow.current.evidence, 1000)}"
            # [발견1 교정 2026-06-13] 검증 대상 산출물이 '존재'하면(owner 위임 인도 OR 리더가 직접
            # 구현=leader_writes>0) 그 후 타 멤버 응답을 교차 검증 참여로 센다 — 리더 독식 Task(owner==0)도
            # 제3자 검증 대상('누가 만들었든 제3자 검증'은 보편 이치). 종전엔 owner_delivered만 봐서 리더
            # 독식이 검증 면제되던 구멍.
            product_ready = (flow.current.owner_delivered
                             or (not flow.current.owner and getattr(flow.current, "leader_writes", 0) > 0))
            if flow.current and product_ready and to != flow.current.owner:
                flow.current.cross_checks += 1
            flow.req_results[dupkey] = result   # 같은 턴 병렬 중복요청이 재사용할 응답 캐시(동료 재호출 방지)
            return _ok(f"[{to} 응답] {_speech_clip(result, 4000)}{receipt}")


        async def _deliver_tracked():
            payload = await _deliver()
            if detached["on"]:
                try:
                    txt = payload["content"][0]["text"]
                except Exception:
                    txt = str(payload)[:400]
                flow.detached_results.append(f"{flow._info(to) or to} → {_speech_clip(txt, 4000)}")
            return payload

        inner = asyncio.ensure_future(_deliver_tracked())
        flow.inflight_tasks.add(inner)
        inner.add_done_callback(flow.inflight_tasks.discard)
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            if not inner.done():
                detached["on"] = True       # 도구 호출만 죽고 위임은 계속 — 결과는 detached로 전달
                if flow.log:
                    flow.log("delegation_detached", to=to, seg=flow.leader_segment)
            raise

    tools.append(request)

    @tool("recruit",
          "인원이 부족하거나 '새 직군'이 필요하면 채용한다. role=맡길 직군(예: 게임 기획자, UX 디자이너, "
          "사운드 — '예비' 인력을 이 직군으로 신규 채용). member=특정 동료 id/역할명(비우고 role만 주면 예비에서 "
          "자동 선발). reason=사유. 로스터에 없는 직군도 이렇게 런타임에 채용해 쓴다. **1봇 1직업 원칙** — "
          "이미 직군이 있는 봇에 새 직군이 필요하면 예비를 그 직군으로 뽑는 게 정도다. 겸직(기존 직군 유지+추가, "
          "봇당 최대 2개)은 예외 — 예비가 없거나 기존 직군과 비슷한 일일 때만 허용된다. "
          "**직군명은 기존 것 재사용 우선** — 같은 도메인 직군이 이미 있으면 그 이름 그대로 쓰고(변형 금지), "
          "정말 다른 일을 하는 새 직군일 때만 new_role='yes'를 함께 줘 명시적으로 만든다.",
          {"member": str, "role": str, "reason": str, "new_role": str})
    async def recruit(args):
        role_name = (args.get("role") or "").strip()
        spec = (args.get("member") or "").strip()
        # [전문화 정책 — 범용 직군 금지(사용자 결정)] 범용(풀스택 등)은 모든 일을 흡수해 전문 채용을
        # 억제하고(라이브: AI·서버·데이터가 한 봇에 22건 집중) 병렬의 병목이 된다. 전문 직군으로 나눠 뽑는다.
        if role_name and any(g in _norm_job(role_name)
                             for g in ("풀스택", "풀 스택", "fullstack", "full stack", "full-stack",
                                       "제너럴", "generalist", "만능", "올라운드")):
            return _ok(f"채용 거부(전문화 정책): '{role_name}' 같은 범용 직군은 두지 않습니다 — 범용은 모든 "
                       f"일을 흡수해 전문 채용을 막고 병렬의 병목이 됩니다(1봇 1직업 전문화가 회사 원칙). "
                       f"필요한 전문 직군으로 나눠 뽑으세요(예: 백엔드 / 프론트엔드 / AI 엔지니어 / 데이터 엔지니어).")
        # [직군 중복 생성 게이트 — 근본] recruit가 자유 텍스트 직군명을 받다 보니 흐름마다 변형 이름
        # ('VFX 전문가' 있는데 'VFX 아티스트')으로 '같은 도메인 직군'이 새 Discord 역할로 계속 불어났다.
        # 비교 풀은 현재 팀 라벨 + '서버의 커스텀 역할 전체'(직군 역할은 서버 영속이라, 토큰 유실/오프라인
        # 봇의 직군도 보인다). 변형이 감지되면 생성하지 않고 멈춰 세운다 — 재사용(기존 이름 그대로)이나
        # 명시적 신설(new_role='yes')은 에이전트가 정한다(시스템이 정답 이름을 정하는 하드코딩 아님).
        if role_name:
            existing_jobs = {j for v in flow.bot_info.values()
                             if v and not str(v).startswith(_SPARE_LABEL)
                             for j in _jobs_of(v)}   # 겸직 라벨은 구성 직군으로 풀어 비교
            fn_roles = getattr(g, "get_custom_role_names", None)
            if fn_roles and getattr(flow, "guild_id", None):
                try:
                    existing_jobs |= set(await fn_roles(flow.guild_id) or [])
                except Exception:
                    pass
            dup = _find_variant_job(role_name, existing_jobs)
            if dup and _norm_job(args.get("new_role") or "") not in ("yes", "y", "true", "1"):
                if flow.log:
                    flow.log("recruit_variant_blocked", asked=role_name, existing=dup)
                return _ok(f"직군 중복 의심으로 보류: '{role_name}'은(는) 이미 있는 직군 '{dup}'의 변형으로 "
                           f"보입니다(같은 도메인을 다른 이름으로 또 만들면 직군이 계속 불어납니다). 같은 일이면 "
                           f"role='{dup}' 그대로 다시 호출해 기존 직군으로 채용하세요. 정말 '{dup}'과(와) 다른 "
                           f"일을 하는 새 직군이 필요하면 new_role='yes'를 함께 줘 명시적으로 신설하세요.")
        if flow.current is None:
            # [예비 담당자 '자기 직군 우선'] Task 열기 전에 담당자가 자기 직군부터 정하는 건 허용한다 — 자기
            # 자신 + role 지정일 때만. 이래야 '예비'인 채로 create_project/create_task를 열어 화면(상태블록·동료
            # 프롬프트)에 '예비'로 박히는 걸 막는다(사용자가 본 '담당자가 예비로 들어옴'의 직접 원인). 다른 사람
            # 채용 등은 종전대로 Task가 먼저 있어야 한다.
            self_pick = _resolve_members(spec, flow, flow.pool) if spec else []
            if role_name and ((not spec) or (self_pick and self_pick[0] == me_id)):
                # 1봇 1직업: 이 분기는 '예비(무직)' 담당자용이다 — 이미 직군이 있는 봇이 자기 직군을
                # 덮어쓰면(디자이너→게임 기획자) 전문화 기억이 영속 오염된다(라이브 관측). 같은 직군
                # 재확인만 통과시키고, 다른 직군은 거부한다(필요하면 예비를 그 직군으로 뽑는 것).
                cur = (flow._info(me_id) or "").strip()
                new_label = role_name
                if cur and not _is_spare(flow, me_id):
                    cur_jobs = _jobs_of(cur)
                    if any(_norm_job(j) == _norm_job(role_name) for j in cur_jobs):
                        return _ok(f"이미 '{role_name}' 직군을 보유하고 있습니다 — 그대로 진행하세요(변경 없음).")
                    # 겸직 예외(사용자 정책): ① 풀에 예비가 한 명도 없거나 ② 새 직군이 기존 직군과
                    # '비슷한 일'(도메인 토큰 공유)일 때만, **기존 직군을 유지한 채** 새 직군을 더한다
                    # (교체 아님 — 전문화 기억 보존). 봇당 최대 2개(직군 스택 누적 재발 방지). 그 외에는
                    # 1봇 1직업 원칙 — 예비를 그 직군으로 새로 뽑는 게 정도.
                    spares_left = [s for s in flow.pool if _is_spare(flow, s)]
                    similar = any(_job_tokens(j) & _job_tokens(role_name) for j in cur_jobs)
                    if spares_left and not similar:
                        return _ok(f"자기 직군 추가 거부: 당신은 이미 '{cur}' 직군입니다 — **1봇 1직업** 원칙이라 "
                                   f"무관한 직군('{role_name}') 겸직은 예비가 없거나 비슷한 일일 때만 허용됩니다"
                                   f"(전문화 보호). '{role_name}'이 필요하면 Task를 연 뒤 recruit(role='{role_name}')로 "
                                   f"'예비'를 그 직군으로 채용하세요(예비 {len(spares_left)}명).")
                    if len(cur_jobs) >= 2:
                        return _ok(f"겸직 한도 초과: 당신은 이미 직군 2개('{cur}')를 보유하고 있습니다 — 봇당 "
                                   f"겸직은 최대 2개입니다. '{role_name}'은 예비나 다른 동료에게 맡기세요.")
                    new_label = f"{cur}{_JOB_SEP}{role_name}"
                flow.bot_info[me_id] = new_label
                if getattr(flow, "persist_role", None):
                    try:
                        flow.persist_role(me_id, new_label)
                    except Exception:
                        pass
                fn = getattr(g, "assign_job_role", None)
                if fn and getattr(flow, "guild_id", None):
                    try:
                        await fn(flow.guild_id, me_id, new_label)
                    except Exception:
                        pass
                what = "겸직 추가" if _JOB_SEP in new_label else "확정"
                return _ok(f"자기 직군 {what}: 당신(id {me_id})의 직군 = '{new_label}' — 한 직원으로 "
                           f"참여합니다. 이어서 create_project → create_task로 팀을 꾸려 시작하세요.")
            return _ok("오류: 진행 중인 Task가 없습니다. 먼저 create_task로 Task를 여세요. (단 '예비' 담당자가 자기 "
                       "직군을 정하는 recruit(member=자신, role=…)는 Task 전에도 됩니다 — 자기 직군부터 정하세요.)")
        # 충원 루프 하드 차단: 최근 요청이 연속 2회+ 실패(시스템 일시불안정)면 채용을 막는다 — 지금 새로
        # 뽑아도 같은 불안정으로 똑같이 실패한다('백엔드 6명' 사태의 구조적 차단; 안내가 아니라 거부).
        # 기존 동료에게 다시 요청해 한 명이라도 응답이 오면 consec_fail이 리셋돼 다시 채용 가능.
        if getattr(flow, "consec_fail", 0) >= 2:
            return _ok(f"채용 보류: 최근 요청이 연속 {flow.consec_fail}회 무응답/실패 — 시스템 일시 불안정입니다. "
                       f"지금 새로 뽑아도 같이 실패하니 채용을 막습니다(무한 충원 루프 방지). 기존 동료에게 잠시 뒤 "
                       f"다시 요청해 한 명이라도 응답이 오면 그때 충원하거나, 계속 안 되면 사용자에게 보고하고 멈추세요.")
        cand = _resolve_members(spec, flow, flow.pool) if spec else []
        if not cand:
            # member 미지정(또는 못 찾음): 직군 채용이면 '예비' 인력에서 자동 선발(아직 프로젝트팀에 없는 예비)
            spares = [m for m in flow.pool if _is_spare(flow, m) and m not in flow.project_team]
            if role_name and spares:
                cand = [spares[0]]
            else:
                return _ok(f"채용할 인력을 못 찾음 — member로 기존 동료(id/역할)를 지정하거나, role로 새 직군을 "
                           f"적어 '예비'를 채용하세요. 남은 예비: {len(spares)}명 / 현재 풀: {flow._names(flow.pool)}")
        mid = cand[0]
        # 예비(직군 미배정)는 'role=직군'을 줘야만 채용된다 — 말로만 배정 차단(직군은 구조적으로 부여).
        if _is_spare(flow, mid) and not role_name:
            return _ok(f"채용 거부: {flow._info(mid) or mid}는 '예비'(직군 미배정)입니다 — role='직군명'을 함께 "
                       f"지정해 어떤 직군으로 채용할지 정하세요(예: recruit(member='{mid}', role='게임 기획자')). "
                       f"직군 없이는 합류·위임 불가(말로만 배정 금지 — 직군이 실제로 부여돼야 일을 맡길 수 있음).")
        # [같은 직군 채용도 자유] role 중복/실패상태로 채용을 거부하지 않는다 — 반복 채용('백엔드 6명')의 진짜
        # 원인은 '동료 무응답(서브프로세스 행)'이었고 그건 워커 턴 타임아웃으로 끊었다(8분 내 인프라실패 처리).
        # 따라서 필요하면 같은 직군을 더 뽑아도 된다. '무응답=인프라'라는 판단·안내는 요청 실패 메시지로만 한다.
        hired = ""
        if role_name:
            cur = flow._info(mid)
            if _is_spare(flow, mid) or not cur:
                flow.bot_info[mid] = role_name                    # 예비/무직 → 그 직군으로 (1봇 1직업)
                hired = f" — '{role_name}' 직군으로 채용"
                # '기억'(직업 고정): 한 번 직군을 받은 예비는 다음 흐름에도 그 직업을 유지한다(매 흐름 '예비'로
                # 원복되지 않음) — 직업군을 누적·재사용하기 위함. best-effort(없으면 이번 흐름에만 적용).
                if getattr(flow, "persist_role", None):
                    try:
                        flow.persist_role(mid, role_name)
                    except Exception:
                        pass
            elif not any(_norm_job(j) == _norm_job(role_name) for j in _jobs_of(cur)):
                # 이미 다른 직군 보유 — 원칙은 **1봇 1직업**(새 직군은 예비를 뽑는 게 정도). 겸직은 사용자
                # 정책의 예외 둘 중 하나일 때만: ① 풀에 예비가 한 명도 없음(어쩔 수 없음) ② 새 직군이
                # 기존 직군과 '비슷한 일'(도메인 토큰 공유). 허용 시 교체가 아니라 **추가**다 — 기존 전문화
                # 기억(주직군)을 유지한 채 부직군을 더하고, 봇당 최대 2개(직군 5~6개 스택 재발 방지).
                cur_jobs = _jobs_of(cur)
                spares_left = [s for s in flow.pool if _is_spare(flow, s)]
                similar = any(_job_tokens(j) & _job_tokens(role_name) for j in cur_jobs)
                if spares_left and not similar:
                    return _ok(f"채용 거부: {cur}(id {mid})는 이미 '{cur}' 직군입니다 — **1봇 1직업** 원칙이라 "
                               f"무관한 직군('{role_name}') 겸직은 예비가 없거나 비슷한 일일 때만 허용됩니다"
                               f"(전문화 기억 보호). '{role_name}'이 필요하면 recruit(role='{role_name}')로 "
                               f"'예비'를 그 직군으로 새로 뽑으세요(예비 {len(spares_left)}명).")
                if len(cur_jobs) >= 2:
                    return _ok(f"겸직 한도 초과: {flow._info(mid) or mid}(id {mid})는 이미 직군 2개('{cur}')를 "
                               f"보유 — 봇당 겸직은 최대 2개입니다. '{role_name}'은 예비나 다른 동료에게 맡기세요.")
                new_label = f"{cur}{_JOB_SEP}{role_name}"
                flow.bot_info[mid] = new_label
                hired = f" — '{role_name}' 겸직 추가(보유: {new_label})"
                if getattr(flow, "persist_role", None):
                    try:
                        flow.persist_role(mid, new_label)
                    except Exception:
                        pass
            # 이미 그 직군을 보유하고 있으면 라벨 변경 없이 그대로 합류.
            flow.current.status.group = _group_of(flow, flow.current.team)
            # 이름은 그대로 두고 '직군 라벨 전체'를 Discord 역할(권한)로 동기화 — best-effort.
            fn = getattr(g, "assign_job_role", None)
            if fn and getattr(flow, "guild_id", None):
                try:
                    await fn(flow.guild_id, mid, flow.bot_info.get(mid) or role_name)
                except Exception:
                    pass
        if mid not in flow.project_team:
            flow.project_team.append(mid)
        if mid not in flow.current.team:
            flow.current.team.append(mid)
            flow.current.status.group = _group_of(flow, flow.current.team)
            await flow.refresh()
            await _add_members(g, flow.current.thread_id, [mid])   # 스레드에 합류(멤버십=팀)
        return _ok(f"{flow._info(mid) or mid} 합류{hired}(사유: {args.get('reason', '')}). "
                   f"현재 팀: {flow._names(flow.current.team)}")

    tools.append(recruit)

    @tool("run",
          "작업공간에서 명령을 실행해 산출물을 직접 검증(빌드/구동/테스트). cwd=작업공간, 60s 제한, "
          "웹 작품은 **실제 브라우저 검증 가능**: playwright+chromium 설치됨 — 예: PJT venv의 python -c로 "
          "sync_playwright 페이지 로드→로드시간·콘솔에러·스크린샷 확인('실행됨'과 '사용할 만함'은 다르다). "
          "출력 반환. 서버 구동은 'node server.js & sleep 1; curl -s localhost:3000/'처럼 백그라운드+점검으로 "
          "묶으면 됨 — run이 끝나면 백그라운드 프로세스까지 자동 정리하므로 kill 불필요(다음 run의 포트 충돌 없음). "
          "파괴·git·시스템경로 명령은 차단.",
          {"command": str})
    async def run(args):
        cmd = str(args.get("command", ""))
        if not getattr(flow, "workspace", None):
            return _ok("실행 불가: 작업공간이 설정되지 않았습니다.")
        if any(d in cmd.lower() for d in _RUN_DENY):
            return _ok(f"실행 거부(안전): 파괴/저장소/시스템 패턴 포함 — {cmd[:80]}")
        if any(p in cmd for p in _RUN_AUTHOR):
            return _ok("실행 거부: run은 '실행·빌드·검증' 전용입니다 — 파일 작성/수정은 Write/Edit 도구로 "
                       "하세요(그래야 권한·협의 게이트가 적용되고 누가 무엇을 만들었는지 기록됩니다). 예: "
                       "server.js 작성은 Write, 패키지 설치·서버 구동·curl 점검은 run. 남의 도메인 산출물을 "
                       "run으로 대신 찍어내지 말고 그 owner에게 Work로 위임하세요.")

        def _exec():
            # 자체 세션(프로세스그룹)으로 실행 → 직속 셸 종료 후 그룹째 정리한다.
            # 이게 run 간 포트 충돌(EADDRINUSE)의 구조적 해결: 'node server.js &'로 띄운
            # 백그라운드 서버가 init으로 reparent돼 누수되는 일이 없다.
            # 출력은 파이프 대신 임시파일로 — 백그라운드 자식이 파이프를 잡고 있어도 wait가 안 막힌다.
            of, ef = tempfile.TemporaryFile(), tempfile.TemporaryFile()
            p = subprocess.Popen(cmd, shell=True, cwd=str(flow.workspace),
                                 stdout=of, stderr=ef, start_new_session=True)
            timed_out = False
            try:
                rc = p.wait(timeout=60)        # 직속 셸 종료까지만 대기
            except subprocess.TimeoutExpired:
                timed_out, rc = True, None
            finally:
                _reap_pgroup(p.pid)            # 백그라운드 자식까지 그룹째 정리(누수/포트충돌 차단)
                try:
                    p.wait(timeout=2)          # 셸 좀비 회수
                except Exception:
                    pass
            of.seek(0); ef.seek(0)
            out = of.read().decode("utf-8", "replace"); err = ef.read().decode("utf-8", "replace")
            of.close(); ef.close()
            return timed_out, rc, out, err

        try:
            timed_out, rc, out, err = await anyio.to_thread.run_sync(_exec)
        except Exception as e:
            return _ok(f"실행 오류: {e}")
        if timed_out:
            _dbg(f"[RUN] {me_id} `{cmd[:60]}` TIMEOUT")
            return _ok("실행 시간초과(60s) — 그룹째 정리함. 서버는 'node server.js & sleep 1; curl ...'처럼 "
                       "백그라운드로 띄우세요(포그라운드로 서버를 실행하면 멈춥니다).\n"
                       f"[부분 stdout]\n{out[-800:]}\n[부분 stderr]\n{err[-400:]}")
        _dbg(f"[RUN] {me_id} `{cmd[:60]}` exit={rc}")
        if flow.current is not None:
            flow.current.verified = True          # 실행 0회 완료 차단(layer1)
            flow.current.run_count += 1
            # 시스템이 직접 캡처한 영수증(에이전트 말이 아니라 실제 출력). 완료 보고에 떼어낼 수 없게 묶인다.
            errtail = ("\n[stderr] " + err[-200:]) if (err or "").strip() else ""
            flow.current.evidence = f"exit={rc} `{cmd[:50]}`\n{(out or '')[-400:]}{errtail}"
        return _ok(f"[exit {rc}] (작업공간)\n[stdout]\n{out[-1500:]}\n[stderr]\n{err[-600:]}")

    tools.append(run)

    if role == "leader":
        @tool("create_project",
              "Project로 판단되면 전용 채널 생성 + 규모를 산정해 팀 배정"
              "(team=쉼표구분 동료 id/역할명, 리더 제외분). 비우면 풀 전체.",
              {"name": str, "team": str})
        async def create_project(args):
            if flow.project_channel is not None:
                return _ok(f"이미 project_channel={flow.project_channel} (project_id={flow.project_id}) — "
                           f"개입 중이면 create_project 말고 바로 작업하세요.")
            flow.project_channel = await g.create_project_channel(flow.guild_id, args["name"])
            # [작업공간 격리·신원] 폴더는 여기서 깎지 않는다 — 흐름은 시작부터 고유 임시 폴더(new-…)에서
            # 일했고, 아래 register_project가 그 폴더를 **식별번호 이름(p-00n-슬러그)으로 개명**해
            # 신원을 번호로 확정한다(리더 작명 충돌이 폴더·배포 수준에서 무해 — 사용자 제안).
            assigned = _resolve_members(args.get("team", ""), flow, flow.pool)
            if assigned:
                flow.project_team = _uniq([flow.leader] + assigned)
            # 프로젝트는 내부 레지스트리에만 등록(채널 자체가 프로젝트 식별자 — 채널에 앵커 안 박음).
            flow.project_name = args["name"]   # 배포 슬롯 유도용(프로젝트별 결정적 서비스명)
            if flow.register_project:
                flow.project_id = flow.register_project(flow.project_channel, args["name"])
            return _ok(f"project_channel={flow.project_channel} project_id={flow.project_id} "
                       f"프로젝트팀={flow._names(flow.project_team)}")
        tools.append(create_project)

        @tool("create_task",
              "Task '빈 껍데기'를 연다 — **Purpose도 비운 채 멤버만 배정**한다(리더가 할 일을 미리 못 박음 = 중앙집권 "
              "방지). 이후 **배정된 팀이 모여(request Info) Purpose(풀 문제)·Goal(성공기준)을 함께 정해 set_goal로 "
              "확정**한다. Owner는 그 일을 Work로 받은 동료가 된다(선배정 금지). **members=이 일에 필요한 직군 동료를 "
              "당신이 직접 고른다**(자동 전원 소집 아님 — 직군 고정 방지). 비우면 프로젝트팀(예비 제외) 기본, 모자란 "
              "직군은 recruit(role=)로 채운다.",
              {"members": str})
        async def create_task(args):
            if flow.current is not None and flow.current.status.status != "완료":
                return _ok(f"현재 Task({flow.current.task_id}: {(flow.current.status.purpose or '미정')[:24]})가 아직 "
                           f"'진행'입니다 — 단일흐름은 한 번에 Task 하나만. complete_task로 먼저 마감한 뒤 "
                           f"다음 Task를 여세요(여러 산출물도 하나씩 순차로).")
            ch = flow.project_channel or flow.user_channel
            tid = flow.next_task_id()
            pool = flow.project_team or flow.pool
            picked = _resolve_members(args.get("members", ""), flow, pool)
            # 팀은 담당자(리더)가 '일에 맞게' 동적으로 고른다 — 자동 전원 소집 아님. members=로 필요한 직군만
            # 지정하면 그들로. 비우면 기본 팀은 **직군당 1명**(실행 핵심)으로 둔다 — [팀 비대 차단, 라이브
            # 2026-06-14: 역할 드리프트(과거 recruit가 Discord 역할로 영속)로 백엔드 5명 등이 기본 팀에 다
            # 들어와, set_goal '전원 협의' × 비대 = meet 4회·6 잠수·override 노이즈·136분 미수렴]. 같은 직군
            # 중복은 협의·게이트 비용만 키우므로(Brooks: 소통비용~인원²) 기본에서 빼고, 정말 병렬 일손이
            # 필요하면 recruit/members=로 더한다(리더 자율). 매직넘버 아님 — '한 도메인 한 책임자'는 이미
            # 시스템의 단일-owner 보편 이치. set_goal은 '이 (슬림한) 팀 전원' 협의로 통과.
            if picked:
                base = picked
            else:
                base, _seen = [], set()
                for m in flow.project_team:
                    if m == flow.leader or _is_spare(flow, m):
                        continue
                    r = (flow._info(m) or "").strip()
                    if r and r in _seen:
                        continue        # 같은 직군 중복은 기본 팀에서 제외(recruit로 추가 가능)
                    _seen.add(r)
                    base.append(m)
            team = _uniq([flow.leader] + base)
            # 'PM 혼자 Task' 차단(구조): 프로젝트에 직군 동료가 있는데 리더 혼자만 멤버로 여는 건 팀을 버리고
            # 단독작업·독식하는 패턴(사용자가 본 'PM 혼자 있는 Task'). 동료가 무응답이라고 새 솔로 Task로 도망가지
            # 말 것 — 그건 '환경 불안정'이니 사용자에게 보고하고 멈춰야 한다. 진짜 1인 프로젝트(동료 없음)·개입은 허용.
            others = [m for m in flow.pool if m != flow.leader and not _is_spare(flow, m)]
            if team == [flow.leader] and others and not getattr(flow, "intervention", None):
                return _ok(f"단독 Task 거부: 이 프로젝트엔 동료({flow._names(others)})가 있는데 당신 혼자만 멤버인 "
                           f"Task는 열 수 없습니다(팀 버리고 단독작업·독식 금지 — 사용자가 지적한 'PM 혼자 Task'). "
                           f"일에 맞는 동료를 members로 넣어 함께 하세요. 동료가 모두 응답 불능이면 새 솔로 Task로 "
                           f"넘어가지 말고 '환경(인프라) 불안정으로 일시 중단'을 사용자에게 보고하고 멈추세요.")
            # Purpose·Goal·Owner 모두 비워둔다 — 빈 껍데기. Purpose·Goal은 배정된 팀이 모여 set_goal로 정하고,
            # Owner는 Work-request 수신으로 떠오른다(리더가 할 일·목표·담당을 미리 박던 중앙집권 제거).
            status = TaskStatus(task_id=tid, purpose="", status="진행",
                                goal="", owner="", group=_group_of(flow, team))
            block_id, thread_id = await g.open_task(ch, status)
            await _add_members(g, thread_id, [m for m in team if m != flow.leader])  # 멤버십=팀
            flow.project_channel = ch
            ref = TaskRef(task_id=tid, thread_id=thread_id, block_id=block_id,
                          status=status, team=team, owner=0)   # participated는 빈 set에서 시작(Task별 협의 추적)
            flow.tasks.append(ref)
            flow.current = ref
            flow.comm.reset_task_tracking()   # 새 산출물 단위 → '완료/Redo' 추적 초기화(Redo는 같은 Task 안에서만)
            _ckpt(flow)                       # 크래시-세이프: 열린 즉시 영속(동면·강제종료에도 같은 Task로 복구)
            # [공급 원칙 — RFC-005 / 매직넘버 제거(사용자 원칙 2026-06-13)] '소통 비용은 인원²'은
            # 보편 이치(Brooks)지만 '6명+'라는 트리거는 임의값(4항목과 같은 부류). 크기 임계를 빼고
            # '실행 핵심만 + 나머지는 검증·자문' 분리를 크기 무관 질적 조언으로 — 판단은 리더.
            size_note = ("\n[정보 — 판단은 당신 몫] 실행(직접 구현)에 꼭 필요한 핵심만 owner로 두고, 나머지 "
                         "전문가는 검증·자문(request Info)으로 두는 편이 좋습니다 — 소통·조율 비용은 실행 "
                         "인원이 늘수록 가파르게 커집니다(필요 이상 큰 실행 팀은 비효율). 회의·검증엔 전원, "
                         "실행엔 핵심만.")
            return _ok(f"task={tid} (빈 껍데기·담당자가 팀 선정) thread={thread_id} 팀={flow._names(team)}{size_note} — 이 팀은 "
                       f"당신이 고른 구성입니다(직군이 부족하면 recruit(role=)로 더하세요). 배정된 팀과 **meet(회의)로 "
                       f"'Purpose(풀 문제)·Goal(성공기준)·각자 도메인 할 일'을 함께 정한 뒤** set_goal로 확정하세요 — "
                       f"meet은 독립의견을 동시에 모으고(앵커링 방지) 토론·회의록(합의)까지 남깁니다(1:1 request(Info)를 "
                       f"여러 번 도는 것보다 합의가 또렷하고 빠름 — 개별 후속 확인만 Info로). 전원 협의 전엔 set_goal "
                       f"거부됨. 그 다음 일을 맡길 동료에게 Work로 위임.")
        tools.append(create_task)

        @tool("set_goal",
              "팀 회의로 정한 이번 Task의 **Purpose(풀 문제)와 Goal(측정가능한 성공기준)**을 확정·기록한다. 리더 "
              "단독/선지정 금지 — **이 Task의 멤버 전원**과 meet(회의)로 'Purpose·각 도메인의 목표·성공기준'을 "
              "수렴한 결과를 적는다(1:1 request(Info)보다 meet 권장 — 앵커링↓·회의록 자동 기록). Goal엔 '무엇이 "
              "되면 성공인가'(결과·시나리오)만 쓰고 '어떤 파일·엔드포인트·스택으로 만들지'(구현 방법)는 쓰지 말 것 — "
              "그건 owner가 정한다. Work 위임은 확정 뒤에만 가능. required_roles=이 goal에 필요한 전문 직군 목록"
              "(쉼표구분, 예: '프론트엔드, 사운드 디자이너') — 팀에 해당 전문가 없으면 확정 거부→recruit 강제"
              "(전문화 파이프라인 보장: 정확한 직군에게 맡겨야 경험 축적→증류→개선이 작동).",
              {"purpose": str, "goal": str, "required_roles": str})
        async def set_goal(args):
            if flow.current is None:
                return _ok("오류: 진행 중인 Task가 없습니다. create_task로 먼저 여세요.")
            goal = (args.get("goal") or "").strip()
            purpose = (args.get("purpose") or "").strip()
            if not goal:
                return _ok("오류: goal이 비었습니다.")
            # Purpose·Goal은 '담당 팀이 함께' 정한다(docs: Task.Team이 Goal을 정한다). 이 Task 멤버 전원이
            # '실질 협의(participated)'에 참여했는지 검사 — 리더가 물었든 peer끼리 물었든 인정(허브 완화),
            # 단 빈 핑('응답 가능?')은 불인정(실질 강제). → 매 Task를 팀이 모여 정하는 분산 구조를 구조적으로 보장.
            members = [x for x in flow.current.team if x != me_id]
            missing = [m for m in members if m not in flow.current.participated]
            if missing:
                return _ok(f"확정 거부: 이 Task의 Purpose·Goal은 담당 팀이 함께 정합니다(리더 독단·선지정 금지). "
                           f"아직 의견을 안 받은 멤버: {flow._names(missing)} — 그들과 **meet(회의)로 '풀 문제·각 "
                           f"도메인의 목표·성공기준'을 함께 정한 뒤** set_goal로 기록하세요(meet 발언이 협의로 인정됨 — "
                           f"1:1 request(Info)도 인정되나 회의가 앵커링↓·합의 기록↑). 파일·엔드포인트 같은 구현 스펙 "
                           f"말고 '측정가능한 결과'로.")
            # [P7 — 범주적 완성 점검: recognition→action 강제, RFC-010] 확정 전 1회, 장르 예시 대비 '통째로
            # 없는 범주'를 goal에 '구축 대상'으로 반영(없으면 recruit)하거나 불필요 사유를 명시하게 강제한다 —
            # 라이브: P6 넛지로 사운드를 grep '점검'만 하고 구현 0(인지≠행동). 점검을 '구축'으로 한 칸 올림.
            # 1회 보류 후 재호출 통과(막지 않되 의식적 결정 — override 게이트와 같은 정신). 직군 키워드 하드코딩
            # 없음 — 장르·범주 판단은 리더(비체험형이면 'N/A 불필요'로 재호출). set_goal_gap_check 로그로 가시화.
            if not getattr(flow, "gap_checked", False):       # 흐름당 1회(per-flow) — 작품의 범주 점검은 한 번
                flow.gap_checked = True
                if flow.log:
                    flow.log("set_goal_gap_check", task=flow.current.task_id)
                return _ok("확정 보류(범주적 완성 점검 — RFC-010 P7 / RFC-011 M1): 확정 전 한 번만 — **이 작품과 "
                           "같은 종류의 '훌륭한 예'를 WebSearch로 실제로 하나 찾아보고**(상상 말 것 — LLM은 자기 "
                           "산출을 기준 삼아 '평범=충분'으로 수렴하므로 실제 레퍼런스가 외부 기준이 됩니다), 그것이 "
                           "*당연히 갖춘* 요소 중 우리 작품엔 *통째로 빠진* 범주가 있는지 보세요. 무엇이 그런 범주인지는 "
                           "**작품 종류를 아는 당신이 판단**합니다(시스템이 "
                           "특정 범주·직군을 지정하지 않음 — 하드코딩 없음). 있으면 그건 '개선'이 아니라 신규 구축이니 "
                           "**goal에 '구축 대상'으로 넣으세요**(담당 직군이 팀에 없으면 recruit). 정말 없어도 되면 "
                           "goal에 그 이유를 적은 뒤 set_goal을 재호출해 확정하세요(인지를 *점검*에서 *구축*으로). "
                           "재호출 시 **required_roles에 이 goal에 필요한 전문 직군을 모두 나열**하세요"
                           "(예: '프론트엔드, 백엔드, 디자이너'). 팀에 해당 전문가가 없으면 확정이 "
                           "거부되고 recruit가 강제됩니다 — 정확한 직군에게 맡겨야 경험 축적→증류→개선이 작동합니다"
                           "(1봇1직업 전문화 파이프라인 보장). 재호출은 통과합니다(판단은 당신).")
            # [팀 전문성 커버리지 — 전문화 파이프라인의 구조적 보장(사용자 교정 2026-06-15)]
            # P7(범주 인식)이 '무엇이 빠졌는가'를 밝히지만, '그 전문가가 팀에 있는가'까지 검증하지
            # 않았다 → 리더가 범주를 인지해도 비전문가에게 시켜 placeholder가 나오고, 경험이 그 직군에
            # 안 쌓여 학습 플라이휠(craft→경험→증류→개선)이 안 도는 근본 원인이었다. required_roles
            # 로 리더가 필요 직군을 선언하면, 시스템이 팀 구성을 대조해 부재 시 recruit를 강제한다.
            # 직군·도메인 하드코딩 없음 — 판단(무엇이 필요한가)은 리더, 검증(있는가)은 시스템.
            required = [r.strip() for r in (args.get("required_roles") or "").split(",") if r.strip()]
            if required:
                _skip = {_norm_job(s) for s in ("현재 팀 충분", "현재팀충분", "n/a", "없음", "")}
                real_required = [r for r in required if _norm_job(r) not in _skip]
                if real_required:
                    team_roles = set()
                    for m in flow.current.team:
                        info = (flow._info(m) or "").strip()
                        if info and not info.startswith(_SPARE_LABEL):
                            for j in _jobs_of(info):
                                if j.strip():
                                    team_roles.add(j.strip())
                    missing = []
                    for r in real_required:
                        rn = _norm_job(r)
                        if not rn:
                            continue
                        if any(_norm_job(j) == rn for j in team_roles):
                            continue
                        if _find_variant_job(r, team_roles):
                            continue
                        missing.append(r)
                    if missing:
                        spares = [s for s in flow.pool if _is_spare(flow, s)]
                        if flow.log:
                            flow.log("set_goal_team_gap", task=flow.current.task_id,
                                     missing=missing, team_roles=sorted(team_roles))
                        return _ok(
                            f"확정 거부(팀 전문성 부재 — 1봇1직업 전문화 파이프라인): goal에 필요하다고 "
                            f"선언한 전문 직군 중 현재 Task 팀에 없는 것: **{', '.join(missing)}**. "
                            f"recruit(role='{missing[0]}')로 해당 전문가를 먼저 채용한 뒤 set_goal을 "
                            f"다시 호출하세요(예비 인력 {len(spares)}명). "
                            f"정확한 전문 직군에게 맡겨야 경험이 그 직군에 쌓이고 전문화 파이프라인"
                            f"(craft profile → 경험 축적 → 수면 증류 → 기준 개선)이 작동합니다 — "
                            f"'할 수 있다'가 아니라 '그 분야 전문성으로 잘한다'가 배정 기준입니다"
                            f"(현재 팀: {', '.join(sorted(team_roles)) or '(없음)'}).")
            if purpose:
                flow.current.status.purpose = purpose
            flow.current.status.goal = goal
            await flow.refresh(flow.current)
            _ckpt(flow)                       # 크래시-세이프: 확정된 Purpose·Goal 영속
            # [공급 원칙 — 정보는 구조가, 판단은 리더가. 매직넘버 제거(사용자 지적 2026-06-13)]
            # 종전엔 'goal 4항목+'(항목 수)로 분해를 트리거했으나, 항목 '수'는 표면 프록시다 — RFC-008이
            # 경고한 측정의 함정(Goodhart)의 재발이었다. 분해 필요성의 본질은 개수가 아니라 '독립적으로
            # 구현·검증할 단위로 나뉘는가'(응집도/검증 단위). 숫자 게이트 없이 질적 기준만 공급하고 판단은
            # 리더(LLM)에게 — 검증·마감 게이트가 부분마다 생겨 결함이 일괄 통과되지 않는다(P-009 교훈).
            tip = ("\n[정보 — 판단은 당신 몫] 이 goal이 **서로 독립적으로 구현·검증할 수 있는 여러 부분**을 "
                   "담고 있으면(개수가 아니라 '독립성'이 기준), 부분마다 Task로 나눠(create_task→위임→검증→"
                   "complete_task 반복) 각각 마감하는 것을 고려하세요 — 한 Task로 가면 검증·마감이 1회뿐이라 "
                   "부분 결함이 묻힙니다(라이브 P-009). 깊게 얽혀 한 덩어리면 굳이 나누지 마세요.")
            # [RFC-008 P0 — 품질/기능 분리] 측정 가능한 기능만 goal에 담으면 측정 어려운 품질이 빠진다
            # (Holmström-Milgrom 다중작업: 측정가능한 것만 보상 → 품질 이탈이 최적). 기능 체크리스트와
            # 별도로 '이 도메인의 훌륭함'(완성도·UX·재미)을 품질 차원으로 의식하게 — 정의 불가한 품질도
            # 부분 operationalize는 가능(Graham). 강제 아닌 공급(암묵지라 다 못 적음 — Polanyi).
            team_roles = [r for r in flow._names([m for m in flow.current.team if m != me_id]) if r]
            qbar = ("\n[품질 차원 — '되는가'≠'배포할 만한가'] 측정가능한 기능만 goal에 담으면 측정 어려운 품질"
                    "(완성도·UX·재미·연출)이 빠집니다(라이브: 작동하나 폴리시 0인 게임). 지금 두 가지를 의식하세요: "
                    f"① **이 팀 구성에서 품질 축을 유도** — 팀의 각 직군({', '.join(team_roles) or '동료'})이 "
                    "*자기 도메인에서 '훌륭함'으로 치는 기준*이 곧 '완성'의 축입니다(무엇이 훌륭함인지는 그 직군이 "
                    "정의 — 시스템이 특정 항목을 박지 않음). goal에 기능 체크리스트와 함께 그 품질 기대치를 한 줄씩 "
                    "담으세요. ② **폴리시(기능을 넘는 품질) 직군이 팀에 있는가** — 이 작품이 그런 사용 경험을 "
                    "요구하는 종류면(판단은 당신) 그 전문가가 팀에 있는지 보고 없으면 recruit하세요 — 안 부르면 그 "
                    "품질은 아무도 책임지지 않습니다(라이브: 폴리시 직군 미채용/잠수로 '최소 기능'만 배포됨).")
            # [RFC-010 P3·P5 — 발산→수렴 + '완성' 재정의] LLM은 정렬(RLHF)으로 '전형적·자명한 1개 완성'으로
            # 수렴한다(mode collapse) → "언급한 것만" 구현(라이브 사용자 지적). 창의/체험형은 ① 복수 접근안을
            # 내고 골라야 뻔함을 깬다(발산→수렴, CreativeDC) ② '작동=완성'이 아니라 '경험돼서 좋음'이 완성이다
            # (Pirsig·Craftsmanship). 강제 아닌 환기 — 취향의 천장은 LLM이라 최종 판단은 리더/사용자(RFC-010 §3).
            creative = ("\n[창의·완성 기준 — 자명한 1개로 수렴 금지] 이 작품이 경험·재미·디자인이 중요한 "
                        "종류라면: ① **'언급된 것'만 하지 말고** 더 좋게 만들 접근을 2~3개 떠올려 비교한 뒤 "
                        "고르세요(LLM은 시키면 가장 뻔한 1개로 수렴 — 의식적 발산→수렴). ② '완성'의 기준은 "
                        "'작동한다'가 아니라 **'사용자로서 써보니 좋다'**입니다 — 마감 전 누군가 실제로 써보고"
                        "(플레이) '재밌나·뭐가 아쉽나'를 비평하고 최소 1회 개선하세요(작동≠좋음).")
            # [RFC-010 P6 — 장르 예시 대비 '범주적 부재' 점검(라이브: 게임에 사운드 0인데 아무도 인지·채용
            # 안 함). LLM은 '있는 것 개선'엔 강하나 '통째로 없는 범주'를 못 본다(mode collapse는 확장만 한다).
            # 처방: 같은 '종류의 훌륭한 예'와 비교해 그쪽엔 있는데 우리엔 없는 범주를 찾고, 그건 '개선'이
            # 아니라 '신규 구축'이며 필요 직군이 없으면 recruit한다(Graham '최고를 알아야 목표가 보인다' +
            # exemplar anchoring). 직군 키워드 하드코딩 없음 — 장르 판단·예시는 LLM 지식, 채용은 리더.
            gapcheck = ("\n[범주적 부재 점검 — '있는 것 개선'에만 머물지 말 것] 이 작품과 **같은 종류의 훌륭한 "
                        "예**를 하나 **WebSearch로 실제로 찾아보고**(상상 말 것 — 자기 산출 기준으론 '평범=충분'으로 "
                        "수렴), 그것이 *당연히 갖춘* 요소 중 우리에겐 **통째로 없는 범주**가 있는지 "
                        "보세요. 무엇이 그런 범주인지는 **작품 종류를 아는 당신이 판단**합니다(시스템이 특정 범주를 "
                        "지정하지 않음 — 직군·키워드 하드코딩 안 함). 있으면 그건 '개선'이 아니라 **신규 구축**이고, "
                        "담당 직군이 팀에 없으면 **recruit**하세요. 라이브 교훈: 기존 것만 깊게 파고 *통째로 빠진 "
                        "범주*는 아무도 본 적이 없었음 — 훌륭한 예라면 당연히 있을 범주를 먼저 점검(다듬기 전에).")
            # [RFC-011 M3 — 누적 사용자 취향을 '진짜 품질 기준'으로] 상용 품질의 천장은 LLM 취향이라
            # (인간 상관 ~0.5) 유일한 신뢰 앵커는 사용자다. 이 프로젝트에서 사용자가 반복해 지적·요구한
            # 말을 그대로 되돌린다 — '언급된 것'만 고치지 말고 *되풀이되는 불만의 범주*를 goal의 품질 축으로
            # (직군·키워드 하드코딩 0 — 사용자 자신의 말). 배포→플레이→비평이 돌수록 기준이 스스로 올라간다.
            fb_texts = [f.get("text", "") for f in (getattr(flow, "user_feedback", None) or []) if f.get("text")]
            taste = ""
            if fb_texts:
                bullets = "\n".join(f"  · “{_speech_clip(t, 160)}”" for t in fb_texts[-8:])
                taste = ("\n[누적 사용자 취향 — 이 작품의 진짜 품질 기준] 사용자가 이 프로젝트에서 해 온 "
                         "말들입니다. **되풀이되는 불만·요구가 곧 '상용 수준'의 기준**입니다(LLM 취향엔 천장이 "
                         "있어 사용자가 유일한 앵커). 이번에 '언급된 것'만 처리하지 말고, 아래에서 **반복되는 "
                         "범주**(어떤 측면이 계속 '부족·구리다'고 지적되는지)를 찾아 goal의 품질 축으로 박으세요 "
                         "— 그 범주가 통째로 부실하면 신규 구축·recruit로 끌어올리세요:\n" + bullets)
            return _ok(f"task={flow.current.task_id} 정의 확정 — Purpose: {purpose[:50] or '(유지)'} / Goal: {goal[:80]}{tip}{qbar}{creative}{gapcheck}{taste}")
        tools.append(set_goal)

        @tool("complete_task",
              "현재 Task의 목표가 충족되면 상태블록을 완료로 마감(result 기록). 다음 Task는 create_task로.",
              {"result": str})
        async def complete_task(args):
            if flow.current is None:
                return _ok("오류: 진행 중인 Task가 없습니다.")
            if not flow.current.verified:
                return _ok(f"완료 거부: 이 Task({flow.current.task_id})를 run으로 한 번도 실행하지 않았습니다 "
                           f"— 산출물을 run으로 실제 실행한 뒤 complete_task 하세요(허위 완료 금지).")
            # owner가 '턴 한도'로 미완 반환한 Task는 완료 불가 — 같은 owner에게 request(Work)로 '이어서' 재위임해
            # 마저 끝내야 한다(허위완료→다음 Task churn·유실 차단). 이어가기는 Redo 한도와 무관하게 계속 가능.
            if flow.current.owner_incomplete:
                return _ok(f"완료 거부: 이 Task의 담당자가 '턴 한도'로 작업을 미완 반환했습니다 — 새 Task로 넘어가지 말고, "
                           f"같은 담당자에게 request(Work)로 '이어서 남은 부분을 마저 끝내라'고 재위임해 완성시킨 뒤 "
                           f"complete_task 하세요(이어가기는 횟수 제한 없음). 미완을 두고 다음으로 넘어가면 그 작업이 유실됩니다.")
            # owner에게 Work를 위임해 놓고(소유자 지정됨) 그 owner가 '검증된 산출물+응답'을 아직 내지 않았는데
            # 리더가 대신 완료하는 것을 막는다 — 이것이 사용자가 지적한 '허위 완료'(owner가 일하는 중/응답 전인데
            # 완료 때리고 다음 Task 열기)의 정확한 차단점. owner가 실제로 일하고 응답이 돌아와야(owner_delivered)
            # 완료 가능. (리더가 위임 없이 자기 도메인을 직접 한 Task는 owner==0이라 이 게이트를 건너뛴다.)
            if flow.current.owner and not flow.current.owner_delivered:
                return _ok(f"완료 거부: 이 Task는 owner({flow.current.status.owner or flow.current.owner})에게 "
                           f"위임돼 있는데 그 owner가 아직 '검증된 산출물'을 응답으로 내지 않았습니다(착수 전·작업 중일 "
                           f"수 있음). **owner가 일하는 중에 대신 완료하지 마세요(허위 완료 금지).** 같은 owner에게 "
                           f"request(Work)로 맡겨 run 검증 증거가 붙은 완료 응답을 받은 뒤 complete_task 하세요. "
                           f"끝내 무응답이면 recruit/재배정으로 다른 담당에게 맡기세요(리더가 대리 구현·완료 금지).")
            # [지각 비대칭 검증 — 범용 대문제 교정(사용자 "사운드 직군 없이 만들어냄" 2026-06-15)] LLM의
            # 외부현실 검증(비전: 스크린샷→Read, WebSearch 대조)은 '검증자가 지각 가능한 차원'(시각·텍스트)을
            # 암묵 전제한다. 검증자가 직접 경험해야만 품질을 아는 차원(들어야 아는 소리·음악, 느껴야 아는
            # 손맛 등)은 외부 대조가 불가능해 'presence(코드가 호출되나)'로 회귀 → 비전문가의 코드 합성
            # placeholder(라이브 P-010: 사운드=오실레이터 자급, 사운드 직군 0·recruit 0)가 완성으로 통과.
            # 지각 불가 차원은 '자기 판정'이 구조적으로 불가하므로, 자급을 완성으로 닫지 말고 '검증된 실제
            # 자원 또는 전문성'을 의무화한다(외부현실·전문화 원칙의 비시각 차원 확장). 흐름당 1회 보류 후
            # 재호출 통과(막지 않되 보이게 — 판단·범주는 리더. 직군·도메인 하드코딩 없음, 'gap_checked' 패턴).
            if not getattr(flow, "percept_checked", False):
                flow.percept_checked = True
                if flow.log:
                    flow.log("complete_percept_gate", task=flow.current.task_id)
                return _ok("마감 보류(지각 비대칭 점검 — 흐름당 1회): 이 작품이 만든 것 중 **화면으로 보거나 "
                           "코드로 '됐다' 확인할 수 없고, 직접 경험해야만(보는 것 말고 듣거나 느껴야) 품질을 "
                           "아는 차원**이 있습니까? — 있다면 LLM 검증자는 그것을 지각할 수 없어 '코드가 "
                           "호출되는가'까지만 검증되고 '좋은가'는 판정 불가입니다. 그런 차원은 코드로 합성한 "
                           "placeholder('있긴 하나 상용 아님')를 완성으로 닫지 마세요 — **WebSearch로 실제 제작 "
                           "자원(CC0 등)을 받아 통합하거나, 그 분야 전문 직군을 recruit**하세요(지각 불가 차원은 "
                           "자기판정이 불가하므로 자급을 완성으로 인정하지 않습니다). 그런 차원이 없으면(전부 "
                           "화면·코드로 검증 가능) 그대로 complete_task를 재호출하세요(통과). 무엇이 그런 차원인지는 "
                           "작품을 아는 당신이 판단합니다(시스템은 특정 범주·직군을 지정하지 않음).")
            # [검증 분업 — 1회 보류] 품질 판정이 리더 1인에게 독점되는 것을 구조적으로 흔든다(라이브
            # P-009: QA·교차 검증 0인 채 단독 마감 → 브라우저 렉·적 돌진 등 사용성 결함이 그대로 통과,
            # 사용자가 첫 발견). owner 인도 후 '다른 멤버'의 검증 참여가 0이면 첫 호출만 보류하고 검증
            # 위임을 안내한다 — 재호출은 통과(판단은 결국 리더 몫, 무한 반려 금지. 직군 키워드 없음).
            # [교차 검증 의무 — Rule/Task.md 6 (사용자 확정: 범용 이치는 하드 제한도 옳다)]
            # 작업자(owner) 아닌 멤버가 산출물을 '사용자처럼 실제로 사용해' 검증해야 완수 선언 가능.
            # 제3멤버가 팀에 있는 한 우회 없음(거부 반복) — 라이브 P-009: 단독 마감이 렉·사용성
            # 결함을 통과시킴. 제3멤버가 정말 없을 때만 예외(단독 마감 마커가 기록에 남는다).
            third = [m for m in flow.current.team
                     if m not in (flow.leader, flow.current.owner)]
            # [발견1 교정] 검증 대상: owner 위임 산출물 OR 리더 직접구현(leader_writes>0). 리더 독식도
            # 제3자 검증을 면제하지 않는다(보편 이치). 산출물도 없으면(아무것도 안 만든 Task) 게이트 무의미.
            has_product = bool(flow.current.owner) or getattr(flow.current, "leader_writes", 0) > 0
            if has_product and flow.current.cross_checks == 0 and third:
                idle = [m for m in third if flow.act_by.get(m, 0) == 0]
                idle_note = (f"\n[정보] 이 Task 팀에서 **실작업·검증 참여 0**인 멤버: {flow._names(idle)} — "
                             f"goal에 이들의 전문 영역이 있다면 그 부분의 검증·보완을 이들에게 맡기는 것이 "
                             f"자연스럽습니다." if idle else "")
                per_item = ("goal의 각 부분이 '존재하나'가 아니라 **그 산출물을 쓰는 사람으로서 처음부터 끝까지 "
                            "써본 경험**으로 — **시각 산출물이면 화면을 스크린샷으로 찍어 Read로 직접 '눈으로 보고'**"
                            "(존재가 아니라 '실제로 이렇게 보인다/작동한다'를 직접 확인 — 임시방편처럼 보이는지 상용 "
                            "수준인지), **같은 종류의 훌륭한 예를 WebSearch로 실제로 찾아 대조**해 '써보니 좋은가·"
                            "답답한가? 뭐가 싸구려 같고 빠지거나 떨어지나? 상용 수준이면 당연히 있을 게 없나?'를")
                # [RFC-008 P0 + RFC-010 P4 — 직무 기준을 '좋은가' 비평 루브릭으로] 산출물 도메인의 craft
                # profile을 검증 루브릭으로 제공한다. 검증자가 "작동하는가"(holistic)가 아니라 "**실제로
                # 써보니 이 도메인 기준에 비춰 좋은가·뭐가 아쉬운가**"를 차원별로 보게 — rubric-guided judge가
                # 인간 일치 2배(LLM-Rubric 2501.00274). 단일 점수 아닌 차원별 비평 + 사용자가 최종 취향 앵커.
                rubric = ""
                owner_job = ((flow._info(flow.current.owner) if flow.current.owner
                              else flow._info(flow.leader)) or "").strip()
                if callable(getattr(flow, "craft_of", None)) and owner_job:
                    parts = [flow.craft_of(j) for j in owner_job.split("·") if j.strip()]
                    parts = [p for p in parts if p]
                    if parts:
                        rubric = (f"\n[검증 루브릭 — 산출물 도메인 '{owner_job}'의 품질 기준. 검증 위임 본문에 "
                                  f"**이 기준을 그대로 전달**하고, 검증자가 **실제로 실행/플레이한 뒤** 각 항목을 "
                                  f"'써보니 좋은가·충분한가'로 평가하게 하세요 — '돌아가는가'가 아니라 '이 기준에 "
                                  f"비춰 좋은가, 뭐가 아쉬운가'가 질문입니다(미달은 구체적 결함으로):\n"
                                  + _speech_clip("\n---\n".join(parts), 2500) + "]")
                fb_v = [f.get("text", "") for f in (getattr(flow, "user_feedback", None) or []) if f.get("text")]
                taste_v = ""
                if fb_v:
                    taste_v = ("\n[사용자가 반복해 지적한 것 — 검증에서 직접 확인] 이 프로젝트에서 사용자가 해 온 "
                               "비평입니다. 검증자에게 **이 항목들이 이번엔 실제로 해소됐는지 써보고 확인**하라고 "
                               "전하세요(되풀이된 불만이 곧 상용 기준):\n"
                               + "\n".join(f"  · “{_speech_clip(t, 140)}”" for t in fb_v[-8:]))
                return _ok(f"완료 거부(교차 검증 의무 — Rule/Task + RFC-010 P1·P2 / RFC-011 M2): 산출물 인도 후 "
                           f"**다른 멤버의 검증 참여가 0**입니다. **만든 사람이 아닌** 다른 멤버에게 request(Work)로 "
                           f"'**코드만 읽지 말고 산출물을 처음부터 끝까지 실제로 실행·사용·플레이해 본 뒤**(라이브 "
                           f"근거: 실제로 써본 검증자가 읽기만 한 쪽보다 결함을 훨씬 많이 잡음 — TITAN 82% vs 18%) "
                           f"{per_item} 보고하라'고 맡긴 뒤 마감하세요. **'요소 존재·JS 에러 0·서버 기동됨' 같은 것은 "
                           f"'작동'이지 '좋음'의 증거가 아닙니다 — 검증으로 인정하지 마세요**(라이브: 그렇게 통과시킨 게 "
                           f"'상용 수준 아님'으로 반려됨). 검증자의 결함·아쉬움 보고가 Redo(창의적 개선)의 근거입니다. "
                           f"**자기 산출물 자기검증은 무효**(편향 — Pride&Prejudice): 반드시 만든 사람이 아닌, 실제로 "
                           f"써본 다른 멤버. 검증 응답이 오면 게이트는 자동으로 열립니다.{rubric}{taste_v}{idle_note}")
            # [팀 기여 의무 게이트 — RFC-009] 교차 검증(cross_checks)과 **독립**. 검증이 됐어도(검증은
            # 기능 위주라 폴리시 부재를 못 잡음 — RFC-009 §3), 팀에 부른 직군이 이 흐름에서 회의 발언만 하고
            # 실작업·검증 0(act_by==0: Write/Edit/run 한 번도 없음)이면 그 도메인(타격감·그래픽·사운드·디자인·
            # UX 등 폴리시)은 작품에 '반영되지 않은' 것이다 — 라이브 P-010: VFX·디자이너·모션·게임비주얼이
            # 실구현 0인 채 마감돼 "단순 나열 웹·타격감 없는 게임"이 됨(발언≠기여). 직군 키워드 없이 '실작업
            # 0'만 본다(보편 이치: 부른 직군은 기여한다, 회의 참석≠기여). 1회 보류 후 재호출 통과(무한 반려
            # 금지 — 판단은 리더). 동면 복구로 act_by가 0에서 재시작한 경우에도 1회 환기되나 '복구 후 기여
            # 재확인'으로 무해(검증 누계 리셋과 같은 정신) — 재호출 통과.
            if has_product and not flow.current.contrib_checked:
                contrib_idle = [m for m in third if flow.act_by.get(m, 0) == 0]
                if contrib_idle:
                    flow.current.contrib_checked = True
                    if flow.log:
                        flow.log("task_contrib_idle", task=flow.current.task_id,
                                 idle=[int(m) for m in contrib_idle])
                    # [RFC-009 2단계 정수 — 발언→책임] 회의록(meet 미니츠는 '[NR] 직군: 발언'으로 화자
                    # 귀속)에서 잠수 직군 '본인의 발언'을 끌어와 게이트에 그대로 되돌린다 — "당신이 회의에서
                    # 한 이 말이 산출물에 들어갔나?"(발언≠구현). 직군 키워드 없이 본인 발언만 에코(보편
                    # 이치). 발언은 collab_notes로 Work 위임에 자동 동봉되므로(577·1562) ①로 맡기면 본인
                    # 약속이 구현자=본인에게 전달돼 루프가 닫힌다 — 별도 '발언→Task' 게이트가 불필요(중복).
                    notes_lines = (getattr(flow.current, "collab_notes", "") or "").splitlines()
                    commits = []
                    for m in contrib_idle:
                        role = (flow._info(m) or "").strip()
                        said = [ln.split(":", 1)[1].strip() for ln in notes_lines
                                if role and f"] {role}:" in ln]
                        said = [s for s in said if s]
                        if said:
                            commits.append(f"· {role}: “{_speech_clip(' / '.join(said), 240)}”")
                    commit_note = ("\n[회의 발언 대조 — 발언≠구현] 아래는 이 직군들이 회의에서 한 말입니다 — "
                                   "각 발언이 실제 산출물에 반영됐는지 직접 확인하고, 안 됐으면 ①로 맡기세요:\n"
                                   + "\n".join(commits)) if commits else ""
                    return _ok(
                        f"완료 보류(팀 기여 의무 — RFC-009): 팀의 {flow._names(contrib_idle)}이(가) 이 흐름에서 "
                        f"**회의 발언 외 실작업·검증이 0**입니다(Write/Edit/run 0회) — 이 직군의 도메인"
                        f"('되는가'를 넘는 그 직군의 품질·폴리시)이 **작품에 반영되지 "
                        f"않았습니다**. 셋 중 하나를 택하세요: ① 필요한 도메인이면 request(Work)로 맡겨 "
                        f"**실제로 만들게** 하고 그 산출물을 교차 검증까지 받으세요 ② 애초에 불필요했으면 "
                        f"팀에서 빼세요(왜 불렀나=다음 학습) ③ 둘 다 아니면 complete_task 재호출로 통과(판단은 "
                        f"당신) — **단, 재호출로 통과하면 '이 직군들을 뺀 채 마감'이 Task 기록에 남습니다**(정말 "
                        f"불필요하면 result에 그 이유를 적으세요; 반사적 통과 방지). 특히 회의에서 '중요하다'고 한 "
                        f"부분이 실제 산출물에 들어갔는지 확인하세요 — 발언만으로는 작품이 바뀌지 않습니다.{commit_note}")
            done_ref = flow.current
            # 허위보고 차단(도메인 무관): 완료의 '진짜'는 에이전트 산문이 아니라 시스템이 캡처한 실행 영수증.
            # 코드는 합격/불합격을 판단하지 않고(하드코딩·QA역할 가정 X), 보고 옆에 실제 출력을 떼어낼 수 없게 묶는다.
            report = _speech_clip(args.get("result") or "", 800)   # Task 블록(Discord 2000 한도) 안에 들어가는 요약
            # [침묵 강행 불가] 검증 분업 보류를 재호출로 강행한 '단독 마감'은 기록에 그렇게 보이게 한다
            # ("자를 수는 있어도 조용히는 못 자른다"의 마감 버전) — 행동은 막지 않되(자동 회사·리더 판단),
            # 사후 분석·사용자가 한눈에 보게(범용 이치의 구조 잠금, 사용자 승인 2026-06-12).
            solo = bool((flow.current.owner or getattr(flow.current, "leader_writes", 0) > 0)
                        and flow.current.cross_checks == 0)
            if solo and flow.log:
                flow.log("task_solo_completed", task=flow.current.task_id, owner=int(flow.current.owner or 0))
            # [기여 미흡 마감 가시화 — RFC-009, 침묵 강행 불가] 게이트 1회 보류를 재호출로 통과해(옵션③)
            # 잠수 직군이 여전히 실작업 0인 채 마감되면, '이 직군들을 뺀 채 마감'을 결과에 박아 영속한다 —
            # 라이브 3/3 게이트가 전부 반사적 재호출로 통과해 폴리시가 또 빠짐(사용자 지적). 행동은 막지
            # 않되(리더 자율) 사후 분석·사용자·학습이 한눈에 보게(단독 마감 마커와 같은 정신). 직군 키워드 없음.
            contrib_idle_now = [m for m in third if flow.act_by.get(m, 0) == 0] if has_product else []
            if contrib_idle_now and flow.log:
                flow.log("task_contrib_overridden", task=flow.current.task_id,
                         idle=[int(m) for m in contrib_idle_now])
            done_ref.status.status = "완료"
            done_ref.status.result = (
                (f"[검증: 단독 마감 — 교차 검증 0, 리더 판정만]\n" if solo else "")
                + (f"[기여 미흡: {flow._names(contrib_idle_now)} 실작업 0 — 리더 판단으로 마감(폴리시 미반영 가능)]\n"
                   if contrib_idle_now else "")
                + f"[보고] {report}\n"
                f"[시스템 실행기록 {done_ref.run_count}회·마지막] {done_ref.evidence or '(없음)'}"
            )[:1400]
            await flow.refresh(done_ref)
            await _react(g, flow.project_channel, done_ref.block_id, "✅")  # 완료=이모지
            flow.current = None
            _ckpt(flow)                       # 크래시-세이프: 마감 즉시 '미완 없음'으로 영속(유령 복원 방지)
            return _ok(f"task={done_ref.task_id} 완료 마감 (시스템 실행기록 {done_ref.run_count}회 첨부)")
        tools.append(complete_task)

        @tool("vote",
              "팀 표결(구조적 합의): 선택지를 두고 멤버 전원의 선택+근거를 **동시에**(독립·앵커링 방지) "
              "수집·집계한다. question=안건, options='선택지1;선택지2;...', members=쉼표구분(비우면 현재 "
              "Task 팀 전원). 1:1 Info를 여러 번 도는 대신 합의를 구조화 — 결과(집계+근거)를 보고 리더가 확정한다.",
              {"question": str, "options": str, "members": str})
        async def vote(args):
            if flow.current is None:
                return _ok("오류: 진행 중인 Task가 없습니다. create_task 먼저 여세요.")
            opts = [o.strip() for o in str(args.get("options", "")).split(";") if o.strip()]
            if len(opts) < 2:
                return _ok("오류: options에 선택지 2개 이상을 ';'로 구분해 주세요.")
            voters = _resolve_members(args.get("members", ""), flow, flow.current.team) or \
                     [m for m in flow.current.team if m != me_id]
            voters = [v for v in voters if v != me_id and not _is_spare(flow, v)]
            if not voters:
                return _ok("오류: 표결할 멤버가 없습니다.")
            if (any(not x.done() for x in getattr(flow, "inflight_tasks", ()))
                    and flow.comm.alive != me_id and not flow.comm.done):
                return _ok("[대기] 직전 위임이 아직 진행 중입니다 — 표결은 그 결과를 받은 뒤 여세요.")
            if getattr(flow, "fork_active", 0) > 0:
                return _ok("[대기] 다른 의견 수집이 진행 중입니다 — 그 결과를 받은 뒤 여세요(중첩 수집 금지).")
            if flow.comm.done or flow.comm.alive != me_id:
                return _ok(f"지금은 표결을 열 수 없습니다(활성={flow.comm.alive}) — 진행 중인 요청의 "
                           f"응답을 받은 뒤 다시 시도하세요.")
            question = str(args.get("question", "")).strip()

            detached = {"on": False}

            async def _run_vote():
                # [병렬 fork-join] 표는 서로 '독립'(앵커링 방지)이라 동시 수집이 의미를 바꾸지 않고
                # 시간만 줄인다 — 수집이 싸지면 표결을 아껴 쓰지 않게 된다(협동 빈도↑ = 품질).
                def body_of(v):
                    return (f"[표결 — 독립 의견] 안건: {question}\n선택지: {' / '.join(opts)}\n"
                            f"동료들의 표는 보이지 않습니다(앵커링 방지). 당신의 전문가 관점에서 "
                            f"하나를 고르고 근거를 2줄 이내로. 반드시 형식: [표] 선택지명\n근거")
                tally, reasons = {o: 0 for o in opts}, []
                for v, res, note in await _fork_collect(flow, me_id, voters, body_of):
                    if res is None:
                        reasons.append(f"{flow._info(v) or v}: {note}")
                        continue
                    m = re.search(r"\[표\]\s*([^\n]+)", res or "")
                    pick = (m.group(1).strip() if m else "")
                    chosen = next((o for o in opts if o in pick or pick in o), None)
                    if chosen:
                        tally[chosen] += 1
                    # [판정자 사본도 침묵 절단 금지] 리더는 이 근거로 표결을 '판정'한다 — 채널
                    # 발언(400 안전망+잘림 표기)과 같은 내용이어야 한다. 종전 [:150] 하드컷은
                    # 판정자가 동강난 근거로 결정하게 만들던 같은 부류의 결함(잘림 사건의 잔재).
                    reasons.append(f"{flow._info(v) or v}: {(pick or '무효')} — {_speech_clip(res, 400)}")
                    await _say(v, f"[표] {(pick or '무효')} — {_speech_clip(res, 400)}")  # 본인 명의 발언
                    if v in flow.current.team and v != flow.leader:
                        flow.current.participated.add(v)        # 표결 참여 = 실질 협의 인정
                board = " / ".join(f"{o}: {n}표" for o, n in tally.items())
                if flow.current is not None:
                    record = f"[표결] {question}\n{board}\n" + "\n".join(reasons)
                    flow.current.collab_notes = _speech_clip(
                        (getattr(flow.current, 'collab_notes', '') + '\n\n' + record).strip(), 6000)
                    _ckpt(flow)
                return _ok(f"[표결 집계] {question}\n{board}\n\n[각자의 선택·근거]\n" + "\n".join(reasons)
                           + "\n\n(집계는 참고 — 최종 확정은 당신(리더)의 판정입니다.)")

            inner = asyncio.ensure_future(_run_vote())
            flow.inflight_tasks.add(inner)
            inner.add_done_callback(flow.inflight_tasks.discard)
            try:
                return await asyncio.shield(inner)
            except asyncio.CancelledError:
                if not inner.done():
                    detached["on"] = True
                    if flow.log:
                        flow.log("delegation_detached", to="vote", seg=flow.leader_segment)

                    def _hand(t):
                        try:
                            flow.detached_results.append(f"표결 완료 → {_speech_clip(t.result()['content'][0]['text'], 4000)}")
                        except Exception:
                            pass
                    inner.add_done_callback(_hand)
                raise
        tools.append(vote)

        @tool("meet",
              "라운드로빈 회의: 1라운드는 전원의 '독립 의견'을 동시에 수집하고(앵커링 방지), 2라운드부터 "
              "서로의 발언을 보며 직렬로 토론한다(회의록 반환). topic=주제, members=쉼표구분(비우면 현재 "
              "Task 팀 전원), rounds=라운드 수(기본 2). 1:1 중계 없이 실제 다자 토론을 구조화 — 회의록을 "
              "보고 리더가 수렴·확정한다.",
              {"topic": str, "members": str, "rounds": str})
        async def meet(args):
            if flow.current is None:
                return _ok("오류: 진행 중인 Task가 없습니다. create_task 먼저 여세요.")
            members = _resolve_members(args.get("members", ""), flow, flow.current.team) or \
                      [m for m in flow.current.team if m != me_id]
            members = [m for m in members if m != me_id and not _is_spare(flow, m)]
            if not members:
                return _ok("오류: 회의할 멤버가 없습니다.")
            if (any(not x.done() for x in getattr(flow, "inflight_tasks", ()))
                    and flow.comm.alive != me_id and not flow.comm.done):
                return _ok("[대기] 직전 위임이 아직 진행 중입니다 — 회의는 그 결과를 받은 뒤 여세요.")
            if getattr(flow, "fork_active", 0) > 0:
                return _ok("[대기] 다른 의견 수집이 진행 중입니다 — 그 결과를 받은 뒤 여세요(중첩 수집 금지).")
            if flow.comm.done or flow.comm.alive != me_id:
                return _ok(f"지금은 회의를 열 수 없습니다(활성={flow.comm.alive}) — 진행 중인 요청의 "
                           f"응답을 받은 뒤 다시 시도하세요.")
            topic = str(args.get("topic", "")).strip()
            try:
                rounds = max(1, min(3, int(str(args.get("rounds", "2")).strip() or "2")))
            except ValueError:
                rounds = 2

            async def _run_meet():
                minutes = []
                # 1라운드 = 독립 의견 fork(동시 수집) — 첫 입장은 서로를 안 보는 게 앵커링 없는
                # 진짜 다양성이고, 동시 수집이라 회의 비용도 준다(회의가 싸져야 자주 연다 = 협동성).
                def body_r1(m):
                    return (f"[회의 1라운드 — 독립 의견] 주제: {topic}\n(이 라운드에선 동료 발언이 "
                            f"보이지 않습니다 — 앵커링 방지)\n당신({flow._info(m)})의 전문 관점 "
                            f"입장을 3~5줄(최대 1000자)로, 근거와 함께.")
                for m, res, note in await _fork_collect(flow, me_id, members, body_r1):
                    cut = _speech_clip(res or note)   # 회의록·채널 발언은 같은 내용(기록 일치)
                    line = f"[1R] {flow._info(m) or m}: {cut}"
                    minutes.append(line)
                    await _say(m, f"[회의 1R] {cut}")  # 본인 명의 발언
                    if res is not None and m in flow.current.team and m != flow.leader:
                        flow.current.participated.add(m)        # 회의 발언 = 실질 협의 인정
                # 2라운드+ = 직렬 상호 토론(서로의 발언을 보며 동의/반박/보완) — 품질의 원천인
                # 순차 문맥은 병렬화 대상이 아니다(여기는 종전 그대로).
                for r in range(2, rounds + 1):
                    for m in members:
                        if flow.comm.done or flow.comm.alive != me_id:
                            break
                        log_txt = "\n".join(minutes[-8:]) or "(아직 발언 없음)"
                        body = (f"[회의 {r}라운드] 주제: {topic}\n지금까지의 발언:\n{log_txt}\n\n"
                                f"당신({flow._info(m)})의 차례입니다 — 앞 발언에 동의/반박/보완하며 "
                                f"당신 전문 관점의 입장을 3~5줄(최대 1000자)로. 맹목적 동의 금지(근거 필수).")
                        try:
                            frame = flow.comm.request(me_id, m, "meet", Kind.INFO)
                        except BusyInOtherFlow as e:
                            # 멤버 단위 사유(라운드 사이에 타 흐름이 데려감) — 회의를 끊지 않고 그
                            # 멤버만 건너뛴다(부분 진행). 베턴 경합(아래)과 달리 시스템 문제가 아니다.
                            minutes.append(f"[{r}R] {flow._info(m) or m}: (타 흐름({e.holder_scope}) "
                                           f"참여 중 — 이 라운드 불참)")
                            continue
                        except CommError as e:
                            minutes.append(f"(회의 중단 — 베턴 경합: {str(e)[:60]})")
                            break
                        try:
                            res = await flow.wake(m, body, Kind.INFO)
                        except Exception as e:
                            res = f"(발언 실패: {e})"
                        try:
                            flow.comm.respond(m, "accept", res)
                        except CommError:
                            pass
                        cut = _speech_clip(res)
                        line = f"[{r}R] {flow._info(m) or m}: {cut}"
                        minutes.append(line)
                        await _say(m, f"[회의 {r}R] {cut}")  # 본인 명의 발언
                        if m in flow.current.team and m != flow.leader:
                            flow.current.participated.add(m)    # 회의 발언 = 실질 협의 인정
                if flow.current is not None:
                    record = f"[회의] {topic} ({rounds}R)\n" + "\n".join(minutes)
                    flow.current.collab_notes = _speech_clip(
                        (getattr(flow.current, 'collab_notes', '') + '\n\n' + record).strip(), 6000)
                    _ckpt(flow)   # 합의는 크래시-세이프(재개 위임에도 동봉되도록 스냅샷에 포함)
                return _ok(f"[회의록] 주제: {topic} ({rounds}라운드, {len(members)}명)\n"
                           + "\n".join(minutes)
                           + "\n\n(수렴·확정은 당신(리더)의 몫 — 합의점을 정리해 set_goal/결정에 반영하세요.)")

            inner = asyncio.ensure_future(_run_meet())
            flow.inflight_tasks.add(inner)
            inner.add_done_callback(flow.inflight_tasks.discard)
            try:
                return await asyncio.shield(inner)
            except asyncio.CancelledError:
                if not inner.done():
                    if flow.log:
                        flow.log("delegation_detached", to="meet", seg=flow.leader_segment)

                    def _hand(t):
                        try:
                            flow.detached_results.append(f"회의 완료 → {_speech_clip(t.result()['content'][0]['text'], 4000)}")
                        except Exception:
                            pass
                    inner.add_done_callback(_hand)
                raise
        tools.append(meet)

        @tool("parallel_work",
              "파일 영역이 겹치지 않는 **독립 Work 여러 건을 동시에** 위임(병렬 실행+직렬 통합, RFC-006). "
              "assignments=JSON 배열 '[{\"to\":\"봇id\",\"files\":\"상대경로,상대경로\",\"body\":\"지시\"}]'. "
              "각자 배정된 files에만 쓸 수 있다(쓰기 리스 — 영역 겹침은 거부). 영역이 겹치거나 순서 의존이면 "
              "request(Work) 직렬로. 조인 후 통합·검증·마감은 직렬로 진행.",
              {"assignments": str})
        async def parallel_work(args):
            # [RFC-006 Work-fork v1] 검증된 fork 인프라(_fork_collect: 점유·부분 조인·FAN·detach-safe
            # 코어)에 Work 의미론(쓰기 리스·owner·실작업 판정)을 입힌다 — alive-집합 전면 개편 없이
            # '병렬 실행 + 직렬 통합'(RFC-005 P1)을 연다. 가지는 comm 프레임을 열지 않으므로 재위임
            # 불가(구조 강제) — 실측 근거: P-009·P-010 워커의 중첩 request 0회(막히면 보고→리더 직렬).
            if flow.current is None:
                return _ok("오류: 진행 중인 Task가 없습니다. create_task 먼저 여세요.")
            goal = (flow.current.status.goal or "").strip()
            if not goal:
                return _ok("오류: Goal 확정 전엔 병렬 위임 불가 — set_goal 먼저(분할은 합의된 목표 위에서).")
            if getattr(flow, "fork_active", 0) > 0:
                return _ok("[대기] 다른 수집/병렬이 진행 중입니다 — 조인 후 시도하세요(중첩 병렬 금지).")
            if (any(not x.done() for x in getattr(flow, "inflight_tasks", ()))
                    and flow.comm.alive != me_id and not flow.comm.done):
                return _ok("[대기] 직전 위임이 아직 진행 중입니다 — 결과를 받은 뒤 병렬을 여세요.")
            try:
                items = json.loads(args.get("assignments") or "")
                assert isinstance(items, list) and items
            except Exception:
                return _ok('형식 오류: assignments는 JSON 배열 — 예: [{"to":"12","files":"public/app.js","body":"..."}]')
            fan = max(1, int(os.environ.get("ORGANT_FORK_FAN", "3")))
            if len(items) < 2:
                return _ok("병렬은 2건부터입니다 — 1건은 request(Work)로 위임하세요.")
            if len(items) > fan:
                return _ok(f"병렬 폭 초과({len(items)} > {fan}) — 가장 독립적인 {fan}건만 먼저, 나머지는 조인 후.")
            ws = str(getattr(flow, "workspace", "") or "")
            plan = []
            for it in items:
                try:
                    to = int(str(it.get("to")).strip())
                except Exception:
                    return _ok(f"형식 오류: to가 봇 id가 아닙니다: {it.get('to')!r}")
                if to == me_id:
                    return _ok("자기 자신에게는 병렬 위임 불가 — 자기 몫은 조인 후 직접.")
                if to not in flow.current.team:
                    return _ok(f"요청 거부: {flow._info(to) or to}는 이 Task 팀이 아닙니다 — 팀에 더한 뒤 위임하세요.")
                if _is_spare(flow, to):
                    return _ok(f"요청 거부: {flow._info(to) or to}는 직군 미배정('예비') — recruit로 직군 부여 먼저.")
                files = [f.strip() for f in str(it.get("files") or "").split(",") if f.strip()]
                if not files:
                    return _ok(f"형식 오류: {flow._info(to) or to}의 files가 비었습니다 — 병렬의 전제는 영역 분리(리스).")
                body = str(it.get("body") or "").strip()
                if not body:
                    return _ok(f"형식 오류: {flow._info(to) or to}의 body(지시)가 비었습니다.")
                paths = [os.path.realpath(os.path.join(ws, f)) for f in files]
                plan.append((to, paths, body))
            tos = [p[0] for p in plan]
            if len(set(tos)) != len(tos):
                return _ok("같은 동료에게 두 영역 동시 배정 — 한 건으로 합치세요.")
            # [토큰 중립 조건 ⓐ — 기계 강제] 영역 상호 배타: 일치/포함이면 거부(겹침은 통합 충돌→Redo→토큰 손실).
            for i in range(len(plan)):
                for j in range(i + 1, len(plan)):
                    for a in plan[i][1]:
                        for b in plan[j][1]:
                            if a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep):
                                return _ok(f"영역 겹침 거부: {flow._info(plan[i][0])} ↔ {flow._info(plan[j][0])} "
                                           f"({os.path.basename(a)}) — 겹치는 작업은 직렬(request)로.")
            notes = getattr(flow.current, "collab_notes", "")
            m2 = {to: (paths, body) for to, paths, body in plan}

            def body_of(m):
                paths, body = m2[m]
                files_txt = ", ".join(os.path.relpath(p, ws) if ws else p for p in paths)
                t = (f"[병렬 Work — 이 영역의 책임자는 당신] 이 Task의 Goal: {goal}\n"
                     f"**당신의 쓰기 영역(리스): {files_txt}** — 이 파일들에만 씁니다. 다른 가지가 다른 "
                     f"영역을 동시 작업 중이므로 영역 밖은 Read 참고만 하고, 필요한 변경은 보고의 "
                     f"[리스크]에 적으세요. 동료 재위임은 불가(병렬 가지) — 막히면 막힌 지점을 보고하면 "
                     f"리더가 직렬로 풉니다. 직군 밖이면 첫 줄 `[직군밖] 필요직군` 반려.\n"
                     f"직접 구현하고 run으로 검증한 뒤, 보고 계약([결과]/[변경]/[검증]/[리스크])으로 간결히.\n"
                     f"[요청 맥락] {body}")
                if notes:
                    t += f"\n[팀 협의 기록(회의·표결) — 준수]\n{_speech_clip(notes, 6000)}"
                return t

            acts0 = {to: flow.act_by.get(to, 0) for to in tos}
            if getattr(flow, "write_lease", None) is None:
                flow.write_lease = {}
            for to, paths, _b in plan:
                flow.write_lease[to] = paths
            if flow.log:
                flow.log("parallel_work", n=len(tos), to=",".join(map(str, tos)), seg=flow.leader_segment)

            async def _run_parallel():
                try:
                    results = await _fork_collect(flow, me_id, tos, body_of, kind=Kind.WORK)
                finally:
                    for to in tos:
                        flow.write_lease.pop(to, None)   # 조인=리스 해제(겹침 게이트는 가지 동안만)
                out = []
                for m, res, note in results:
                    acted = flow.act_by.get(m, 0) - acts0.get(m, 0)
                    if res is not None and flow.current and m in flow.current.team and m != flow.leader:
                        flow.current.participated.add(m)
                    if flow.current:
                        flow.current.work_delegated += 1
                    mark = "" if acted > 0 else " ⚠실작업 0(계획만 — 같은 영역 직렬 재위임 고려)"
                    await _say(m, f"[병렬 보고] {_speech_clip(res or note, 1500)}")
                    out.append(f"[{flow._info(m) or m}]{mark}\n{_speech_clip(res or note, 4000)}")
                if flow.current and not flow.current.owner:
                    flow.current.owner = tos[0]   # 기존 규칙(첫 Work 수신자=owner)과 일관 — 통합 기준점
                    if flow.act_by.get(tos[0], 0) > acts0.get(tos[0], 0) and any(
                            m == tos[0] and r is not None for m, r, _n in results):
                        flow.current.owner_delivered = True
                if flow.log:
                    flow.log("parallel_join", n=len(results), seg=flow.leader_segment)
                _ckpt(flow)
                return _ok(f"[병렬 조인 — {len(results)}건]\n" + "\n\n".join(out)
                           + "\n\n(통합·교차 검증·마감은 직렬로 — 겹치는 후속 작업은 request(Work) 한 명에게.)")

            inner = asyncio.ensure_future(_run_parallel())
            flow.inflight_tasks.add(inner)
            inner.add_done_callback(flow.inflight_tasks.discard)
            try:
                return await asyncio.shield(inner)
            except asyncio.CancelledError:
                if not inner.done():
                    if flow.log:
                        flow.log("delegation_detached", to="parallel", seg=flow.leader_segment)

                    def _hand(t):
                        try:
                            flow.detached_results.append(
                                f"병렬 조인 → {_speech_clip(t.result()['content'][0]['text'], 4000)}")
                        except Exception:
                            pass
                    inner.add_done_callback(_hand)
                raise
        tools.append(parallel_work)


        @tool("deploy",
              "검증을 마친 산출물을 실제로 공개 배포한다(GitHub push + Render 웹서비스 생성/갱신). "
              "name=영문 소문자·하이픈 서비스명(예: slither-multiplayer). 라이브 URL을 반환. "
              "Node 앱이어야 하고 서버는 process.env.PORT를 사용해야 함. run 검증을 끝낸 뒤 마지막에 호출.",
              {"name": str})
        async def deploy(args):
            # [배포 폴링 차단] 재호출은 '점검'이 아니라 새 배포 트리거(git push+빌드 리셋)다 — 빌드가
            # 길어지면 리더가 1분마다 deploy를 다시 불러 빌드를 계속 리셋하는 자기 영속 루프 + 같은 턴
            # 병렬 4연발이 라이브 관측됨([안내][배포] 도배). 흐름당 동시 1회만, 진행 중엔 [대기].
            if getattr(flow, "deploy_inflight", False):
                return _ok("[대기] 배포가 이미 진행 중입니다 — deploy를 다시 부르지 마세요(재호출은 점검이 "
                           "아니라 **새 배포를 또 트리거**해 빌드를 계속 리셋합니다). 진행 중인 배포의 "
                           "성공/실패 결과가 곧 이 도구의 응답으로 돌아옵니다 — 그때 판단하세요.")
            name = deploy_service_name(flow, args.get("name", ""))   # 프로젝트별 결정적 서비스명
            if not name:
                return _ok("배포 불가: 미등록 흐름은 배포 슬롯이 없습니다 — 배포는 프로젝트마다"
                           "(P-번호 슬롯, organt-p-00n) 설정됩니다. create_project로 등록한 뒤 "
                           "다시 배포하세요.")
            gh, ghu = os.environ.get("GH_PAT"), os.environ.get("GH_USER")
            rk, owner = os.environ.get("RENDER_KEY"), os.environ.get("RENDER_OWNER")
            if not (gh and ghu and rk and owner):
                return _ok("배포 불가: 배포 자격증명(GH_PAT/GH_USER/RENDER_KEY/RENDER_OWNER)이 설정되지 않았습니다.")
            if not getattr(flow, "workspace", None):
                return _ok("배포 불가: 작업공간이 없습니다.")
            from .deploy import deploy_sync
            flow.deploy_inflight = True
            try:
                result = await anyio.to_thread.run_sync(deploy_sync, flow.workspace, name, gh, ghu, rk, owner)
            finally:
                flow.deploy_inflight = False
            flow.deployed = result                 # 배포 호출됨 기록(SYS의 배포 강제가 중복 안 하게)
            return _ok(result)
        tools.append(deploy)

    return tools


def build_guide_server(flow: Flow, me_id: int, role: str):
    return create_sdk_mcp_server("guide", "1.0.0", make_guide_tools(flow, me_id, role))
