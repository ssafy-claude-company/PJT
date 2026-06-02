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
import time
from dataclasses import dataclass, field
from typing import List, Optional

import anyio

from claude_agent_sdk import create_sdk_mcp_server, tool

from .communication import CommError, CommunicationManager
from .protocol import Kind, TaskStatus

ORIGIN = 0
REQUEST_TOOL = "mcp__guide__request"
RECRUIT_TOOL = "mcp__guide__recruit"
# 모든 Organt 공통 흐름 도구(요청/채용). 리더 전용 셋업 도구는 LEADER_TOOLS.
FLOW_TOOLS = [REQUEST_TOOL, RECRUIT_TOOL]
LEADER_TOOLS = [f"mcp__guide__{n}" for n in ("create_project", "create_task", "complete_task")]


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


@dataclass
class TaskRef:
    """채널에 누적되는 Task 하나 (상태블록 + 대화 Thread + 배정 팀)."""
    task_id: str
    thread_id: str
    block_id: str
    status: TaskStatus
    team: List[int] = field(default_factory=list)   # 이 Task에 배정된 Organt들


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
        self.wake = None   # async (to_id, body, kind) -> result text  (SYS가 주입)

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
            print(f"{tag} ✗거부:Task없음", flush=True)
            return _ok("오류: 진행 중인 Task가 없습니다. (리더가 create_task 먼저)")
        if to not in flow.current.team:
            print(f"{tag} ✗거부:팀밖 팀={flow._names(flow.current.team)}", flush=True)
            return _ok(f"요청 거부: {to}({flow._info(to)})는 이 Task 팀이 아닙니다. "
                       f"먼저 recruit로 합류시키세요. 현재 팀: {flow._names(flow.current.team)}")
        if flow.wake is None:
            return _ok("오류: 시스템 준비 안 됨")
        # 병렬 요청 직렬화: 베턴이 '내 차례'가 될 때까지 대기(거부 대신 큐잉). 단일흐름 보존.
        deadline = time.monotonic() + 120
        while flow.comm.alive != me_id and not flow.comm.done and time.monotonic() < deadline:
            await anyio.sleep(0.05)
        # 검증→점유는 await 없이 인접 실행 → 형제 요청과 경합하지 않음(원자적).
        try:
            flow.comm.check_request(me_id, to, kind)
        except CommError as e:
            print(f"{tag} ✗거부:규약 ({e})", flush=True)
            await _note(f"{flow._info(to) or to}에게 요청했으나 거부됨 — {e}")
            return _ok(f"요청 거부(규약): {e}")
        frame = flow.comm.request(me_id, to, "pending", kind)   # 베턴 점유(alive→to)
        thread_id = flow.current.thread_id
        req = await g.send_request(thread_id, me_id, to, kind, body)
        frame.request_id = str(req)                              # 실제 메시지 id로 기록 갱신
        print(f"{tag} ✓전송 req={req}", flush=True)
        try:
            result = await flow.wake(to, body, kind)   # 동료 깨워 응답(중첩 베턴)
        except Exception as e:                          # 동료가 실패해도 베턴은 반드시 복귀
            result = f"(동료 처리 중 오류: {e})"
        try:
            resp = await g.send_response(thread_id, to, req, result)
            print(f"{tag} ✓응답 resp={resp} len={len(result)}", flush=True)
        finally:
            flow.comm.respond(to, "accept", result)    # 프레임 close = 베턴 복귀(누수 방지)
        return _ok(f"[{to} 응답] {result[:600]}")

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
        return _ok(f"{flow._info(mid) or mid} 합류(사유: {args.get('reason', '')}). "
                   f"현재 팀: {flow._names(flow.current.team)}")

    tools.append(recruit)

    if role == "leader":
        @tool("create_project",
              "Project로 판단되면 전용 채널 생성 + 규모를 산정해 팀 배정"
              "(team=쉼표구분 동료 id/역할명, 리더 제외분). 비우면 풀 전체.",
              {"name": str, "team": str})
        async def create_project(args):
            if flow.project_channel is not None:
                return _ok(f"이미 project_channel={flow.project_channel}")
            flow.project_channel = await g.create_project_channel(flow.guild_id, args["name"])
            assigned = _resolve_members(args.get("team", ""), flow, flow.pool)
            if assigned:
                flow.project_team = _uniq([flow.leader] + assigned)
            return _ok(f"project_channel={flow.project_channel} "
                       f"프로젝트팀={flow._names(flow.project_team)}")
        tools.append(create_project)

        @tool("create_task",
              "Task 생성 + 필요한 인원 배정(members=쉼표구분 id/역할명, 프로젝트팀 내). "
              "부족하면 나중에 recruit. Goal은 측정가능하게.",
              {"purpose": str, "goal": str, "members": str})
        async def create_task(args):
            ch = flow.project_channel or flow.user_channel
            tid = flow.next_task_id()
            picked = _resolve_members(args.get("members", ""), flow, flow.project_team or flow.pool)
            team = _uniq([flow.leader] + (picked if picked
                                          else [m for m in flow.project_team if m != flow.leader]))
            status = TaskStatus(task_id=tid, purpose=args["purpose"], status="진행",
                                goal=args["goal"], group=_group_of(flow, team))
            block_id, thread_id = await g.open_task(ch, status)
            flow.project_channel = ch
            ref = TaskRef(task_id=tid, thread_id=thread_id, block_id=block_id,
                          status=status, team=team)
            flow.tasks.append(ref)
            flow.current = ref
            return _ok(f"task={tid} thread={thread_id} 팀={flow._names(team)}")
        tools.append(create_task)

        @tool("complete_task",
              "현재 Task의 목표가 충족되면 상태블록을 완료로 마감(result 기록). 다음 Task는 create_task로.",
              {"result": str})
        async def complete_task(args):
            if flow.current is None:
                return _ok("오류: 진행 중인 Task가 없습니다.")
            done_ref = flow.current
            done_ref.status.status = "완료"
            done_ref.status.result = (args.get("result") or "")[:500]
            await flow.refresh(done_ref)
            flow.current = None
            return _ok(f"task={done_ref.task_id} 완료 마감")
        tools.append(complete_task)

    return tools


def build_guide_server(flow: Flow, me_id: int, role: str):
    return create_sdk_mcp_server("guide", "1.0.0", make_guide_tools(flow, me_id, role))
