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
        # 첫 요청이 길게(중첩 협의) 걸려도 베턴은 결국 돌아오므로 위임이 끊기지 않는다(이전엔 여기서 거부해
        # 리더가 '요청이 막혔다'고 오판→독점하는 역효과가 났다). 데드라인은 교착 안전장치.
        deadline = time.monotonic() + 600
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
        runs_before = flow.current.run_count if flow.current else 0
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
        try:
            await g.send_response(thread_id, to, req, result)
            await _react(g, thread_id, req, "⚠️" if failed else "✅")  # 상태=이모지(해소/실패)
            _dbg(f"{tag} {'⚠실패' if failed else '✓응답'} len={len(result)}")
        finally:
            # 프레임 close = 베턴 복귀(누수 방지). 정상이면 alive==to 라 그대로 닫힌다.
            try:
                flow.comm.respond(to, "clarify" if was_clarify else "accept", result)
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
        if failed:   # 실패는 답으로 넘기지 않고 재요청을 유도
            return _ok(f"[{to}] 일시 오류로 응답 실패 — 잠시 후 다시 request 하세요. ({result[:120]})")
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
          "인원이 부족하면 채용 풀에서 동료를 현재 Task 팀에 합류시킨다(member=id 또는 역할명, reason).",
          {"member": str, "reason": str})
    async def recruit(args):
        cand = _resolve_members(args.get("member", ""), flow, flow.pool)
        if not cand:
            return _ok(f"'{args.get('member','')}'를 채용 풀에서 못 찾음. 풀: {flow._names(flow.pool)}")
        if flow.current is None:
            return _ok("오류: 진행 중인 Task가 없습니다.")
        mid = cand[0]
        if mid not in flow.project_team:
            flow.project_team.append(mid)
        if mid not in flow.current.team:
            flow.current.team.append(mid)
            flow.current.status.group = _group_of(flow, flow.current.team)
            await flow.refresh()
            await _add_members(g, flow.current.thread_id, [mid])   # 스레드에 합류(멤버십=팀)
        return _ok(f"{flow._info(mid) or mid} 합류(사유: {args.get('reason', '')}). "
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
              "Task 공간을 연다 — **Purpose(풀어야 할 문제)만** 부여한다. Goal·Owner는 미리 못 정한다(중앙집권 방지): "
              "**Goal은 Task 안에서 팀과 request(Info)로 합의한 뒤 set_goal로 확정**하고, **Owner는 그 일을 Work로 "
              "받은 동료가 된다**(선배정 금지). members=함께 논의·작업할 동료(비우면 프로젝트팀 전체).",
              {"purpose": str, "members": str})
        async def create_task(args):
            if flow.current is not None and flow.current.status.status != "완료":
                return _ok(f"현재 Task({flow.current.task_id}: {flow.current.status.purpose[:24]})가 아직 "
                           f"'진행'입니다 — 단일흐름은 한 번에 Task 하나만. complete_task로 먼저 마감한 뒤 "
                           f"다음 Task를 여세요(여러 산출물도 하나씩 순차로).")
            ch = flow.project_channel or flow.user_channel
            tid = flow.next_task_id()
            pool = flow.project_team or flow.pool
            picked = _resolve_members(args.get("members", ""), flow, pool)
            base = picked if picked else [m for m in flow.project_team if m != flow.leader]
            team = _uniq([flow.leader] + base)
            # Goal·Owner는 비워둔다 — Goal은 팀 합의(set_goal)로, Owner는 Work-request 수신으로 떠오른다
            # (판 걸 때 업무를 분배하던 중앙집권 구조 제거: 분배의 출처 = 실제 요청).
            status = TaskStatus(task_id=tid, purpose=args["purpose"], status="진행",
                                goal="", owner="", group=_group_of(flow, team))
            block_id, thread_id = await g.open_task(ch, status)
            await _add_members(g, thread_id, [m for m in team if m != flow.leader])  # 멤버십=팀
            flow.project_channel = ch
            ref = TaskRef(task_id=tid, thread_id=thread_id, block_id=block_id,
                          status=status, team=team, owner=0)
            flow.tasks.append(ref)
            flow.current = ref
            flow.comm.reset_task_tracking()   # 새 산출물 단위 → '완료/Redo' 추적 초기화(Redo는 같은 Task 안에서만)
            return _ok(f"task={tid} thread={thread_id} 팀={flow._names(team)} — 이제 팀과 request(Info)로 "
                       f"Goal을 합의해 set_goal로 확정하고, 일을 맡길 동료에게 Work로 요청하세요(받은 동료가 owner).")
        tools.append(create_task)

        @tool("set_goal",
              "팀과 합의된 이번 Task의 **측정가능한 Goal**을 확정·기록한다(상태블록 갱신). 혼자 정하지 말고 동료와 "
              "request(Info)로 합의한 결과를 적으세요 — '목표는 팀 합의의 산물'을 보장하는 자리이며, Work 위임은 "
              "Goal 확정 뒤에만 가능합니다.",
              {"goal": str})
        async def set_goal(args):
            if flow.current is None:
                return _ok("오류: 진행 중인 Task가 없습니다. create_task로 먼저 여세요.")
            goal = (args.get("goal") or "").strip()
            if not goal:
                return _ok("오류: goal이 비었습니다.")
            # 목표는 팀 합의의 산물(docs: Team이 Goal을 정한다). 팀과 Info 협의 없이 리더 독단 지정 차단.
            discussed = any(
                ev[0] == "request" and ev[1] == me_id and ev[2] != me_id
                and str(getattr(ev[4], "value", ev[4])).lower().startswith("i")
                for ev in flow.comm.history)
            if not discussed:
                return _ok("목표 지정 거부: 목표는 팀 합의의 산물입니다(리더 독단 금지). 먼저 관련 동료에게 "
                           "request(Info)로 '이 요청에서 네 도메인의 목표·범위'를 물어 합의한 뒤, 그 합의를 "
                           "set_goal로 기록하세요.")
            flow.current.status.goal = goal
            await flow.refresh(flow.current)
            return _ok(f"task={flow.current.task_id} Goal 확정: {goal[:100]}")
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
