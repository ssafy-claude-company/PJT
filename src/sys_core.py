"""SYS — Organt 주도 + P2P Communication.

User 입력 → SYS가 담당(리더)을 깨움 → Organt가 판단·행동(파일/Guide 도구).
필요하면 어떤 Organt든 `request`로 동료를 부르고, SYS가 그 동료를 중첩 베턴으로
깨워(run_turn) 응답을 돌려준다. 항상 1명만 활성(단일흐름) → 사이드이펙트·토큰 절약.

SYS는 얇다: 깨우기(wake) 제공 + 단일흐름 lock + 라우팅. 베턴/권한 강제는 Rule·Hook.
Organt 생성(모델·권한·State)은 organt_builder로 주입받는다.
"""
from typing import Dict, Optional

from .communication import CommError
from .guide_tools import Flow, build_guide_server
from .protocol import Kind, Request, format_response


class Sys:
    def __init__(self, guide, guild_id, organt_builder, bot_info: Optional[Dict[int, str]] = None):
        self.guide = guide
        self.guild_id = guild_id
        self.organt_builder = organt_builder   # (organt_id, guide_server, role) -> Organt
        self.bot_info = bot_info or {}
        self.active_flow: Optional[Flow] = None
        self.queue = []                        # 진행 중 들어온 명령(순차 처리 대기)
        self.flow_log = []

    def _log(self, event, **f):
        self.flow_log.append({"event": event, **f})

    # 모든 Organt 공통 원칙: 추론보다 검증, 소통으로 규약을 맞춘다.
    _PRINCIPLE = (
        "[원칙: 추론보다 검증] 다른 파트(동료의 규격·산출물·의도)에 대해 모르거나 가정이 필요한 "
        "순간, 추측해서 진행하지 마세요. 그 정보를 가진 동료에게 request(kind=Info)로 물어 "
        "확인하세요. 받은 답이 모호하거나 부족하면 다시 물어도 됩니다(재질문). 단, 진행에 "
        "꼭 필요한 것만 물으세요(불필요한 질문·정보 적재 금지).\n"
        "[규약은 합의로] 필드명·데이터 형태·API 경로·디자인 토큰 같은 인터페이스는 혼자 임의로 "
        "정하지 말고, 그걸 함께 쓰는 동료와 request(Info)로 합의해 정하세요. 동료 산출물은 "
        "Read/Glob로 직접 확인해 검증하세요.\n"
        "[멈춤 규칙] 당신에게 요청해 응답을 기다리는 '상위 동료'에게는 되물을 수 없습니다(그들은 "
        "멈춰 있음). 그 경우 그 동료의 산출물을 Read 하거나 멈춰있지 않은 다른 동료에게 물으세요.\n"
        "[작업공간 레이아웃] 모든 산출물은 작업공간 '루트' 기준 하나의 일관된 구조로 만드세요. "
        "중첩 프로젝트 폴더(todo-app/ 등) 만들지 말 것. 표준 경로: 백엔드 서버는 루트(server.js 또는 "
        "app.py), 프론트엔드는 public/(index.html·style.css·app.js), 스펙은 루트(api-spec.md·"
        "design-spec.md). 같은 산출물을 두 위치에 만들지 말고, 동료에게 위임할 땐 정확한 경로를 주세요.\n"
        "[보고] 결과는 간결한 일반 텍스트로 반환하세요 — 그 반환값이 곧 요청자에게 가는 Response. "
        "'---' 구분선/'✅ 완성' 배너/표/긴 머리말 같은 장식은 쓰지 말고, 보고하려고 request 쓰지 마세요."
    )

    def _prompt(self, body, kind, role, me):
        peers = ", ".join(f"{i}({self.bot_info.get(i, '?')})" for i in self.bot_info if i != me)
        my_role = self.bot_info.get(me, "리더" if role == "leader" else "팀원")
        if role == "leader":
            return (
                f"당신은 진행을 여는 '대표'입니다(독단으로 다 정하지 말 것). 당신의 역할: {my_role}\n"
                f"User 요청: {body}\n동료: {peers}\n\n"
                f"{self._PRINCIPLE}\n\n"
                f"[팀 구성 — 시작 시 먼저] 혼자 즉흥적으로 부르지 말고, **규모를 산정해 팀을 배정**하세요. "
                f"create_project(team=…)로 프로젝트 팀을, create_task(members=…)로 그 Task에 필요한 인원만 "
                f"배정(동료는 id 또는 역할명으로 지정). 진행 중 부족하면 recruit로 풀에서 더 데려오고, 넉넉하면 "
                f"다음 Task엔 적게. **request는 '현재 Task 팀' 안에서만** 됩니다(팀 밖이면 recruit 먼저).\n"
                f"[판단] 요청 성격을 보고 셋 중 하나로 처리하세요.\n"
                f"- '단순 질문/인사이트'(혼자 답 가능) → 답만 간결히 반환.\n"
                f"- '팀 논의/토론/선택'(여러 입장의 의견·정의·찬반·언어/안 선택) → create_project→"
                f"create_task 후 **진행자(주동자)로서 토론을 굴리세요**: ① 각 참가자에게 request(Info)로 "
                f"입장·논거를 받고 ② **한 참가자의 실제 주장을 다른 참가자(들)에게 그대로 전달**하며 반박/수용을 "
                f"요청해(2명이면 양자, 3명+면 교차로) 라운드 수는 고정 말고 '더 다툴 게 있나'를 당신이 판단해 "
                f"멈출 때까지 **실제 반박이 오가게** 하고 ③ 전제가 모호하다 반려되면 명확히 해 다시 묻고 "
                f"④ 충분히 다투면 **종합·조율**해 결론을 반환하세요. **요청이 '하나만 선택/결정'이면 반드시 "
                f"단일 결론을 근거와 함께 확정**(무승부·애매 종료 금지). 당신이 한 입장을 대표하더라도 선택은 "
                f"논거의 우열로 **공정하게**(자기 편 들지 말 것). 한 번씩만 묻고 끝내지 말 것.\n"
                f"- '실작업 Project' → create_project(채널 1개) 후 일을 **서로 겹치지 않는 Task들**로 "
                f"나눠 진행(보통 2~4개, 같은 작업 두 번 위임 금지).\n"
                f"각 Task: create_task(purpose, goal=측정가능) → 당신 몫은 직접 작업하되 "
                f"**가정이 생기면 동료에게 Info로 확인** → 나머지는 역할에 맞는 동료에게 request(Work)로 위임 → "
                f"동료 결과를 **Read로 검증**하고 미흡/불일치면 구체적 피드백으로 다시 request(리뷰·반복) → "
                f"goal 충족 시 complete_task로 마감.\n"
                f"[협업 유도 — 중요] 여러 동료가 서로 **맞물리는 공유 인터페이스/계약**(예: 두 백엔드의 "
                f"데이터↔서버 API, 필드명·반환형·에러형·함수 시그니처)은 **당신이 미리 못박지 마세요.** "
                f"목표와 역할 분담만 정하고, 위임 시 \"세부 인터페이스는 상대 동료와 request(Info)로 직접 합의해 정하라\"고 "
                f"지시하세요. 미리 다 정해주면 동료끼리 물을 게 없어져 협업이 사라집니다 — 의존성을 남겨 둬야 합니다.\n"
                f"[마무리 검증] 보고 전에 실제 산출물을 Read/Glob로 점검하세요 — 백엔드·프론트가 같은 "
                f"구조에서 연동되는지, 보고에 적을 '실행 방법'이 실제 파일과 일치하는지 확인한 뒤 "
                f"결과를 간결히 반환하세요."
            )
        return (
            f"당신은 자율적으로 일하는 팀원입니다(당신도 필요하면 동료에게 먼저 묻습니다). "
            f"당신의 역할: {my_role}\n받은 요청({getattr(kind, 'value', kind)}): {body}\n동료: {peers}\n\n"
            f"{self._PRINCIPLE}\n\n"
            f"역할에 충실하게(역할 밖 산출물 금지) 처리하되, 위 원칙대로 가정 대신 확인하세요. "
            f"당신 산출물이 다른 동료 것과 **맞물리면**(공유 인터페이스·계약), 한쪽이 일방적으로 정하지 말고 "
            f"그 동료에게 request(Info)로 **먼저 합의**한 뒤 구현하세요 — 상대가 정한 게 있으면 Read·질의로 확인, "
            f"없으면 같이 결정하고 이견은 근거로 조율. 리더가 인터페이스를 안 정해줬다면 그건 '둘이 정하라'는 뜻입니다. "
            f"일손이 더 필요하면 recruit로 풀에서 동료를 현재 Task에 합류시킬 수 있습니다.\n"
            f"**토론 입장/대표(예: 보수/진보, 특정 언어·기술)가 주어졌다면** 그 입장에서 논거를 펴고, 전달된 "
            f"상대 주장에는 맹목적 동의 말고 **구체적으로 반박하거나 일부만 수용**하세요(근거와 함께). 전제가 부정확·모호하면 "
            f"지적하고 되물으세요. 파일은 작업공간에 상대경로로 만드세요. 끝나면 결과(또는 답)를 간결히 반환하세요."
        )

    async def run_turn(self, flow: Flow, organt_id, body, kind, role) -> str:
        server = build_guide_server(flow, organt_id, role)
        organt = self.organt_builder(organt_id, server, role)
        return await organt.handle(self._prompt(body, kind, role, organt_id))

    def _close_flow(self, flow, leader_id, result):
        """베턴을 origin까지 닫는다. 정상이면 리더가 alive→clean close, 비정상(중간 미응답)이면
        열린 프레임을 위로 강제 정리(escalate)해 교착 없이 종료한다."""
        comm = flow.comm
        if not comm.done and comm.alive == leader_id and len(comm.open_requests) == 1:
            comm.respond(leader_id, "accept", result)        # 정상 종료
            return
        guard = 0
        while not comm.done and guard < 64:                   # 비정상: 강제 드레인
            guard += 1
            try:
                comm.escalate("흐름 종료 강제 정리(중간 미응답)")
            except CommError:
                break

    async def handle_user_input(self, channel_id, leader_id, user_text, root_id=None) -> dict:
        # 단일흐름 보존: 활성 흐름 중이면 명령을 '큐'에 넣어 끝난 뒤 순차 처리(버리지 않음).
        if self.active_flow is not None and not self.active_flow.done:
            self.queue.append((channel_id, leader_id, user_text, root_id))
            self._log("queued", text=user_text[:80], depth=len(self.queue))
            return {"mode": "queued", "queued": len(self.queue)}

        flow = Flow(self.guide, channel_id, self.guild_id, leader_id, self.bot_info)
        if root_id is not None:
            flow.start_root(root_id)
        flow.wake = lambda to, b, k: self.run_turn(flow, to, b, k, "member")
        self.active_flow = flow
        try:
            result = await self.run_turn(flow, leader_id, user_text, Kind.WORK, "leader")
        except Exception as e:                     # 리더가 죽어도 흐름은 닫고 보고한다
            result = f"(리더 처리 중 오류: {e})"
        # 리더의 반환값 = 사용자에게 가는 Response(=보고). origin 프레임을 닫아 시작점 복귀.
        await self.guide.post(flow.user_channel, leader_id, format_response(result),
                              reply_to=flow.root_id)
        self._close_flow(flow, leader_id, result)
        flow.done, flow.final = True, result
        # 안전망: 리더가 닫지 않은 현재 Task가 있으면 완료로 마감.
        if flow.current is not None:
            flow.current.status.status = "완료"
            flow.current.status.result = (result or "")[:500]
            await flow.refresh(flow.current)
            flow.current = None
        self._log("flow_done", project=flow.project_channel is not None,
                  tasks=len(flow.tasks), comm_done=flow.comm.done)
        self.active_flow = None
        # 큐에 대기 중인 명령이 있으면 순차로 이어서 처리(단일흐름 유지).
        if self.queue:
            nxt = self.queue.pop(0)
            return await self.handle_user_input(*nxt)
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
