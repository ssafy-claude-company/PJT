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

from .communication import CommError, CommunicationManager, RedoLimitExceeded
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
                ("create_project", "create_task", "set_goal", "complete_task", "deploy")]

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
        self.project_team: List[int] = list(self.pool)             # 기본=풀 전체(리더가 좁힐 수 있음)
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


def make_guide_tools(flow: Flow, me_id: int, role: str):
    g = flow.guide
    tools = []

    async def _note(text):
        """거부 등 흐름 사건을 스레드에 보이게 남긴다(조용한 유실 방지)."""
        try:
            if flow.current:
                await g.post(int(flow.current.thread_id), me_id, f"[안내] {text}")
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
                _dbg(f"{tag} ✗거부:프로젝트밖")
                await _note(f"{flow._info(to) or to}는 이 프로젝트 팀이 아님 — recruit로 합류 후 요청")
                return _ok(f"요청 거부: {to}({flow._info(to)})는 이 프로젝트 팀이 아닙니다. "
                           f"프로젝트 외부 인력은 신중히 — recruit로 합류시킨 뒤 요청하세요.")
            else:
                return _ok(f"요청 거부: {to}는 채용 풀에 없습니다. 풀: {flow._names(flow.pool)}")
        if flow.wake is None:
            return _ok("오류: 시스템 준비 안 됨")
        # 직렬화: 베턴이 내 차례가 될 때까지 대기(거부 아님). 서로 다른 동료로의 병렬 요청은 순차 처리되며,
        # 첫 요청이 길게(중첩 협의·긴 구현) 걸려도 베턴은 결국 돌아오므로 위임이 끊기지 않는다. 데드라인은
        # 교착 안전장치 — 게임처럼 한 동료가 10분+ 작업하는 경우까지 넉넉히(1시간) 둬 '활성=동료' 반려가
        # 안 뜨게 한다(이전 600초는 긴 작업 중 병렬요청이 타임아웃돼 무서운 '거부' 노이즈를 냈다).
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
            return _ok(f"[{to} 응답] {flow.req_results[dupkey][:600]}\n"
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
        except CommError as e:
            if flow.log:   # 관측: 거부 시점의 베턴 상태(alive)·요청자를 영속 기록 → 원인 규명
                flow.log("req_rejected", frm=me_id, to=to, kind=str(getattr(kind, "value", kind)),
                         alive=flow.comm.alive, seg=flow.leader_segment, reason=str(e)[:70])
            _dbg(f"{tag} ✗거부:규약 ({e})")
            await _note(f"{flow._info(to) or to}에게 요청했으나 거부됨 — {e}")
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
                await _note(f"{flow._info(to) or to}에게 같은 산출물 재위임 한도 초과 — 직접 보완하거나 수락하세요")
                return _ok(f"재위임 거부(Redo 한도 초과): {to}({flow._info(to)})는 이미 이 산출물을 여러 번 "
                           f"보완했습니다. 같은 일을 또 떠넘기지 말고 — 직접 Read/run으로 확인 후 Write/Edit로 "
                           f"마무리하거나, goal이 충족됐으면 complete_task로 마감하세요.")
            owner_body = (f"[보완 요청(Redo) — 직전 산출물이 목표에 못 미쳐 되돌아왔습니다] 고칠 구체적 결함: {body}\n"
                          f"[이 Task의 Goal] {goal}\n결함만 정확히 고치고 run으로 재검증해 그 증거와 함께 보고하세요.")
        else:
            frame = flow.comm.request(me_id, to, "pending", kind)   # 베턴 점유(alive→to)
            if kind == Kind.WORK:
                # 위임의 '계약'은 리더가 매번 새로 쓰는 스펙이 아니라 팀 합의로 확정된 Goal이다(스펙 리파인
                # 루프=재요청의 뿌리를 끊는다). owner가 그 목표를 끝까지(구현+검증) 책임진다.
                owner_body = (f"[위임 — 이 목표를 끝까지 책임지는 owner는 당신입니다] 이 Task의 Goal: {goal}\n"
                              f"직접 구현하고 run으로 '목표가 충족됨'을 검증한 뒤(리더에게 되넘기지 말 것), "
                              f"그 실행 증거와 함께 간결히 보고하세요.\n[요청 맥락] {body}")
        thread_id = flow.current.thread_id
        # Owner = 그 일을 Work로 받은 동료(수신=소유). 선배정이 아니라 요청으로 owner가 떠오른다 —
        # 이 Task에 아직 owner가 없을 때 첫 Work-request 수신자가 책임자가 된다(중앙집권 방지).
        if kind == Kind.WORK and not flow.current.owner:
            flow.current.owner = to
            flow.current.status.owner = flow._info(to) or f"<@{to}>"
            await flow.refresh(flow.current)
        req = await g.send_request(thread_id, me_id, to, kind, body)
        frame.request_id = str(req)                              # 실제 메시지 id로 기록 갱신
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
        runs_before = flow.current.run_count if flow.current else 0
        acts_before = flow.act_count   # 위임 도중 owner(단일흐름이라 깨운 동료만 활성)가 실제로 일했는지 측정
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
        # 동료가 'turn 한도'로 미완 반환했나(Work) — 그러면 이 Task는 완료로 못 닫고(complete_task 거부),
        # 같은 owner에게 '이어서(continuation)' 재위임해 끝내야 한다(허위완료→다음 Task churn 차단). 미완은
        # delivered(accept)로 안 쳐서 respond 마커를 'incomplete'로 두면, 재위임이 Redo 한도에 안 걸린다
        # (이어가기는 '직전 결함 보완'이 아니라 '남은 작업 마저 하기'이므로 횟수 제한 없이 계속 가능).
        incomplete = (kind == Kind.WORK and not was_clarify and not failed
                      and "턴 한도 도달" in (result or ""))
        if kind == Kind.WORK and not was_clarify and flow.current:
            flow.current.owner_incomplete = incomplete
        # owner가 '위임 도중 실제로 일했나' — 단일흐름이라 깨운 동료(+그 하위)만 활성이므로 wake 전후
        # act_count(run/Write/Edit) 증가 = owner 작업. 거짓이면 owner는 깨어났지만 착수 전/계획만 하고
        # 곧장 반환한 것(허위완료의 씨앗). 이걸로 '검증된 인도'와 '빈 응답'을 가른다.
        owner_acted = flow.act_count > acts_before
        is_owner_work = (kind == Kind.WORK and not was_clarify and not failed and not incomplete
                         and flow.current is not None and to == flow.current.owner)
        # owner가 Work를 받고도 실작업(run/Write) 0회로 곧장 반환 = 착수 전/계획만 = '인도 아님'.
        premature = is_owner_work and not owner_acted
        if is_owner_work and owner_acted and _is_substantive(result):
            flow.current.owner_delivered = True   # 이 owner가 실작업+응답을 냈다 → complete_task 허용 근거
        try:
            await g.send_response(thread_id, to, req, result)
            await _react(g, thread_id, req, "⚠️" if failed else "✅")  # 상태=이모지(해소/실패)
            _dbg(f"{tag} {'⚠실패' if failed else ('…미완' if (incomplete or premature) else '✓응답')} len={len(result)}")
        finally:
            # 프레임 close = 베턴 복귀(누수 방지). 정상이면 alive==to 라 그대로 닫힌다. 미완·미착수(premature)는
            # 'accept'로 안 쳐서 delivered로 기록 안 함 → 같은 owner 재위임이 Redo 한도에 안 걸리고 '실제 첫 인도'로 성립.
            try:
                flow.comm.respond(to, "clarify" if was_clarify else
                                  ("incomplete" if (incomplete or premature) else "accept"), result)
            except CommError:
                # to의 중첩 하위요청이 응답 없이 끝나(크래시/이탈) 베턴이 to에 '굳은' 비정상 상황 →
                # me_id(요청자)가 다시 alive 될 때까지 위 프레임을 강제 close. 흐름 교착(굳음) 방지.
                if flow.log:
                    flow.log("baton_recover", me=me_id, stuck_alive=flow.comm.alive, to=to)
                guard = 0
                while (not flow.comm.done and flow.comm.alive != me_id
                       and flow.comm.open_requests and guard < 30):
                    flow.comm.escalate("베턴 굳음 안전복구")
                    guard += 1
        if failed:   # 동료 무응답/일시오류 → 리더가 직접 떠안지 말고 '재배정/recruit'로 분산 유지
            return _ok(f"[{to}] 응답 실패(무응답/일시오류). **직접 떠안지 마세요** — 같은 도메인의 다른 동료에게 "
                       f"재배정(request Work)하거나, 없으면 recruit로 풀에서 충원해 맡기세요(분산 유지). 같은 동료 "
                       f"재시도는 한 번만. ({result[:100]})")
        # owner가 깨어났지만 '실작업 없이'(run/Write/Edit 0회) 곧장 반환 = 아직 착수 전/계획만. 리더가 대신
        # 구현·완료하지 말 것(독점·허위완료의 정확한 진입점). 같은 owner에게 다시 맡겨 '검증된 산출물'을 받게
        # 안내한다. 이 응답은 캐시하지 않는다 → 같은 턴에 재위임해도 합쳐지지 않고 실제로 다시 깨운다.
        if premature:
            _dbg(f"{tag} ⚠owner 미착수(실작업 0)")
            if flow.log:
                flow.log("owner_no_work", to=to, seg=flow.leader_segment)
            return _ok(f"[{to} 응답] {result[:300]}\n\n[중요] {flow._info(to) or to}가 아직 산출물을 만들지 "
                       f"않았습니다(run/파일작성 0회 — 착수 전이거나 계획만). **당신이 대신 구현하거나 이 Task를 "
                       f"완료하지 마세요(독점·허위완료 금지).** 같은 owner에게 request(Work)로 다시 맡겨 'run으로 "
                       f"검증한 실제 산출물'을 받은 뒤 진행하세요. 정말 끝까지 무응답이면 recruit/재배정으로.")
        # 위임 응답엔 owner가 '직접 돌린 실행 증거(시스템 캡처)'를 붙여 돌려준다 — 위임자가 말이 아니라
        # 증거로 '검증 후 수락'할 수 있게(반사적 재요청 대신). owner가 이번에 run을 돌렸을 때만.
        receipt = ""
        if (kind == Kind.WORK and not was_clarify and flow.current
                and flow.current.run_count > runs_before and flow.current.evidence):
            receipt = f"\n[owner 실행 증거(시스템 캡처)] {flow.current.evidence[:300]}"
        flow.req_results[dupkey] = result   # 같은 턴 병렬 중복요청이 재사용할 응답 캐시(동료 재호출 방지)
        return _ok(f"[{to} 응답] {result[:600]}{receipt}")

    tools.append(request)

    @tool("recruit",
          "인원이 부족하거나 '새 직군'이 필요하면 채용한다. role=맡길 직군(예: 게임 기획자, UX 디자이너, "
          "사운드 — '예비' 인력을 이 직군으로 신규 채용). member=특정 동료 id/역할명(비우고 role만 주면 예비에서 "
          "자동 선발). reason=사유. 로스터에 없는 직군도 이렇게 런타임에 채용해 쓴다.",
          {"member": str, "role": str, "reason": str})
    async def recruit(args):
        if flow.current is None:
            return _ok("오류: 진행 중인 Task가 없습니다. 먼저 create_task로 Task를 여세요.")
        role_name = (args.get("role") or "").strip()
        spec = (args.get("member") or "").strip()
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
        hired = ""
        if role_name and (_is_spare(flow, mid) or not flow._info(mid)):
            flow.bot_info[mid] = role_name        # 예비를 요청 직군으로 '신규 채용'(런타임 직군 배정)
            flow.current.status.group = _group_of(flow, flow.current.team)
            hired = f" — '{role_name}'(으)로 신규 채용"
            fn = getattr(g, "set_nick", None)     # 디스코드 닉네임도 직군으로(가시성, best-effort)
            if fn and getattr(flow, "guild_id", None):
                try:
                    await fn(flow.guild_id, mid, role_name)
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
            assigned = _resolve_members(args.get("team", ""), flow, flow.pool)
            if assigned:
                flow.project_team = _uniq([flow.leader] + assigned)
            # 프로젝트는 내부 레지스트리에만 등록(채널 자체가 프로젝트 식별자 — 채널에 앵커 안 박음).
            if flow.register_project:
                flow.project_id = flow.register_project(flow.project_channel, args["name"])
            return _ok(f"project_channel={flow.project_channel} project_id={flow.project_id} "
                       f"프로젝트팀={flow._names(flow.project_team)}")
        tools.append(create_project)

        @tool("create_task",
              "Task '빈 껍데기'를 연다 — **Purpose도 비운 채 멤버만 배정**한다(리더가 할 일을 미리 못 박음 = 중앙집권 "
              "방지). 이후 **배정된 팀이 모여(request Info) Purpose(풀 문제)·Goal(성공기준)을 함께 정해 set_goal로 "
              "확정**한다. Owner는 그 일을 Work로 받은 동료가 된다(선배정 금지). members=이 Task를 함께 정하고 수행할 "
              "동료(비우면 프로젝트팀 전체).",
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
            # 첫 Task = '전원 기획 회의': 채용 풀 '전체'를 강제로 멤버에 넣는다(리더가 프로젝트팀을 좁혀
            # 일부를 빼두거나, 좁은 1인 Task로 후퇴하는 걸 차단 — 정의는 놀던 인력까지 모두 모여서). 풀 전원을
            # 프로젝트 팀에도 반영해 이후 구현 Task에서도 쓸 수 있게 한다. 구현 Task는 owner 중심으로 좁혀도 됨.
            is_first = not flow.tasks
            if is_first:
                # 직군이 정해진 동료 전원 강제(놀던 인력 포함). '예비'(직군 미배정)는 제외 — 필요할 때
                # recruit(role=…)로 그 직군을 채용해 합류시킨다(전원 기획엔 실제 직군 보유자만).
                base = [m for m in flow.pool if m != flow.leader and not _is_spare(flow, m)]
                flow.project_team = _uniq([flow.leader] + base)         # 프로젝트 팀에도 반영
            else:
                base = picked if picked else [m for m in flow.project_team if m != flow.leader]
            team = _uniq([flow.leader] + base)
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
            tag0 = "전원 기획 회의(모두 모여 정의)" if is_first else "빈 껍데기"
            return _ok(f"task={tid} ({tag0}) thread={thread_id} 팀={flow._names(team)} — 배정된 팀 **전원**에게 "
                       f"request(Info)로 'Purpose(풀 문제)·Goal(성공기준)·각자 도메인 할 일'을 물어 함께 정한 뒤 "
                       f"set_goal로 확정하세요(전원 협의 전엔 set_goal 거부됨). 그 다음 일을 맡길 동료에게 Work로 위임.")
        tools.append(create_task)

        @tool("set_goal",
              "팀 회의로 정한 이번 Task의 **Purpose(풀 문제)와 Goal(측정가능한 성공기준)**을 확정·기록한다. 리더 "
              "단독/선지정 금지 — **이 Task의 멤버 전원**에게 request(Info)로 'Purpose·네 도메인의 목표·성공기준'을 "
              "물어 수렴한 결과를 적는다. Goal엔 '무엇이 되면 성공인가'(결과·시나리오)만 쓰고 '어떤 파일·엔드포인트·"
              "스택으로 만들지'(구현 방법)는 쓰지 말 것 — 그건 owner가 정한다. Work 위임은 확정 뒤에만 가능.",
              {"purpose": str, "goal": str})
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
                           f"아직 의견을 안 받은 멤버: {flow._names(missing)} — 그들에게 request(Info)로 '이 Task에서 "
                           f"풀 문제·네 도메인의 목표·성공기준'을 먼저 물어 수렴한 뒤 set_goal로 기록하세요(파일·"
                           f"엔드포인트 같은 구현 스펙 말고 '측정가능한 결과'로).")
            if purpose:
                flow.current.status.purpose = purpose
            flow.current.status.goal = goal
            await flow.refresh(flow.current)
            return _ok(f"task={flow.current.task_id} 정의 확정 — Purpose: {purpose[:50] or '(유지)'} / Goal: {goal[:80]}")
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
            done_ref = flow.current
            # 허위보고 차단(도메인 무관): 완료의 '진짜'는 에이전트 산문이 아니라 시스템이 캡처한 실행 영수증.
            # 코드는 합격/불합격을 판단하지 않고(하드코딩·QA역할 가정 X), 보고 옆에 실제 출력을 떼어낼 수 없게 묶는다.
            report = (args.get("result") or "")[:300]
            done_ref.status.status = "완료"
            done_ref.status.result = (
                f"[보고] {report}\n"
                f"[시스템 실행기록 {done_ref.run_count}회·마지막] {done_ref.evidence or '(없음)'}"
            )[:1400]
            await flow.refresh(done_ref)
            await _react(g, flow.project_channel, done_ref.block_id, "✅")  # 완료=이모지
            flow.current = None
            return _ok(f"task={done_ref.task_id} 완료 마감 (시스템 실행기록 {done_ref.run_count}회 첨부)")
        tools.append(complete_task)

        @tool("deploy",
              "검증을 마친 산출물을 실제로 공개 배포한다(GitHub push + Render 웹서비스 생성/갱신). "
              "name=영문 소문자·하이픈 서비스명(예: slither-multiplayer). 라이브 URL을 반환. "
              "Node 앱이어야 하고 서버는 process.env.PORT를 사용해야 함. run 검증을 끝낸 뒤 마지막에 호출.",
              {"name": str})
        async def deploy(args):
            # 프로젝트에 정식 서비스명(DEPLOY_NAME)이 설정돼 있으면 그걸로 '고정' — 에이전트가 임의 이름으로
            # 엉뚱한 새 서비스에 배포하는 사고를 구조적으로 차단(라이브가 안 바뀌는 원인이었음).
            name = (os.environ.get("DEPLOY_NAME", "").strip()
                    or re.sub(r"[^a-z0-9-]", "-", str(args.get("name", "")).lower()).strip("-")
                    or "organt-app")
            gh, ghu = os.environ.get("GH_PAT"), os.environ.get("GH_USER")
            rk, owner = os.environ.get("RENDER_KEY"), os.environ.get("RENDER_OWNER")
            if not (gh and ghu and rk and owner):
                return _ok("배포 불가: 배포 자격증명(GH_PAT/GH_USER/RENDER_KEY/RENDER_OWNER)이 설정되지 않았습니다.")
            if not getattr(flow, "workspace", None):
                return _ok("배포 불가: 작업공간이 없습니다.")
            from .deploy import deploy_sync
            result = await anyio.to_thread.run_sync(deploy_sync, flow.workspace, name, gh, ghu, rk, owner)
            flow.deployed = result                 # 배포 호출됨 기록(SYS의 배포 강제가 중복 안 하게)
            await _note(f"[배포] {result}")
            return _ok(result)
        tools.append(deploy)

    return tools


def build_guide_server(flow: Flow, me_id: int, role: str):
    return create_sdk_mcp_server("guide", "1.0.0", make_guide_tools(flow, me_id, role))
