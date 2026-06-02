"""SYS — Organt 주도 + P2P Communication.

User 입력 → SYS가 담당(리더)을 깨움 → Organt가 판단·행동(파일/Guide 도구).
필요하면 어떤 Organt든 `request`로 동료를 부르고, SYS가 그 동료를 중첩 베턴으로
깨워(run_turn) 응답을 돌려준다. 항상 1명만 활성(단일흐름) → 사이드이펙트·토큰 절약.

SYS는 얇다: 깨우기(wake) 제공 + 단일흐름 lock + 라우팅. 베턴/권한 강제는 Rule·Hook.
Organt 생성(모델·권한·State)은 organt_builder로 주입받는다.
"""
from typing import Dict, Optional

from .guide_tools import Flow, build_guide_server
from .protocol import Kind, Request


class Sys:
    def __init__(self, guide, guild_id, organt_builder, bot_info: Optional[Dict[int, str]] = None):
        self.guide = guide
        self.guild_id = guild_id
        self.organt_builder = organt_builder   # (organt_id, guide_server, role) -> Organt
        self.bot_info = bot_info or {}
        self.active_flow: Optional[Flow] = None
        self.flow_log = []

    def _log(self, event, **f):
        self.flow_log.append({"event": event, **f})

    def _prompt(self, body, kind, role, me):
        peers = [i for i in self.bot_info if i != me]
        if role == "leader":
            return (
                f"당신은 총괄 리더입니다. User 요청: {body}\n동료 ID: {peers}\n\n"
                f"먼저 이게 '단순 질문/인사이트'인지 '실작업 Project'인지 판단하세요.\n"
                f"- 단순 질문/인사이트 → answer_question 으로 그 자리에서 답하고 끝.\n"
                f"- 실작업 Project → create_project → create_task 후, 필요한 동료에게 "
                f"request(kind=Work)로 위임(직접 일부 수행도 가능), 끝나면 report.\n"
                f"통합이 필요한 작업은 동료들이 request(kind=Info)로 서로 규격을 합의하도록 맡기거나 "
                f"당신이 규격을 정해 전달하세요."
            )
        return (
            f"당신은 팀원입니다. 받은 요청({getattr(kind, 'value', kind)}): {body}\n동료 ID: {peers}\n\n"
            f"진행에 필요한 정보(예: 다른 파트의 규격)가 있으면 그 정보를 가진 동료에게 "
            f"request(kind=Info)로 물어 합의한 뒤 작업하세요. 파일은 작업공간에 상대경로로 만들고, "
            f"결과(또는 답)를 간결히 반환하세요."
        )

    async def run_turn(self, flow: Flow, organt_id, body, kind, role) -> str:
        server = build_guide_server(flow, organt_id, role)
        organt = self.organt_builder(organt_id, server, role)
        return await organt.handle(self._prompt(body, kind, role, organt_id))

    async def handle_user_input(self, channel_id, leader_id, user_text, root_id=None) -> dict:
        # 단일흐름 보존: 활성 흐름 중이면 조언으로만 주입(새 흐름/Response 없음)
        if self.active_flow is not None and not self.active_flow.done:
            self.active_flow.advice.append(user_text)
            self._log("advice", text=user_text)
            return {"mode": "advice", "flow": self.active_flow}

        flow = Flow(self.guide, channel_id, self.guild_id, leader_id, self.bot_info)
        if root_id is not None:
            flow.start_root(root_id)
        flow.wake = lambda to, b, k: self.run_turn(flow, to, b, k, "member")
        self.active_flow = flow
        await self.run_turn(flow, leader_id, user_text, Kind.WORK, "leader")
        self._log("flow_done", project=flow.project_channel is not None, comm_done=flow.comm.done)
        self.active_flow = None
        return {"mode": "flow", "flow": flow}

    # --- 진짜 입구: 채널의 유저 형식 Request를 읽어 라우팅 ---

    async def read_latest_request(self, channel_id) -> Optional[Request]:
        msgs = await self.guide.read_thread(channel_id, limit=20)
        reqs = [m for m in msgs if isinstance(m, Request)]
        return reqs[-1] if reqs else None

    async def route_channel_request(self, channel_id, request: Request, root_id=None) -> dict:
        if request.to_id is None:
            self._log("ignored", reason="To 없음")
            return {"mode": "ignored"}
        return await self.handle_user_input(channel_id, request.to_id, request.body,
                                            root_id=request.message_id)
