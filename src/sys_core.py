"""SYS — Organt 주도 + P2P Communication.

User 입력 → SYS가 담당(리더)을 깨움 → Organt가 판단·행동(파일/Guide 도구).
필요하면 어떤 Organt든 `request`로 동료를 부르고, SYS가 그 동료를 중첩 베턴으로
깨워(run_turn) 응답을 돌려준다. 항상 1명만 활성(단일흐름) → 사이드이펙트·토큰 절약.

SYS는 얇다: 깨우기(wake) 제공 + 단일흐름 lock + 라우팅. 베턴/권한 강제는 Rule·Hook.
Organt 생성(모델·권한·State)은 organt_builder로 주입받는다.
"""
import asyncio
import glob
import json
import os
import time
from typing import Dict, Optional

from .communication import CommError
from .guide_tools import Flow, build_guide_server
from .protocol import Kind, Request, format_response

# 턴 한도로 작업이 끊겼을 때 같은 세션으로 이어가게 하는 지시(구조적 연속 실행).
_CONTINUE_BODY = (
    "[이어서 계속 — 처음부터 다시 하지 말 것] 직전 턴이 작업 도중 '턴 한도'로 끊겼습니다. "
    "진행 중이던 Task가 아직 열려 있을 수 있습니다. 현재 작업공간 상태를 Read/run으로 먼저 확인한 뒤, "
    "이미 한 부분은 건너뛰고 남은 부분만 마저 진행해 그 Task를 complete_task로 마감하세요. "
    "마감(또는 명시적 완료)까지가 목표입니다."
)



class Sys:
    def __init__(self, guide, guild_id, organt_builder, bot_info: Optional[Dict[int, str]] = None,
                 workspace=None, projects_path=None, session_dir=None, max_continue=6):
        self.guide = guide
        self.guild_id = guild_id
        self.organt_builder = organt_builder   # (organt_id, guide_server, role) -> Organt
        self.bot_info = bot_info or {}
        # 로스터 원본 라벨(직군). recruit(role=…)로 '예비'를 런타임 직군으로 채용하면 bot_info가 바뀌므로,
        # 새 흐름 시작 때 이걸로 원복한다(예비는 다음 흐름에서 다른 직군으로 다시 채용 가능).
        self._roster_labels = dict(self.bot_info)
        self._origin_request = ""   # 이번 흐름의 '사용자 원문 요청'(담당자 paraphrase 아닌 원문) — 모든 프롬프트에 주입
        self.workspace = workspace             # run 툴 cwd(작업공간 경로)
        self.session_dir = session_dir         # organt_state_*.json 위치(새 요청마다 세션 초기화)
        self.max_continue = max_continue       # 턴 한도로 미완 시 같은 세션으로 이어가는 최대 횟수
        # 워커 턴 안전 타임아웃(초): 서브프로세스가 '행'(무응답·무크래시)이면 단일흐름 전체가 영구 정지하므로
        # 한도 넘으면 포기하고 '인프라 실패'로 반환해 리더가 보고·진행하게 한다(리더 턴은 흐름 전체라 제외).
        self.turn_timeout = int(os.environ.get("ORGANT_TURN_TIMEOUT", "480"))   # 기본 8분
        # 흐름 '무진행(행)' 워치독: 요청·파일작성·실행 등 어떤 진행도 이 시간(초) 동안 없으면 흐름이 행으로
        # 멈춘 것(리더 서브프로세스 행 포함 — 리더 턴엔 타임아웃이 없어 생기는 구멍)으로 보고 자동 중단·보고한다.
        # 워커 타임아웃(turn_timeout=8분)보다 넉넉히 커야 워커 1회 행→복구를 '무진행'으로 오인하지 않는다.
        self.idle_timeout = int(os.environ.get("ORGANT_IDLE_TIMEOUT", "720"))   # 기본 12분(>8분 워커 타임아웃)
        self.active_flow: Optional[Flow] = None
        self.queue = []                        # 진행 중 들어온 명령(순차 처리 대기)
        self.flow_log = []
        self.flow_log_path = (os.path.join(session_dir, "flow.jsonl") if session_dir else None)
        self.projects_path = projects_path     # 레지스트리 영속 경로(없으면 인메모리)
        self.projects: Dict[int, dict] = {}    # channel_id → 프로젝트 컨텍스트(개입 진입점)
        self._proj_n = 0
        self._load_projects()

    def _load_projects(self):
        """디스크에서 프로젝트 레지스트리 복원 — 프로세스가 끝나도 '원래 작업'에 개입 가능."""
        if not self.projects_path or not os.path.exists(self.projects_path):
            return
        try:
            data = json.load(open(self.projects_path, encoding="utf-8"))
            self.projects = {int(k): v for k, v in data.get("projects", {}).items()}
            self._proj_n = data.get("n", len(self.projects))
        except Exception:
            pass

    def _save_projects(self):
        if not self.projects_path:
            return
        try:
            data = {"n": self._proj_n,
                    "projects": {str(k): v for k, v in self.projects.items()}}
            # 원자적 저장: 임시파일에 다 쓰고 flush+fsync 후 교체 → 쓰는 도중 프로세스가 죽어도
            # 원본 projects.json이 '반쪽(깨진 JSON)'으로 남지 않는다(개입 레지스트리 유실 방지).
            tmp = f"{self.projects_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.projects_path)
        except Exception:
            pass

    def _register_project(self, channel_id, name, workspace, leader) -> str:
        """프로젝트를 1급 엔티티로 등록 → 식별번호 P-XXX 부여. 같은 채널이나 같은 이름이 이미
        있으면 재사용(중복 방지). 등록 채널에 다시 명령이 오면 '개입'으로 라우팅된다."""
        ch = int(channel_id)
        if ch in self.projects:
            return self.projects[ch]["id"]
        # 같은 이름이 이미 있으면 식별번호를 '그대로 유지'하고 채널만 현재 것으로 이동(증가/중복 금지)
        for c, p in list(self.projects.items()):
            if p.get("name") == name:
                p["channel"], p["workspace"] = ch, workspace
                self.projects[ch] = p
                if c != ch:
                    del self.projects[c]
                self._save_projects()
                return p["id"]
        self._proj_n += 1
        pid = f"P-{self._proj_n:03d}"
        self.projects[ch] = {"id": pid, "name": name, "channel": ch,
                             "workspace": workspace, "leader": leader, "summary": ""}
        self._save_projects()
        return pid

    def _log(self, event, **f):
        rec = {"event": event, "ts": time.time(), **f}
        self.flow_log.append(rec)
        if self.flow_log_path:   # 메모리만이던 continue_incomplete/flow_done/req_sent를 디스크로 영속(관측)
            try:
                with open(self.flow_log_path, "a", encoding="utf-8") as fp:
                    fp.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            except OSError:
                pass

    def _reset_sessions(self):
        """새 최상위 요청마다 에이전트 세션(resume용 session_id)을 초기화한다.
        이전 요청의 '이미 끝냈다/작업중' 맥락이 새 요청에 달라붙어 no-op 하는 앵커링을
        구조적으로 차단한다(수동 rm의 대체). 산출물은 작업공간에 남으므로 맥락은 Read로 복원."""
        if not self.session_dir:
            return
        n = 0
        for fp in glob.glob(os.path.join(str(self.session_dir), "organt_state_*.json")):
            try:
                os.remove(fp)
                n += 1
            except OSError:
                pass
        self._log("reset_sessions", cleared=n)

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
        "[재현으로 진단 — 스펙 아닌 '실제 행동'에서] 보고된 버그·요청은 문서·스펙(design-spec 등)에서 "
        "'유추'하지 말고, 먼저 run으로 실제 산출물을 돌려 보고된 증상을 직접 재현·관찰한 뒤 그 증상을 "
        "일으키는 '실제 코드'를 찾아 고치세요. 동작·규칙·물리 문제(충돌·이동·점수·판정·무적·즉사·안 먹힘 "
        "등)는 거의 항상 **서버(server.js) 로직**이 원인이고, 색·레이아웃·그리는 순서(z-order)·글로우처럼 "
        "**'눈에 보이는' 표현만 클라이언트(public/)**입니다. 예: '제자리 회전 무적/안 죽음/먹이 안 먹힘'은 "
        "서버의 충돌·판정 로직 문제지 z-order·비네팅 같은 렌더 문제가 아닙니다. 스펙 문서는 '참고'일 뿐 "
        "'할 일 목록'이 아닙니다 — 사용자가 보고한 그 증상만, 재현해 확인한 원인 코드에서 고치세요.\n"
        "[실행으로 검증] '구동·연결되는가'가 아니라 **의도한 동작(사용자가 실제로 받는 결과)이 일어나는가**를 "
        "run 툴로 재현해 확인하세요 — 실제 사용 시나리오를 한 번 끝까지 돌려, 핵심 동작이 깨지지 않는지(즉시 "
        "실패·빈 결과·오작동·곧장 종료 등)를 봅니다. '서버가 떴다/메시지가 오간다'에서 멈추지 말 것 — goal에 적힌 "
        "성공 조건이 진짜 충족되는지가 기준입니다. 또한 **엣지 케이스(자원 0·경계값·극단/연속 입력)와 일관성"
        "(비용↔효과가 서로 맞나 — 자원이 0이면 그 동작이 막히나, 소모와 효과가 함께 일어나나, 조작이 의도대로 "
        "반응하나·제자리 맴돌지 않나)을 유저 입장에서 직접 재현해 확인**하세요('된다'가 아니라 '플레이가 말이 "
        "되나'). 동료 응답에 '⚠ 턴 한도 도달'이 붙어 있으면 미완이니 다시 "
        "보완을 요청하세요.\n"
        "[완성도 기준] 산출물은 '동작하는' 수준이 아니라 '그 종류 결과물로서 완성·정돈된' 수준이어야 "
        "합니다 — 같은 요청을 숙련자가 받았다면 당연히 갖췄을 요소·손맛·디자인을 갖추세요(요청의 함의에 맞는 "
        "깊이, 골격/최소판 금지). '무엇이 완성인지'는 그 artifact 종류의 상식에서 끌어내세요(하드코딩 아님). "
        "검증·리뷰도 '되나'만이 아니라 '완성도·경험의 질'까지 봅니다.\n"
        "[되묻기 규칙] 당신에게 일을 맡긴 '직속 위임자'에게는 request(Info)로 되물을 수 있습니다 — 그 질문은 "
        "위임자에게 전달되니(이 턴은 짧게 마치고 반환하세요) 위임자가 답한 뒤 그 일을 당신에게 다시 맡깁니다. "
        "단 더 위(위임자의 위임자 등)나 다른 멈춘 동료에겐 되물을 수 없으니, 그 산출물을 Read 하거나 멈춰있지 "
        "않은 동료에게 물으세요.\n"
        "[작업공간 레이아웃] 모든 산출물은 작업공간 '루트' 기준 하나의 일관된 구조로 만드세요. "
        "중첩 프로젝트 폴더(todo-app/ 등) 만들지 말 것. 표준 경로: 백엔드 서버는 루트(server.js 또는 "
        "app.py), 프론트엔드는 public/(index.html·style.css·app.js), 스펙은 루트(api-spec.md·"
        "design-spec.md). 같은 산출물을 두 위치에 만들지 말고, 동료에게 위임할 땐 정확한 경로를 주세요.\n"
        "[보고] 결과는 간결한 일반 텍스트로 반환하세요 — 그 반환값이 곧 요청자에게 가는 Response. "
        "'---' 구분선/'✅ 완성' 배너/표/긴 머리말 같은 장식은 쓰지 말고, 보고하려고 request 쓰지 마세요."
    )

    def _prompt(self, body, kind, role, me, leader_id=None):
        # '담당자'는 고정 직책이 아니라 이번 흐름의 To 수신자(=leader)다. 동료 목록엔 직군만 적고, 담당자에게만
        # '(담당자)' 표식을 단다(다른 흐름에선 같은 봇이 한 직원으로 참여).
        def _peer(i):
            lbl = self.bot_info.get(i, "?")
            return f"{lbl}(담당자)" if i == leader_id else lbl
        peers = ", ".join(f"{i}({_peer(i)})" for i in self.bot_info if i != me)
        domain = self.bot_info.get(me, "")
        # 탈중앙(퍼실리테이터): 모두가 '담당자의 요약'이 아니라 '사용자 원문'을 직접 본다 → 한 명의 해석을
        # 거치며 의도가 왜곡되는 걸 막는다. 받은 지시가 원문과 어긋나면 원문 의도를 우선·되물음.
        orig = (getattr(self, "_origin_request", "") or "").strip()
        origin_note = (f"[사용자 원문 요청 — 진짜 의도(누구의 요약·해석도 아닌, 사용자가 실제로 한 말)]: {orig}\n"
                       f"이 원문이 기준입니다. 받은 지시·질문이 원문과 어긋나 보이면 원문 의도를 우선하고, 모호하면 되물으세요.\n\n"
                       if orig else "")
        if role == "leader":
            my_role = f"{domain}(담당자)" if domain else "담당자"
            # 담당자가 '예비'(직군 미배정)로 호명된 경우: 자길 예비로 방치하지 말고 먼저 자기 직군부터 채용해
            # 한 직원으로 참여한 뒤 팀을 꾸린다(사용자: '자기 자신도 프로젝트의 일원으로 참여해야지').
            is_spare_leader = str(domain).startswith("예비")
            spare_lead_note = (
                f"[당신은 '예비'로 호명됨 — 가장 먼저, 무엇보다 자기 직군부터] 당신(id {me})은 아직 직군 미배정 "
                f"'예비'인데 이번 흐름의 담당자로 호명됐습니다. **create_project·create_task·request 그 무엇보다 먼저, "
                f"맨 첫 행동으로 recruit(member={me}, role='당신이 맡을 직군')를 호출해 자기 직군부터 확정**하세요 — "
                f"이건 Task가 없어도 됩니다(Task 전에 호출 가능). 이 순서를 안 지키고 '예비'인 채로 프로젝트/Task를 "
                f"열면 화면(상태블록·동료 프롬프트)에 담당자가 '예비'로 박힙니다. 자기 직군을 정한 뒤엔 한 직원으로 "
                f"직접 참여(자기 도메인 구현에 기여)하고, 일에 필요한 다른 직군 동료를 골라 팀을 꾸리며 부족한 직군은 "
                f"recruit로 채우세요.\n"
                if is_spare_leader else "")
            # 팀은 고정이 아니라 담당자가 일에 맞게 동적으로 짠다(직군 고정 해결).
            team_note = (
                f"[팀은 당신이 동적으로 짠다 — 자동 전원 아님] 직군 구성은 미리 고정돼 있지 않습니다. 이 일에 **필요한 "
                f"직군을 당신이 직접 고르세요** — create_project(team='필요한 직군/동료들')로 팀을 정하고, 모자란 직군은 "
                f"recruit(role='직군명')로 더하세요(예비 인력이 그 직군으로 채용됨). 자동으로 전원이 소집되지 않습니다"
                f"(놀던 인력까지 무조건 부르지 말 것). set_goal은 '당신이 고른 그 팀 전원'의 협의로 통과합니다.\n")
            return (
                f"당신은 이번 요청의 To로 지정돼 흐름을 여는 '담당자'입니다 — 고정 직책이 아니라 To를 받아 "
                f"이번 흐름의 담당이 된 것이며(다른 흐름에선 한 직원으로 참여), 특별한 권력자가 아닙니다. "
                f"당신의 역할: {my_role}\n"
                f"{origin_note}"
                f"받은 형태: {body}\n동료: {peers}\n\n"
                f"{spare_lead_note}{team_note}\n"
                f"{self._PRINCIPLE}\n\n"
                f"[구현은 위임 — 자문만 받고 독식 금지] **Info로 의견만 잔뜩 묻고 정작 파일은 당신이 다 만드는 건 "
                f"중앙집권·독점입니다(구조적으로 차단됨).** 팀이 있으면 구현(파일 작성)은 각 도메인 owner에게 "
                f"request(**Work**)로 맡기세요 — 백엔드는 백엔드 동료, 프론트는 프론트 동료, 이펙트는 VFX가 "
                f"'직접' 만듭니다. 당신은 조율·통합·검증(run)·배포와 '당신 도메인 일부'만. (동료가 무응답이면 인프라 "
                f"문제이니 새로 뽑거나 떠안지 말고 사용자에게 보고.)\n"
                f"[퍼실리테이터 — 중요] 당신은 '해석자'가 아니라 '진행자'입니다. **사용자 원문을 당신 식으로 바꿔 팀에 "
                f"전달하지 마세요** — request로 동료에게 물을 때 사용자 원문을 그대로 인용해 함께 보여주고(당신 요약만 주지 "
                f"말 것), set_goal의 Purpose·Goal은 **당신 생각이 아니라 각 전문가가 제안한 것을 종합**해 적으세요(혼자 "
                f"저작 금지). 채용은 당신 짐작이 아니라 **기획에서 드러난 도메인 공백** 기준입니다 — '이펙트가 약하다'면 VFX, "
                f"'기획이 허술'이면 게임 기획자, '봇이 필요'면 봇 전문가를 recruit(role=…). **무응답·타임아웃이 연속되면 "
                f"새 사람을 계속 뽑지 마세요(같은 불안정으로 똑같이 실패) — 잠시 뒤 재시도하거나 사용자에게 보고**하세요.\n"
                f"[당신의 위치 — 중요] 당신도 팀의 한 직원입니다 — 파일 작성(Write/Edit)·실행(run) 도구를 그대로 가지며 "
                f"직접 구현할 수 있습니다. 리더는 특별한 권력자가 아니라 '구현'에 더해 '조율·수렴·판정'을 함께 맡는 자리일 "
                f"뿐입니다(중앙집권 금지). **모든 걸 혼자 만들지도, 전부 위임하고 지켜보기만 하지도 마세요** — 둘 다 "
                f"잘못입니다. 도메인 전문 동료가 있으면 그 산출물은 Work로 위임해 그 동료(owner)가 구현·검증까지 **끝까지 "
                f"책임**지게 분배하되(한 사람이 일을 독점하지 않도록), **당신도 한 도메인은 직접 맡아 구현에 기여하세요** "
                f"(조율만 하고 다 위임한 채 지켜보는 '노는 리더' 금지 — 리더도 똑같이 일하는 직원입니다). 단 **당신 도메인 "
                f"밖(다른 전문가가 있는 도메인: 프론트·디자인·QA 등)은 당신이 직접 만들지 말고 그 전문가에게 Work로 위임"
                f"하세요** — 당신이 전부 만들어 버리면 팀이 노는 '독점'입니다(놀게 두지도, 독점하지도 말 것). 목표는 팀과 "
                f"**합의**해 set_goal로 확정하고, owner끼리 맞물리면 중재하고, 완수를 판정합니다. **Work로 위임할 땐 '구현 "
                f"스펙'을 매번 새로 쓰지 말고 '측정가능한 목표'를 주세요** — 어떻게 만들지는 owner가 정합니다.\n"
                f"[재요청은 Redo로 — 중요] **owner가 산출물을 내면 반사적으로 다시 시키지 말고 먼저 검증하세요**(run으로 직접, "
                f"또는 응답에 붙은 'owner 실행 증거'로 확인). goal이 충족되면 그 자리에서 complete_task로 마감하고, **구체적 "
                f"결함이 드러날 때만** 그 결함을 적어 다시 요청하세요. 이미 완료 응답을 받은 산출물을 같은 동료에게 또 맡기면 "
                f"구조적으로 'Redo(직전 결함 보완)'로만 처리되고 한계를 넘으면 막힙니다 — 규칙은 '검증 후 수락, 아니면 결함 보완'입니다.\n"
                f"[팀 구성 — 작업 무게에 맞춰] 시작 시 **작업의 무게를 보고 팀 규모를 정해** create_project(team=…)로 "
                f"배정하세요. '도메인당 1명' 최소 구성으로 기계적으로 돌리지 말 것 — **무겁거나 중요한 도메인엔 여러 명을 "
                f"배정**해 일을 나눠 맡기거나 교차 검증하게 하고(단일흐름이라 동시 실행은 아니지만 분담·리뷰로 품질을 높입니다), "
                f"**풀에 여유 인력이 있으면 적극 끌어쓰세요**(한가하게 놀리지 말 것; 부족하면 recruit로 더). "
                f"**로스터에 없는 전문 직군(게임 기획자·UX 디자이너·사운드·레벨 디자이너 등)이 필요하면 "
                f"recruit(role='직군명')으로 '예비' 인력을 그 직군으로 채용**해 맡기세요(직군은 미리 박힌 게 아니라 "
                f"필요에 따라 런타임에 채용하는 것). **'예비'에게는 말로 '너는 X 담당이야' 하고 일을 시킬 수 없습니다 — "
                f"반드시 recruit(role='직군')로 직군을 실제 부여해야 위임이 됩니다(말로만 배정은 구조적으로 거부). "
                f"직군은 **1봇 1직업** — 한 사람에게 직군 2개(겸직)는 줄 수 없습니다. 다른 직군이 필요하면 또 다른 "
                f"'예비'를 그 직군으로 새로 뽑으세요. 한 번 직군을 받은 봇은 그 직업을 계속 유지하니(직업 고정·기억), "
                f"이미 그 직군을 가진 동료가 있으면 새로 뽑지 말고 그 동료를 쓰세요(직업군 재사용).** 각 Task는 "
                f"create_task(purpose=…, members=…)로 **Purpose(문제)만 갖고** 여세요 — **Goal·owner를 미리 정하지 말 것.** "
                f"Goal은 Task 안에서 동료와 request(Info)로 합의해 **set_goal로 확정**하고, owner는 **그 일을 Work로 받는 "
                f"동료가 됩니다**(수신=소유). **request 전에 create_task로 Task를 먼저 여세요.** 프로젝트 팀원은 request하면 "
                f"자동 합류하고, 풀 밖 인력이 필요할 때만 recruit로. **단일흐름이라 한 번에 한 명에게만 request하세요 — 한 "
                f"턴에 request를 여러 개 보내면 베턴을 쥔 첫 요청만 나가고 나머지는 거부됩니다(앞 응답을 받은 뒤 다음을 보내세요).**\n"
                f"[판단] 요청 성격을 보고 셋 중 하나로 처리하세요.\n"
                f"- '단순 질문/인사이트'(혼자 답 가능) → 답만 간결히 반환.\n"
                f"- '팀 논의/토론/선택' → create_project→create_task 후 **진행자로서** ① 각자에게 request(Info)로 "
                f"입장·논거를 받고 ② **한 사람의 주장을 다른 사람에게 그대로 전달**해 반박/수용을 받게(2명이면 양자, "
                f"3명+면 교차) 실제 반박이 오가게 하고 ③ 전제가 모호하면 명확히 해 다시 묻고 ④ **당사자끼리 합의되면 "
                f"그 합의를 채택**하고, 합의가 안 되거나 왕복이 길어지면 그때 **당신이 공정하게 단일 결론을 확정**"
                f"(자기 편 금지, 무승부·애매 종료 금지). 당신의 결정은 '수렴이 안 될 때의 최후 수단'입니다.\n"
                f"- '실작업 Project' → create_project(채널 1개)로 팀을 모은 뒤, **먼저 '기획 회의'를 하세요(중요)**: "
                f"Task 분해와 담당을 당신이 혼자 정하지 말 것. 각 전문 동료에게 request(Info)로 **'이 목표에서 당신 "
                f"도메인의 할 일과 당신이 맡을 것을 제안하라'**고 물으세요(디자이너는 디자인 작업을, 서버 전문가는 서버 "
                f"작업을 스스로 정의 — 당신이 프론트라고 디자이너·서버 일을 대신 분배하지 말 것, 그 분야 전문가가 정합니다). "
                f"받은 제안들을 **종합**해 Task 분해를 확정하되(owner는 미리 못박지 말 것 — 그 일을 Work로 받는 사람이 "
                f"owner가 됨), 겹치거나 충돌하면 그때 공정하게 조율·결정(수렴). "
                f"즉 **분해·담당은 회의의 산물**이고 당신은 진행자입니다.\n"
                f"  **맨 처음 Task(기획 회의)도 팀은 당신이 고릅니다 — create_task(members=…)에 이 일에 필요한 직군 "
                f"동료를 직접 지정**하세요(자동 전원 소집 아님; 직군은 고정이 아니라 일에 맞춰 당신이 구성, 모자라면 "
                f"recruit로 더함). 그 멤버 **전원**에게 request(Info)로 '각자 도메인의 할 일·분담·성공기준'을 물어 "
                f"수렴해야 set_goal이 통과합니다(당신이 고른 팀 전원 협의 전엔 거부). 이게 분해·분담을 함께 정하는 "
                f"자리입니다.** 이후 **산출물 단위**로 진행하되 **단일흐름이라 Task는 한 번에 하나만**"
                f"(complete_task로 마감해야 다음). 구현 Task는 **‘빈 껍데기’로 엽니다 — create_task(members)에 Purpose를 "
                f"적지 마세요(리더가 할 일을 미리 못 박지 않음)**. 연 직후 **그 Task의 멤버 전원에게 request(Info)로 한 번씩 "
                f"'이 Task에서 풀 문제(Purpose)·네 도메인의 목표·성공기준'을 물어**(같은 사람에게 같은 질문 반복 금지 — "
                f"한 번 묻고 답을 반영), 받은 답을 **수렴해 set_goal(purpose, goal)로 Purpose·Goal을 함께 확정**합니다 "
                f"(Task마다 그 담당 팀이 모여 정함). **Goal은 '무엇이 되면 성공인가'(측정가능한 결과·시나리오)만 적고, "
                f"'어떤 파일·엔드포인트·스택으로 만들지'는 적지 마세요 — 구현 결정은 owner의 몫**(리더가 작업물의 구현 "
                f"지점을 지정하면 중앙집권). 그런 다음 **그 일을 맡기로 한 동료에게 request(Work)로 위임**(그 동료가 "
                f"owner가 되어 구현 방법을 스스로 정해 직접 구현)하세요. 당신이 모든 걸 직접 "
                f"구현하지 말 것(중앙집권 금지). 맞물리는 인터페이스는 owner끼리 request(Info)로 합의. **owner가 산출물을 "
                f"내면 곧장 다시 시키지 말고 — 먼저 '검증'하세요**: run으로 실제 동작을 확인(또는 QA에게 위임)하고, "
                f"**goal이 충족되면 그 자리에서 complete_task로 마감**. 검증이 **구체적 결함**(빠진 기능·실패한 시나리오)을 "
                f"드러낼 때만 그 결함을 집어 owner에게 다시 요청하세요 — 결함 없이 같은 일을 반복 요청 금지. "
                f"합의 불가/왕복 2회 초과면 공정하게 결정. 무한 루프·무승부 금지.\n"
                f"[협업 유도 — 중요] 여러 owner가 **맞물리는 공유 인터페이스/계약**(필드명·반환형·메시지 포맷·함수 "
                f"시그니처)은 **당신이 정하지도, 중계(한쪽 말을 다른 쪽에 전달)하지도 마세요.** 목표·역할·owner만 정하고 "
                f"\"세부 인터페이스는 **상대 owner끼리 request(Info)로 직접** 합의하라\"고 지시하세요 — 당신을 거치면 허브 "
                f"집중이 됩니다. 당사자끼리 정해야 협업이 살고 책임도 명확해집니다.\n"
                f"[무응답 시 — 독점 금지] 어떤 동료가 응답을 못 하거나 비면 **당신이 직접 그 산출물을 떠안지 마세요** — "
                f"같은 도메인의 다른 동료에게 재배정(request Work)하거나, 없으면 recruit로 풀에서 충원해 맡기세요. "
                f"직접 구현은 '당신 도메인(예: 백엔드)'에 한하고, 막힐 때 떠안는 건 정말 최후수단입니다(분산 유지).\n"
                f"[owner가 일하는 중엔 완료·대리구현 금지 — 중요] 일단 어떤 Task를 owner에게 Work로 위임하면, **그 owner가 "
                f"'run으로 검증한 실제 산출물'을 응답으로 낼 때까지** 당신은 그 Task를 complete_task로 닫을 수 없고(구조적으로 "
                f"거부됨) 그 도메인 파일을 직접 Write/Edit할 수도 없습니다(거부됨). owner가 '곧 하겠다'처럼 착수 전 응답만 "
                f"주거나 아직 작업 중이면 — **앞질러 대신 만들거나 완료 때리지 말고**(그게 '허위 완료'입니다) 같은 owner에게 "
                f"request(Work)로 다시 맡겨 검증된 산출물을 받은 뒤 마감하세요. 끝내 무응답일 때만 recruit/재배정.\n"
                f"[검증·배포 — 필수] '완료'는 말이 아니라 **run 실행 증거**로 판단하세요 — QA(또는 당신)가 run 없이 "
                f"'검토함/것 같음'만 보고하면 통과시키지 말고 실제 run을 돌리게 하세요. goal의 성공조건이 run 증거로 "
                f"확인되면 **deploy 툴로 반드시 배포**하고(검증만 하고 멈추면 미완) 라이브 URL 결과를 간결히 반환하세요 "
                f"(검증이 구체적 결함을 드러내면 그 결함만 보완→재검증→배포)."
            )
        my_role = domain or "팀원"
        return (
            f"당신은 자율적으로 일하는 팀원입니다(당신도 필요하면 동료에게 먼저 묻습니다). "
            f"당신의 역할: {my_role}\n{origin_note}받은 요청({getattr(kind, 'value', kind)}): {body}\n동료: {peers}\n\n"
            f"{self._PRINCIPLE}\n\n"
            f"**기획 단계에서 '당신 도메인의 할 일·담당'을 물으면**, 당신 전문 영역(디자인이면 디자인, 서버면 서버 등)의 "
            f"할 일을 스스로 정의해 구체적으로 제안하고 당신이 맡을 것을 밝히세요 — 리더가 당신 도메인을 대신 정하게 두지 "
            f"말 것(그 분야 전문가는 당신입니다). **단 협의(Info) 단계에선 '제안·합의'만 — 파일 구현(Write)은 금지됩니다. "
            f"실제 구현은 Goal이 합의된 뒤 Work로 위임받았을 때만 하세요(협의 중 선구현 금지 — 구조적으로 차단됨).**\n"
            f"**당신이 이 산출물의 owner(책임자)라면**, 받은 목표를 끝까지 책임지고 **직접 구현·검증까지 몰고 가세요** "
            f"— 리더에게 되넘기지 말 것. 산출물은 **최소 동작판이 아니라 완성·정돈된 판**으로 만드세요 — 그 종류 "
            f"결과물이 당연히 갖출 요소·손맛·디자인을 갖추고, 리더/동료의 깊이 비평이 오면 변명 말고 끌어올리세요. "
            f"역할에 충실하게(역할 밖 산출물 금지) 처리하되, 위 원칙대로 가정 대신 확인하세요. "
            f"당신 산출물이 다른 동료 것과 **맞물리면**(공유 인터페이스·계약), 한쪽이 일방적으로 정하지 말고 "
            f"그 동료에게 request(Info)로 **먼저 합의**한 뒤 구현하세요 — 상대가 정한 게 있으면 Read·질의로 확인, "
            f"없으면 같이 결정하고 이견은 근거로 조율. 리더가 인터페이스를 안 정해줬다면 그건 '당사자끼리 정하라'는 뜻입니다. "
            f"일손이 더 필요하면 recruit로 풀에서 동료를 현재 Task에 합류시킬 수 있습니다.\n"
            f"**토론 입장/대표(예: 보수/진보, 특정 언어·기술)가 주어졌다면** 그 입장에서 논거를 펴고, 전달된 "
            f"상대 주장에는 맹목적 동의 말고 **구체적으로 반박하거나 일부만 수용**하세요(근거와 함께). 전제가 부정확·모호하면 "
            f"지적하고 되물으세요. 파일은 작업공간에 상대경로로 만드세요. 끝나면 결과(또는 답)를 간결히 반환하세요."
        )

    async def _await_with_idle_watchdog(self, task, flow):
        """task(리더 실행)를 기다리되, flow.last_activity가 idle_timeout 동안 안 바뀌면(=흐름 전체 무진행=행)
        task를 취소한다(→ CancelledError). 요청·파일작성·실행 등 진행이 일어나는 한 아무리 길어도 안 끊는다
        — 고정 타임아웃이 아니라 '무진행' 기준이라, 오래 걸리는 정상 빌드는 보호하고 멈춘 것만 해소한다.
        (리더 턴엔 turn_timeout이 없어 생기던 '리더 행' 구멍을 메운다.)"""
        poll = max(1, min(20, self.idle_timeout))

        async def _wd():
            while not task.done():
                await asyncio.sleep(poll)
                idle = time.monotonic() - getattr(flow, "last_activity", time.monotonic())
                if idle > self.idle_timeout and not task.done():
                    self._log("flow_idle_abort", idle=int(idle), timeout=self.idle_timeout)
                    task.cancel()
                    return

        wd = asyncio.create_task(_wd())
        try:
            return await task
        finally:
            wd.cancel()

    async def run_turn(self, flow: Flow, organt_id, body, kind, role) -> str:
        # 에이전트가 죽으면(SDK 메시지리더 크래시·서브프로세스 SIGTERM 등) 같은 세션으로 되살려 재시도.
        # State는 organt_id별 파일에 영속되므로 새 인스턴스가 세션을 이어간다(전체 워크플로우 보호).
        flow.last_activity = time.monotonic()   # 진행 신호(턴 시작) — 무진행 워치독 갱신
        last = ""
        for attempt in range(3):
            server = build_guide_server(flow, organt_id, role)
            organt = self.organt_builder(organt_id, server, role, flow)
            try:
                # '…입력 중' 표시: 깨어난 Organt가 응답·작업을 작성하는 동안 현재 Task 스레드
                # (없으면 유저 채널)에 가시화. guide에 typing 없으면(테스트 등) 그냥 건너뜀.
                ch = (flow.current.thread_id if flow.current else None) or flow.user_channel
                tcm = getattr(self.guide, "typing", None)

                async def _do():
                    if tcm is not None:
                        async with tcm(ch, organt_id):
                            return await organt.handle(self._prompt(body, kind, role, organt_id, flow.leader))
                    return await organt.handle(self._prompt(body, kind, role, organt_id, flow.leader))

                # 리더 턴은 '흐름 전체'(중첩 워커 포함)를 품으므로 타임아웃 안 건다 — 워커 타임아웃이 행을 끊는다.
                # 워커(비-리더) 턴은 turn_timeout 초과 시 포기하고 '인프라 실패'로 반환(재시도·충원 금지 경로로
                # 라우팅). asyncio.wait_for가 행 코루틴을 취소 → ClaudeSDKClient __aexit__가 서브프로세스 정리.
                if role == "leader":
                    return await _do()
                return await asyncio.wait_for(_do(), timeout=self.turn_timeout)
            except asyncio.TimeoutError:
                self._log("agent_timeout", organt=organt_id, role=role, sec=self.turn_timeout)
                return (f"API Error: timeout — 동료({organt_id}) 서브프로세스가 {self.turn_timeout}s 무응답(행). "
                        f"단일흐름이라 인프라 문제로 간주(크래시와 동일) — 대체 채용 말고 잠시 뒤 재요청하거나 보고.")
            except Exception as e:
                last = f"(에이전트 {organt_id} 처리 실패: {e})"
                self._log("agent_revive", organt=organt_id, attempt=attempt + 1, err=str(e)[:100])
                await asyncio.sleep(2 * (attempt + 1))
        return last

    async def _ensure_deploy(self, flow, lead, result):
        """배포 가능한 산출물(package.json)인데 deploy가 안 불렸고 자격증명·DEPLOY_NAME이 있으면,
        리더에게 의존하지 않고 **SYS가 직접 deploy_sync로 배포**한다(리더가 빼먹는 누락 구멍 차단).
        deploy_sync가 라이브 URL 실제 응답까지 확인하므로, 거짓 성공이 아니라 진짜 배포가 보장된다."""
        ws = str(flow.workspace) if flow.workspace else ""
        deployable = bool(ws) and os.path.exists(os.path.join(ws, "package.json"))
        gh, ghu = os.environ.get("GH_PAT"), os.environ.get("GH_USER")
        rk, owner = os.environ.get("RENDER_KEY"), os.environ.get("RENDER_OWNER")
        name = os.environ.get("DEPLOY_NAME")
        if flow.deployed or not (deployable and name and gh and ghu and rk and owner):
            return result
        try:
            import anyio
            from .deploy import deploy_sync
            dep = await anyio.to_thread.run_sync(deploy_sync, ws, name, gh, ghu, rk, owner)
            flow.deployed = dep
            self._log("ensure_deploy", forced=True)
            return f"{result}\n\n[배포(SYS 강제)] {dep}"
        except Exception as e:
            return f"{result}\n\n(SYS 배포 강제 중 오류: {e})"

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

        self._reset_sessions()   # 새 요청 → 세션 초기화(이전 '이미 했다' 앵커링 차단)
        # 이전 흐름의 런타임 채용(예비→직군) 라벨 원복 — dict는 그대로 두고 내용만 갱신(빌더 클로저가 참조 중).
        self.bot_info.clear()
        self.bot_info.update(self._roster_labels)
        self._origin_request = (user_text or "").strip()   # 원문 보존 — 담당자가 요약·해석하기 전 '사용자가 실제로 한 말'
        proj = self.projects.get(int(channel_id))   # 이 채널이 등록된 프로젝트면 '개입'
        lead = proj["leader"] if proj else leader_id
        flow = Flow(self.guide, channel_id, self.guild_id, lead, self.bot_info)
        flow.register_project = lambda ch, name: self._register_project(ch, name, flow.workspace, flow.leader)
        # '기억'(직업 고정): 예비가 recruit로 직군을 받으면 그 직업을 다음 흐름에도 유지하도록 로스터 라벨에 반영
        # — 흐름 시작 때 _roster_labels로 원복되므로, 여기에 기록해야 채용한 직업이 지속된다(1봇 1직업의 연속성).
        flow.persist_role = lambda mid, role: self._roster_labels.__setitem__(int(mid), role)
        body = user_text
        if proj:                                     # 기존 프로젝트 개입 — 맥락 유지(재생성 X)
            flow.project_channel = int(channel_id)   # 기존 채널 재사용 → create_project는 no-op
            flow.workspace = proj["workspace"]
            flow.project_id, flow.intervention = proj["id"], proj
            body = (
                f"[프로젝트 {proj['id']} 개입 — 기존 산출물 수정] 이미 작업공간·산출물이 있습니다. create_project 다시 만들지 마세요.\n"
                f"사용자가 보고한 요청/증상: {user_text}\n\n"
                f"[개입도 정식 절차로 — 즉흥·독단 수정 금지] **당신 개인 견해로 곧장 파일을 고치지 마세요.** 아래 순서를 지키세요:\n"
                f"① 재현/확인: run으로 보고된 증상을 실제로 재현하고 관련 코드를 Read로 확인해 '진짜 원인'을 파악(스펙·추측에서 "
                f"유추 금지). ② Task 개설 + 목표 확정: create_task(members=고장난 부분의 도메인 담당자)로 수정 Task를 열고, "
                f"**재현된 사실과 사용자의 보고에 근거해** set_goal로 Purpose(무엇이 잘못됐나)·Goal(무엇이 되면 '고쳐짐'인가 — "
                f"측정가능)을 확정하세요. **목표(Goal)가 정해지기 전엔 파일 수정이 구조적으로 막힙니다.** 당신 생각이 아니라 "
                f"보고된 그 문제에만 한정 — 요청하지 않은 기능 추가·임의 개선 금지. ③ 위임: 그 도메인 owner에게 request(Work)로 "
                f"수정을 맡기세요(당신 도메인이면 직접). 혼자 다 떠안지 말 것. ④ 검증 후 마감: run으로 Goal 충족을 확인하고 "
                f"complete_task. 동작·물리·판정 문제는 server.js, 색·레이아웃·그리기 순서만 public/입니다.")
            self._log("intervention", project=proj["id"], text=user_text[:60])
        else:
            flow.workspace = self.workspace
        if root_id is not None:
            flow.start_root(root_id)
        flow.wake = lambda to, b, k: self.run_turn(flow, to, b, k, "member")
        flow.log = self._log                       # 관측: req_sent 등을 flow.jsonl로 영속
        self.active_flow = flow
        flow.last_activity = time.monotonic()

        async def _run_leader():
            flow.leader_segment = 1
            result = await self.run_turn(flow, lead, body, Kind.WORK, "leader")
            # 구조적 연속 실행: 턴 한도로 작업이 끊겼으면(진행 중 Task가 남았거나 '턴 한도' 표시)
            # 같은 세션으로 이어서 완료까지 재호출한다 — '턴 한도 = 무조건 中断' 결함 해소.
            cont = 0
            while ((flow.current is not None or "턴 한도 도달" in (result or ""))
                   and cont < self.max_continue):
                # 위임 도중 리더 턴이 끝나면(턴 한도) 깨우던 동료가 취소되며 베턴이 그 동료에 굳는다.
                # 그대로 리더를 다시 띄우면 '두 흐름'처럼 모든 요청이 '활성=동료'로 거부된다 →
                # 먼저 베턴을 리더로 강제 복구(고아 프레임 escalate-drain)한 뒤 이어간다.
                if flow.comm.alive != lead and not flow.comm.done:
                    guard = 0
                    while (flow.comm.alive != lead and not flow.comm.done
                           and flow.comm.open_requests and guard < 64):
                        try:
                            flow.comm.escalate("continue 전 베턴 복구(위임 고아 정리)")
                        except CommError:
                            break
                        guard += 1
                    self._log("baton_recover_continue", alive=flow.comm.alive, recovered=(flow.comm.alive == lead))
                cont += 1
                flow.leader_segment = cont + 1
                self._log("continue_incomplete",
                          task=(flow.current.task_id if flow.current else None), attempt=cont)
                result = await self.run_turn(flow, lead, _CONTINUE_BODY, Kind.WORK, "leader")
            return result

        leader_task = asyncio.create_task(_run_leader())
        try:
            # 무진행(행) 워치독: idle_timeout 동안 진행이 0이면 리더 턴 취소(리더-행 구멍 메움). 진행 중이면 무제한.
            result = await self._await_with_idle_watchdog(leader_task, flow)
        except asyncio.CancelledError:
            result = (f"(흐름 자동 중단: 약 {self.idle_timeout // 60}분간 아무 진행(요청·파일작성·실행)이 없어 '행'으로 "
                      f"판단했습니다 — 리더/동료 서브프로세스가 멈춘 듯합니다(환경 불안정). 지금까지 산출물은 작업공간에 "
                      f"남아 있습니다. 다시 시도하거나 반복되면 잠시 뒤 재요청하세요.)")
            self._log("flow_idle_aborted")
        except Exception as e:                     # 리더가 죽어도 흐름은 닫고 보고한다
            result = f"(리더 처리 중 오류: {e})"
        # 배포 강제: 배포 가능한 산출물인데 deploy를 안 불렀으면 리더에게 '배포만' 한 번 더(누락 방지).
        result = await self._ensure_deploy(flow, lead, result)
        # 리더의 반환값 = 사용자에게 가는 Response(=보고). origin 프레임을 닫아 시작점 복귀.
        await self.guide.post(flow.user_channel, lead, format_response(result),
                              reply_to=flow.root_id)
        self._close_flow(flow, lead, result)
        flow.done, flow.final = True, result
        # 안전망: 리더가 complete_task로 명시적으로 닫지 않은 현재 Task는 '중단'으로 표시한다
        # (허위 완료 금지 — owner가 실제로 안 끝냈을 수 있으므로 '완료'로 둔갑시키지 않음).
        if flow.current is not None:
            flow.current.status.status = "중단"
            flow.current.status.result = (result or "")[:500]
            await flow.refresh(flow.current)
            flow.current = None
        # 프로젝트 요약 갱신(다음 개입 때 맥락으로 제공)
        if flow.project_channel:
            p = self.projects.get(int(flow.project_channel))
            if p:
                p["summary"] = (result or "")[:300]
                self._save_projects()
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
