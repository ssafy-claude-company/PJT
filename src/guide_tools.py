"""Organt가 쓰는 Guide 도구셋 (P2P Communication + 다중 Task 모델).

모든 깨어난 Organt는 `request`로 *필요한 동료 한 명*에게 요청할 수 있다(Info=질문/Work=작업).
SYS가 대상 동료를 중첩 베턴으로 깨워(flow.wake) 응답을 돌려준다 → 항상 1명만 활성(단일흐름).

리더(첫 Organt)는 추가로 Project(채널)와 **여러 개의 Task(스레드)** 를 만든다:
- create_project: 도메인 채널 1개
- create_task   : 채널에 [Task-XXX] 상태블록 + 대화 Thread를 만든다(원하는 만큼 반복)
- complete_task : 현재 Task의 상태블록을 완료로 마감(result 기록)
대화(Request/Response)는 '현재 Task' 스레드 안에서 일어난다. 보고는 별도 툴이 아니라
반환값(=Response)이 origin까지 unwind되는 것 자체다.
"""
import time
from dataclasses import dataclass
from typing import List, Optional

from claude_agent_sdk import create_sdk_mcp_server, tool

from .communication import CommError, CommunicationManager
from .protocol import Kind, TaskStatus

ORIGIN = 0
REQUEST_TOOL = "mcp__guide__request"
# 리더 전용 셋업 도구. 보고/답변은 별도 툴이 아니라 '반환값=Response'로 처리된다.
LEADER_TOOLS = [f"mcp__guide__{n}" for n in ("create_project", "create_task", "complete_task")]


@dataclass
class TaskRef:
    """채널에 누적되는 Task 하나 (상태블록 + 대화 Thread)."""
    task_id: str
    thread_id: str
    block_id: str
    status: TaskStatus


class Flow:
    """하나의 활성 흐름(단일흐름 보존). 리더가 여러 Task를 순차로 연다. wake로 동료를 중첩 호출."""

    def __init__(self, guide, channel_id, guild_id, leader_id, bot_info=None):
        self.guide = guide
        self.user_channel = channel_id
        self.guild_id = guild_id
        self.leader = leader_id
        self.bot_info = bot_info or {}
        self.comm = CommunicationManager(ORIGIN)
        self.project_channel: Optional[int] = None
        self.tasks: List[TaskRef] = []      # 채널에 만든 Task들(누적)
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


def _ok(text):
    return {"content": [{"type": "text", "text": text}]}


def make_guide_tools(flow: Flow, me_id: int, role: str):
    g = flow.guide
    tools = []

    @tool("request", "필요한 동료 한 명에게 요청(kind: Info=질문 / Work=작업, to_id는 문자열)",
          {"to_id": str, "kind": str, "body": str})
    async def request(args):
        to = int(args["to_id"])
        kind = Kind.WORK if str(args["kind"]).strip().lower().startswith("w") else Kind.INFO
        body = args["body"]
        if flow.current is None:
            return _ok("오류: 진행 중인 Task가 없습니다. (리더가 create_task 먼저)")
        if flow.wake is None:
            return _ok("오류: 시스템 준비 안 됨")
        try:
            flow.comm.check_request(me_id, to, kind)   # 게시 전 베턴 검증(from==to·Work busy)
        except CommError as e:
            return _ok(f"요청 거부(규약): {e}")
        thread_id = flow.current.thread_id
        req = await g.send_request(thread_id, me_id, to, kind, body)
        flow.comm.request(me_id, to, req, kind)
        try:
            result = await flow.wake(to, body, kind)   # 동료 깨워 응답(중첩 베턴)
        except Exception as e:                          # 동료가 실패해도 베턴은 반드시 복귀
            result = f"(동료 처리 중 오류: {e})"
        try:
            await g.send_response(thread_id, to, req, result)
        finally:
            flow.comm.respond(to, "accept", result)    # 프레임 close = 베턴 복귀(누수 방지)
        status = flow.current.status
        mention = f"<@{to}>"
        if mention not in [m for m, _ in status.group]:
            status.group.append((mention, flow._info(to)))
            await flow.refresh()
        return _ok(f"[{to} 응답] {result[:600]}")

    tools.append(request)

    if role == "leader":
        @tool("create_project", "Project로 판단되면 전용 채널을 1개 생성", {"name": str})
        async def create_project(args):
            if flow.project_channel is not None:
                return _ok(f"이미 project_channel={flow.project_channel}")
            flow.project_channel = await g.create_project_channel(flow.guild_id, args["name"])
            return _ok(f"project_channel={flow.project_channel}")
        tools.append(create_project)

        @tool("create_task",
              "Task(Thread)+상태블록을 새로 1개 생성. 프로젝트를 여러 Task로 나눠 반복 호출 가능. "
              "Goal은 측정가능하게.", {"purpose": str, "goal": str})
        async def create_task(args):
            ch = flow.project_channel or flow.user_channel
            tid = flow.next_task_id()
            status = TaskStatus(task_id=tid, purpose=args["purpose"], status="진행",
                                goal=args["goal"],
                                group=[(f"<@{flow.leader}>", flow._info(flow.leader) or "leader")])
            block_id, thread_id = await g.open_task(ch, status)
            flow.project_channel = ch
            ref = TaskRef(task_id=tid, thread_id=thread_id, block_id=block_id, status=status)
            flow.tasks.append(ref)
            flow.current = ref
            return _ok(f"task={tid} thread={thread_id} (현재 Task로 설정)")
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
