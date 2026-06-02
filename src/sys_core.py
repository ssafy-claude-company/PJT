"""SYS — 흐름 제어자 (Organt 주도 모델).

User 입력 → SYS가 담당 Organt에게 흐름으로 전달 → Organt가 *스스로 판단*해서
Guide 도구(answer/create_project/create_task/delegate/report)로 행동한다.

SYS는 얇다:
- 단일흐름 보존: 활성 흐름이 있으면 새 입력은 '조언(advice)'으로만 주입(새 흐름 X)
- 베턴/권한 강제는 Rule(CommunicationManager)·Hook가 담당
- 판단·행동은 Organt(지능)
"""
from typing import Dict, Optional

from .guide_tools import Flow


class Sys:
    def __init__(self, guide, guild_id: int, bot_info: Optional[Dict[int, str]] = None):
        self.guide = guide
        self.guild_id = guild_id
        self.bot_info = bot_info or {}
        self.active_flow: Optional[Flow] = None   # 단일흐름 lock
        self.flow_log = []

    def _log(self, event, **f):
        self.flow_log.append({"event": event, **f})

    def new_flow(self, channel_id, leader_id, teammates, root_id=None) -> Flow:
        flow = Flow(self.guide, channel_id, self.guild_id, leader_id, teammates, self.bot_info)
        if root_id is not None:
            flow.start_root(root_id)
        return flow

    async def handle_user_input(self, channel_id, leader_id, teammates, user_text,
                                leader_factory, root_id=None) -> dict:
        # 단일흐름 보존: 활성 흐름 중이면 조언으로 주입(새 흐름/Response 없음)
        if self.active_flow is not None and not self.active_flow.done:
            self.active_flow.advice.append(user_text)
            self._log("advice", text=user_text)
            return {"mode": "advice", "flow": self.active_flow}

        flow = self.new_flow(channel_id, leader_id, teammates, root_id)
        self.active_flow = flow
        organt = leader_factory(flow)   # 담당 Organt(가이드 도구 장착)
        prompt = (
            f"User 요청: {user_text}\n팀원 ID: {list(teammates)}\n\n"
            f"먼저 이 요청이 '단순 질문/인사이트'인지 '실작업이 필요한 Project'인지 판단하세요.\n"
            f"- 단순 질문/인사이트면 answer_question 으로 그 자리에서 답하고 끝냅니다.\n"
            f"- 실작업이 필요한 Project면 create_project → create_task 한 뒤, 각 작업을 팀원에게 "
            f"delegate(자기 자신에게는 금지) 하고, 끝나면 report 로 보고합니다.\n"
            f"불필요한 단계 없이 판단에 맞게 도구를 사용하세요."
        )
        await organt.handle(prompt)
        self._log("flow_done", project=flow.project_channel is not None, comm_done=flow.comm.done)
        self.active_flow = None
        return {"mode": "flow", "flow": flow}
