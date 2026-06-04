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
    def __init__(self, guide, guild_id, organt_builder, bot_info: Optional[Dict[int, str]] = None,
                 workspace=None):
        self.guide = guide
        self.guild_id = guild_id
        self.organt_builder = organt_builder   # (organt_id, guide_server, role) -> Organt
        self.bot_info = bot_info or {}
        self.workspace = workspace             # run 툴 cwd(작업공간 경로)
        self.active_flow: Optional[Flow] = None
        self.queue = []                        # 진행 중 들어온 명령(순차 처리 대기)
        self.flow_log = []
        self.projects: Dict[int, dict] = {}    # channel_id → 프로젝트 컨텍스트(개입 진입점)
        self._proj_n = 0

    def _register_project(self, channel_id, name, workspace, leader) -> str:
        """프로젝트를 1급 엔티티로 등록 → 식별번호 P-XXX 부여(이미 있으면 재사용).
        등록된 채널에 다시 명령이 오면 '개입'으로 라우팅돼 같은 맥락(워크스페이스·팀)에서 이어간다."""
        ch = int(channel_id)
        if ch in self.projects:
            return self.projects[ch]["id"]
        self._proj_n += 1
        pid = f"P-{self._proj_n:03d}"
        self.projects[ch] = {"id": pid, "name": name, "channel": ch,
                             "workspace": workspace, "leader": leader, "summary": ""}
        return pid

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
        "[요청은 하나씩] 한 턴에 request는 하나만 보내세요 — 여러 개를 한꺼번에 던지면 단일흐름에서 "
        "직렬화되어 대기·지연됩니다. 응답을 받은 뒤 다음 요청을 보내세요.\n"
        "[실행으로 검증] '구동·연결되는가'가 아니라 **의도한 동작(사용자가 실제로 받는 결과)이 일어나는가**를 "
        "run 툴로 재현해 확인하세요 — 실제 사용 시나리오를 한 번 끝까지 돌려, 핵심 동작이 깨지지 않는지(즉시 "
        "실패·빈 결과·오작동·곧장 종료 등)를 봅니다. '서버가 떴다/메시지가 오간다'에서 멈추지 말 것 — goal에 적힌 "
        "성공 조건이 진짜 충족되는지가 기준입니다. 동료 응답에 '⚠ 턴 한도 도달'이 붙어 있으면 미완이니 다시 "
        "보완을 요청하세요.\n"
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
                f"당신은 흐름을 여는 '담당자'입니다 — 단, 특별한 권력자가 아닙니다. 당신의 역할: {my_role}\n"
                f"User 요청: {body}\n동료: {peers}\n\n"
                f"{self._PRINCIPLE}\n\n"
                f"[당신의 위치 — 중요] 당신도 동료와 '동등하게' 협업하는 한 명입니다. 다만 무한 루프·교착을 막기 "
                f"위한 '결정·수렴 담당자'를 겸합니다. 평소엔 동료처럼 일하고, **의견이 갈리거나 왕복이 길어질 때만** "
                f"공정하게 결정해 수렴시키세요. 권력이 아니라 '수렴'이 당신의 유일한 특권입니다 — 일을 혼자 끌어안지도, "
                f"인터페이스를 독단으로 못박지도 마세요.\n"
                f"[팀 구성] 시작 시 규모를 산정해 create_project(team=…)로 팀을 배정하세요(id/역할명). 각 Task는 "
                f"create_task(owner=…, members=…)로 **산출물별 단일 책임자(owner)를 정해** 여세요. **request 전에 "
                f"반드시 create_task로 현재 Task를 여세요.** 프로젝트 팀원은 request하면 자동 합류하고, 풀 밖 인력이 "
                f"필요할 때만 recruit로 신중히.\n"
                f"[판단] 요청 성격을 보고 셋 중 하나로 처리하세요.\n"
                f"- '단순 질문/인사이트'(혼자 답 가능) → 답만 간결히 반환.\n"
                f"- '팀 논의/토론/선택' → create_project→create_task 후 **진행자로서** ① 각자에게 request(Info)로 "
                f"입장·논거를 받고 ② **한 사람의 주장을 다른 사람에게 그대로 전달**해 반박/수용을 받게(2명이면 양자, "
                f"3명+면 교차) 실제 반박이 오가게 하고 ③ 전제가 모호하면 명확히 해 다시 묻고 ④ **당사자끼리 합의되면 "
                f"그 합의를 채택**하고, 합의가 안 되거나 왕복이 길어지면 그때 **당신이 공정하게 단일 결론을 확정**"
                f"(자기 편 금지, 무승부·애매 종료 금지). 당신의 결정은 '수렴이 안 될 때의 최후 수단'입니다.\n"
                f"- '실작업 Project' → create_project(채널 1개) 후 일을 **서로 겹치지 않는 '소유된 산출물' 단위**로 "
                f"나누되, **단일흐름이므로 Task는 한 번에 하나만** 엽니다(현재 Task를 complete_task로 마감해야 다음을 "
                f"열 수 있음 — 산출물별로 순차 진행, 미리 여러 개 만들지 말 것). **각 산출물에 owner를 분산 배정**"
                f"(create_task(owner=…)) — 같은 직군이어도 당신이 모든 걸 직접 구현하면 협업이 사라집니다(중앙집권 "
                f"금지). 전문 동료가 있으면 그를 owner로 세워 **Work로 위임**하고, 당신은 한두 산출물이나 통합만 맡으세요.\n"
                f"  각 Task: **owner가 직접 구현**하고, 맞물리는 인터페이스는 **상대 owner와 request(Info)로 직접 합의**"
                f"(당신이 미리 못박지 말 것). 당신은 ① 산출물을 Read로 검증 ② 맞물림이 어긋나면 양쪽 owner를 교차로 "
                f"물어 조율을 유도 ③ **당사자 합의가 안 되거나 리뷰 왕복이 2회를 넘기면** 그때 공정하게 결정해 수렴 "
                f"④ goal 충족 시 complete_task로 마감. 무한 루프·무승부 금지.\n"
                f"[협업 유도 — 중요] 여러 owner가 **맞물리는 공유 인터페이스/계약**(필드명·반환형·메시지 포맷·함수 "
                f"시그니처)은 **당신이 미리 못박지 마세요.** 목표·역할·owner만 정하고 \"세부 인터페이스는 상대 owner와 "
                f"request(Info)로 직접 합의하라\"고 지시하세요. 미리 다 정하면 동료끼리 물을 게 없어져 협업이 사라집니다.\n"
                f"[마무리 검증] 보고 전에 run 툴로 **실제 사용 시나리오를 재현**해 goal의 성공 조건이 진짜 충족되는지 "
                f"확인하세요(구동 여부가 아니라 의도한 결과 — 핵심 동작이 깨지면 미완으로 보고 owner에게 보완 요청). "
                f"확인 뒤 결과를 간결히 반환하세요."
            )
        return (
            f"당신은 자율적으로 일하는 팀원입니다(당신도 필요하면 동료에게 먼저 묻습니다). "
            f"당신의 역할: {my_role}\n받은 요청({getattr(kind, 'value', kind)}): {body}\n동료: {peers}\n\n"
            f"{self._PRINCIPLE}\n\n"
            f"**당신이 이 산출물의 owner(책임자)라면**, 받은 목표를 끝까지 책임지고 **직접 구현·검증까지 몰고 가세요** "
            f"— 리더에게 되넘기지 말 것. 역할에 충실하게(역할 밖 산출물 금지) 처리하되, 위 원칙대로 가정 대신 확인하세요. "
            f"당신 산출물이 다른 동료 것과 **맞물리면**(공유 인터페이스·계약), 한쪽이 일방적으로 정하지 말고 "
            f"그 동료에게 request(Info)로 **먼저 합의**한 뒤 구현하세요 — 상대가 정한 게 있으면 Read·질의로 확인, "
            f"없으면 같이 결정하고 이견은 근거로 조율. 리더가 인터페이스를 안 정해줬다면 그건 '당사자끼리 정하라'는 뜻입니다. "
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

        proj = self.projects.get(int(channel_id))   # 이 채널이 등록된 프로젝트면 '개입'
        lead = proj["leader"] if proj else leader_id
        flow = Flow(self.guide, channel_id, self.guild_id, lead, self.bot_info)
        flow.register_project = lambda ch, name: self._register_project(ch, name, flow.workspace, flow.leader)
        body = user_text
        if proj:                                     # 기존 프로젝트 개입 — 맥락 유지(재생성 X)
            flow.project_channel = int(channel_id)   # 기존 채널 재사용 → create_project는 no-op
            flow.workspace = proj["workspace"]
            flow.project_id, flow.intervention = proj["id"], proj
            body = (f"[프로젝트 {proj['id']} 개입] 이 프로젝트엔 이미 작업공간·산출물이 있습니다. "
                    f"create_project를 다시 만들지 말고, 먼저 Read/run으로 현황을 파악한 뒤 아래 요청을 "
                    f"수행하고 검증하세요.\n요청: {user_text}")
            self._log("intervention", project=proj["id"], text=user_text[:60])
        else:
            flow.workspace = self.workspace
        if root_id is not None:
            flow.start_root(root_id)
        flow.wake = lambda to, b, k: self.run_turn(flow, to, b, k, "member")
        self.active_flow = flow
        try:
            result = await self.run_turn(flow, lead, body, Kind.WORK, "leader")
        except Exception as e:                     # 리더가 죽어도 흐름은 닫고 보고한다
            result = f"(리더 처리 중 오류: {e})"
        # 리더의 반환값 = 사용자에게 가는 Response(=보고). origin 프레임을 닫아 시작점 복귀.
        await self.guide.post(flow.user_channel, lead, format_response(result),
                              reply_to=flow.root_id)
        self._close_flow(flow, lead, result)
        flow.done, flow.final = True, result
        # 안전망: 리더가 닫지 않은 현재 Task가 있으면 완료로 마감.
        if flow.current is not None:
            flow.current.status.status = "완료"
            flow.current.status.result = (result or "")[:500]
            await flow.refresh(flow.current)
            flow.current = None
        # 프로젝트 요약 갱신(다음 개입 때 맥락으로 제공)
        if flow.project_channel:
            p = self.projects.get(int(flow.project_channel))
            if p:
                p["summary"] = (result or "")[:300]
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
