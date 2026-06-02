"""Organt가 판단해서 쓰는 Guide 도구셋 (MCP).

담당 Organt(LLM)가 User 요청을 받아 *스스로 판단*하고 행동하는 도구들이다.
- answer_question : 단순 질문/인사이트 → 그 채널에 바로 답변(흐름 종료)
- create_project  : Project로 판단될 때 전용 채널 생성(Guide 실행)
- create_task     : Project 채널에 Task(Thread)+[Task-XXX] 상태블록 생성
- delegate        : 팀원 Organt에게 Work 위임(베턴; 팀원 LLM이 실작업, 결과 회수)
- report          : 보고(유저 채널) + 흐름 종료

SYS가 단일흐름·베턴·권한을 강제한다. 실행은 DiscordGuide(system bot/각 봇).
"""
import time
from typing import Dict, List, Optional

from claude_agent_sdk import create_sdk_mcp_server, tool

from .communication import CommunicationManager
from .protocol import Kind, TaskStatus

ORIGIN = 0
TOOL_NAMES = [f"mcp__guide__{n}" for n in
              ("answer_question", "create_project", "create_task", "delegate", "report")]


class Flow:
    """하나의 활성 흐름 상태 (단일흐름 보존)."""

    def __init__(self, guide, channel_id: int, guild_id: int, leader_id: int,
                 teammates: Dict[int, object], bot_info: Optional[Dict[int, str]] = None):
        self.guide = guide
        self.user_channel = channel_id      # 유저가 요청한 채널
        self.guild_id = guild_id
        self.leader = leader_id
        self.teammates = teammates          # {id: Organt}
        self.bot_info = bot_info or {}
        self.task_id = time.strftime("%H%M%S")   # 유니크(중복 [Task-1] 방지)
        self.comm = CommunicationManager(ORIGIN)
        self.project_channel: Optional[int] = None
        self.thread_id: Optional[str] = None
        self.block_id: Optional[str] = None
        self.status: Optional[TaskStatus] = None
        self.done = False
        self.final: Optional[str] = None
        self.advice: List[str] = []         # 활성 중 유저 개입(조언)
        self.root_id: Optional[str] = None  # 유저 요청 메시지 ID(답글 대상)

    def start_root(self, root_id):
        self.root_id = str(root_id)
        self.comm.request(ORIGIN, self.leader, root_id, Kind.WORK)

    async def refresh(self):
        if self.status and self.project_channel and self.block_id:
            await self.guide.update_status(self.project_channel, self.block_id, self.status)

    def _info(self, oid):
        return self.bot_info.get(oid, "")


def make_guide_tools(flow: Flow):
    g = flow.guide

    @tool("answer_question", "단순 질문/인사이트에 그 채널에서 바로 답한다(흐름 종료)", {"body": str})
    async def answer_question(args):
        await g.post(flow.user_channel, flow.leader, f"[Response]\nBody: {args['body']}",
                     reply_to=flow.root_id)
        if not flow.comm.done:
            flow.comm.respond(flow.leader, "accept", args["body"])
        flow.done, flow.final = True, args["body"]
        return {"content": [{"type": "text", "text": "답변 게시·흐름 종료"}]}

    @tool("create_project", "Project로 판단될 때 전용 채널을 1개 생성한다", {"name": str})
    async def create_project(args):
        if flow.project_channel is not None:   # 흐름당 1회(채널 남발 방지)
            return {"content": [{"type": "text", "text": f"이미 project_channel={flow.project_channel}"}]}
        cid = await g.create_project_channel(flow.guild_id, args["name"])
        flow.project_channel = cid
        return {"content": [{"type": "text", "text": f"project_channel={cid}"}]}

    @tool("create_task", "Project 채널에 Task(Thread)+상태블록을 1개 만든다", {"purpose": str, "goal": str})
    async def create_task(args):
        if flow.thread_id is not None:         # 흐름당 1회([Task-XXX] 중복 방지)
            return {"content": [{"type": "text", "text": f"이미 Task 생성됨(thread={flow.thread_id})"}]}
        ch = flow.project_channel or flow.user_channel
        flow.status = TaskStatus(task_id=flow.task_id, purpose=args["purpose"], status="진행",
                                 goal=args["goal"],
                                 group=[(f"<@{flow.leader}>", flow._info(flow.leader) or "leader")])
        flow.block_id, flow.thread_id = await g.open_task(ch, flow.status)
        flow.project_channel = ch
        return {"content": [{"type": "text", "text": f"task={flow.task_id} thread={flow.thread_id}"}]}

    @tool("delegate", "팀원에게 Work를 위임한다(팀원이 실작업, 결과 회수)",
          {"member_id": int, "work": str})
    async def delegate(args):
        member, work = int(args["member_id"]), args["work"]
        if member == flow.leader or member not in flow.teammates:
            return {"content": [{"type": "text", "text": "오류: 자기 자신/비팀원에게는 위임 불가"}]}
        if flow.thread_id is None:
            return {"content": [{"type": "text", "text": "오류: create_task 먼저"}]}
        req = await g.send_request(flow.thread_id, flow.leader, member, Kind.WORK, work)
        flow.comm.request(flow.leader, member, req, Kind.WORK)      # 팀원 wake(베턴)
        result = await flow.teammates[member].handle(work)          # 팀원 LLM 실작업
        await g.send_response(flow.thread_id, member, req, result)
        flow.comm.respond(member, "accept", result)                # 베턴 복귀(Leader)
        if flow.status:
            mention = f"<@{member}>"
            if mention not in [m for m, _ in flow.status.group]:
                flow.status.group.append((mention, flow._info(member)))
            flow.status.status = "분배"
            await flow.refresh()
        return {"content": [{"type": "text", "text": f"[{member} 보고] {result[:200]}"}]}

    @tool("report", "Goal 완수 후 유저 채널에 최종 보고(흐름 종료)", {"body": str})
    async def report(args):
        await g.post(flow.user_channel, flow.leader, f"[Response]\nBody: {args['body']}",
                     reply_to=flow.root_id)
        if not flow.comm.done:
            flow.comm.respond(flow.leader, "accept", args["body"])
        if flow.status:
            flow.status.status = "보고"
            flow.status.result = args["body"]
            await flow.refresh()
        flow.done, flow.final = True, args["body"]
        return {"content": [{"type": "text", "text": "보고 게시·흐름 종료"}]}

    return [answer_question, create_project, create_task, delegate, report]


def build_guide_server(flow: Flow):
    return create_sdk_mcp_server("guide", "1.0.0", make_guide_tools(flow))
