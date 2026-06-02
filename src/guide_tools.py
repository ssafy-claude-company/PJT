"""Organt가 쓰는 Guide 도구셋 (P2P Communication 모델).

모든 깨어난 Organt는 `request`로 *필요한 동료 한 명*에게 요청할 수 있다(Info=질문/Work=작업).
SYS가 대상 동료를 중첩 베턴으로 깨워(flow.wake) 응답을 돌려준다 → 항상 1명만 활성(단일흐름).
리더(첫 Organt)는 추가로 project/task 생성·report·answer를 쓴다. 리더도 직접 파일 작업 가능.
"""
import time
from typing import Optional

from claude_agent_sdk import create_sdk_mcp_server, tool

from .communication import CommError, CommunicationManager
from .protocol import Kind, TaskStatus

ORIGIN = 0
REQUEST_TOOL = "mcp__guide__request"
# 리더 전용 셋업 도구. 보고/답변은 별도 툴이 아니라 '반환값=Response'로 처리된다.
LEADER_TOOLS = [f"mcp__guide__{n}" for n in ("create_project", "create_task")]


class Flow:
    """하나의 활성 흐름(단일흐름 보존). wake로 동료를 중첩 호출한다."""

    def __init__(self, guide, channel_id, guild_id, leader_id, bot_info=None):
        self.guide = guide
        self.user_channel = channel_id
        self.guild_id = guild_id
        self.leader = leader_id
        self.bot_info = bot_info or {}
        self.comm = CommunicationManager(ORIGIN)
        self.task_id = time.strftime("%H%M%S")
        self.project_channel: Optional[int] = None
        self.thread_id: Optional[str] = None
        self.block_id: Optional[str] = None
        self.status: Optional[TaskStatus] = None
        self.done = False
        self.final: Optional[str] = None
        self.root_id: Optional[str] = None
        self.advice = []
        self.wake = None   # async (to_id, body, kind) -> result text  (SYS가 주입)

    def start_root(self, root_id):
        self.root_id = str(root_id)
        self.comm.request(ORIGIN, self.leader, root_id, Kind.WORK)

    async def refresh(self):
        if self.status and self.project_channel and self.block_id:
            await self.guide.update_status(self.project_channel, self.block_id, self.status)

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
        if flow.thread_id is None:
            return _ok("오류: 아직 Task(스레드)가 없습니다. (리더가 create_task 먼저)")
        if flow.wake is None:
            return _ok("오류: 시스템 준비 안 됨")
        try:
            flow.comm.check_request(me_id, to, kind)   # 게시 전 베턴 검증(from==to·Work busy)
        except CommError as e:
            return _ok(f"요청 거부(규약): {e}")
        req = await g.send_request(flow.thread_id, me_id, to, kind, body)
        flow.comm.request(me_id, to, req, kind)
        result = await flow.wake(to, body, kind)       # 동료 깨워 응답(중첩 베턴)
        await g.send_response(flow.thread_id, to, req, result)
        flow.comm.respond(to, "accept", result)        # 베턴 복귀
        if flow.status:
            mention = f"<@{to}>"
            if mention not in [m for m, _ in flow.status.group]:
                flow.status.group.append((mention, flow._info(to)))
                await flow.refresh()
        return _ok(f"[{to} 응답] {result[:400]}")

    tools.append(request)

    if role == "leader":
        @tool("create_project", "Project로 판단되면 전용 채널을 1개 생성", {"name": str})
        async def create_project(args):
            if flow.project_channel is not None:
                return _ok(f"이미 project_channel={flow.project_channel}")
            flow.project_channel = await g.create_project_channel(flow.guild_id, args["name"])
            return _ok(f"project_channel={flow.project_channel}")
        tools.append(create_project)

        @tool("create_task", "Project 채널에 Task(Thread)+상태블록을 1개 생성", {"purpose": str, "goal": str})
        async def create_task(args):
            if flow.thread_id is not None:
                return _ok(f"이미 task thread={flow.thread_id}")
            ch = flow.project_channel or flow.user_channel
            flow.status = TaskStatus(task_id=flow.task_id, purpose=args["purpose"], status="진행",
                                     goal=args["goal"],
                                     group=[(f"<@{flow.leader}>", flow._info(flow.leader) or "leader")])
            flow.block_id, flow.thread_id = await g.open_task(ch, flow.status)
            flow.project_channel = ch
            return _ok(f"task={flow.task_id} thread={flow.thread_id}")
        tools.append(create_task)

    return tools


def build_guide_server(flow: Flow, me_id: int, role: str):
    return create_sdk_mcp_server("guide", "1.0.0", make_guide_tools(flow, me_id, role))
