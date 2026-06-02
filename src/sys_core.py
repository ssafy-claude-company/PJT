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
        peers = ", ".join(f"{i}({self.bot_info.get(i, '?')})" for i in self.bot_info if i != me)
        my_role = self.bot_info.get(me, "리더" if role == "leader" else "팀원")
        if role == "leader":
            return (
                f"당신은 총괄 리더입니다. 당신의 역할: {my_role}\nUser 요청: {body}\n동료: {peers}\n\n"
                f"먼저 '단순 질문/인사이트'인지 '실작업 Project'인지 판단하세요.\n"
                f"- 단순 질문/인사이트 → 그냥 답을 작성해 반환하면 됩니다(그게 사용자 응답).\n"
                f"- 실작업 Project → create_project → create_task 후, **당신 역할 부분은 직접 파일로 작업**, "
                f"나머지는 **역할에 맞는 동료**에게 request(kind=Work)로 위임.\n"
                f"통합 규격은 직접 정해 전달하거나 동료끼리 request(kind=Info)로 협의하게 하세요.\n"
                f"모두 끝나면 결과 요약을 **반환**하세요 — 그 반환값이 곧 사용자 보고(Response)입니다. "
                f"(별도 보고 도구는 없습니다.)"
            )
        return (
            f"당신은 팀원입니다. 당신의 역할: {my_role}\n받은 요청({getattr(kind, 'value', kind)}): {body}\n"
            f"동료: {peers}\n\n"
            f"진행에 필요한 정보(다른 파트의 규격·산출물)가 있으면 그 정보를 가진 동료에게 "
            f"request(kind=Info)로 물어 합의한 뒤 작업하세요. 파일은 작업공간에 상대경로로 만드세요.\n"
            f"끝나면 결과(또는 답)를 간결히 **반환**하세요 — 그 반환값이 곧 요청자에게 가는 응답(Response)입니다. "
            f"보고하려고 request 를 쓰지 마세요."
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
        result = await self.run_turn(flow, leader_id, user_text, Kind.WORK, "leader")
        # 리더의 반환값 = 사용자에게 가는 Response(=보고). origin 프레임을 닫아 시작점 복귀.
        await self.guide.post(flow.user_channel, leader_id, f"[Response]\nBody: {result}",
                              reply_to=flow.root_id)
        if not flow.comm.done:
            flow.comm.respond(leader_id, "accept", result)
        flow.done, flow.final = True, result
        if flow.status:
            flow.status.status = "완료"
            flow.status.result = (result or "")[:500]
            await flow.refresh()
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
