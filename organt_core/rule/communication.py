"""Communication Rule — 단일 활성 '베턴'과 요청 스택(순수 로직, 네트워크 없음).

docs(Rule/Communication.md):
- 흐름은 User(SMS)에서 시작한다. Organt는 스스로 흐름을 시작하지 않는다.
- 활성(alive) Organt은 항상 1명. Request 시 sender sleep / receiver wake.
- Work Request는 이미 미완 Work를 가진(흐름에 참여 중인) Organt에게 보낼 수 없다
  (겹침·순환 방지 = busy-guard).
- Response는 스택 역순(LIFO)으로 close. 모든 요청이 닫히면 흐름은 시작점(origin)으로
  복귀하고 종료된다. → 항상 1명만 활성(단일흐름) = 토큰 절약·사이드이펙트 감소.
- Work Response 불만족 시 Redo, 한계 초과 시 위로 상신(escalate).

[병렬 — docs Communication.md 13–14행 "여럿(병렬)은 이 제약을 완화하는 Feature로 둔다"]
완화는 '서로 다른 흐름의 동시 진행'뿐이고, 흐름 '안'의 단일활성(베턴)은 불변이다. 흐름 간
안전은 흐름 수 상한(임의 숫자) 같은 가드가 아니라 **점유의 배타성**으로 보장한다: 전역 점유
장부(Engagement)에 의해 한 직원(봇)은 한 시점에 한 흐름에만 참여한다(현실의 '한 사람은 한
회의에만'). 동시 작업량의 자연 한도 = 직원 수. 장부는 SYS가 소유하고 흐름의 comm에
attach_engagement로 붙는다 — 모든 점유/해제는 request/respond/escalate 안에서만 일어나
(vote·meet·복구 경로 포함) 등록·해제가 구조적으로 대칭이다.

메시지 인코딩/파싱(`[Request]`/`[Response]` 포맷)은 protocol.py가 담당한다.
"""
import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import List, Optional

from ..protocol import Kind


class CommError(Exception):
    """통신 규약 위반(베턴/순서 등)."""


class RedoLimitExceeded(CommError):
    """Redo 한계 초과 → 상신(escalate)해야 함."""


class BusyInOtherFlow(CommError):
    """타 흐름에 점유된 동료에게 요청 — 규약 위반이 아니라 '지금 자리에 없음'이다.
    호출부(guide의 request 도구)가 같은 직군의 가용 동료·채용을 대안으로 안내한다."""

    def __init__(self, msg, to_id=None, holder_scope=None):
        super().__init__(msg)
        self.to_id = to_id
        self.holder_scope = holder_scope


class Engagement:
    """[전역 점유 장부 — 병렬 안전의 1기둥] 봇 단위 배타성: 한 봇은 한 시점에 한 흐름(스코프)에만
    참여한다. 어떤 Kind든 타 흐름이 점유한 봇에게는 요청할 수 없다(같은 봇이 두 채널에서 동시에
    '입력 중'이 되는 이중 존재 차단 — 흐름 '안'의 Info 규칙은 종전 그대로).

    - 인메모리 전용: 재시작이 곧 초기화이고, 부팅 복구가 흐름을 다시 세우며 재등록한다(영속 불필요).
    - 유령 자가 치유: holder 조회 때 스코프 생존 검사(is_live)로 끝난/죽은 흐름의 점유를 지운다 —
      예외로 해제가 누락돼도 봇이 영구히 '바쁨'으로 굳지 않는다.
    """

    def __init__(self, is_live=None):
        self._m: dict = {}        # bot_id → scope(점유 중인 흐름)
        self._is_live = is_live   # scope -> bool 콜백(없으면 항상 살아있다고 본다)

    def holder(self, bot_id):
        scope = self._m.get(bot_id)
        if scope is None:
            return None
        if self._is_live is not None and not self._is_live(scope):
            self._m.pop(bot_id, None)   # 유령 점유(끝난 흐름) 자가 치유
            return None
        return scope

    def engage(self, bot_id, scope):
        self._m[bot_id] = str(scope)

    def release(self, bot_id, scope):
        """자기 스코프의 점유만 해제(타 흐름 점유를 실수로 풀지 않게)."""
        if self._m.get(bot_id) == str(scope):
            self._m.pop(bot_id, None)

    def release_scope(self, scope):
        """흐름 종료 안전망 — 그 스코프의 모든 점유를 일괄 해제(예외 경로 누락 대비)."""
        for b in [b for b, s in list(self._m.items()) if s == str(scope)]:
            self._m.pop(b, None)

    def busy_elsewhere(self, bot_id, scope) -> bool:
        h = self.holder(bot_id)
        return h is not None and h != str(scope)


@dataclass
class Frame:
    """열린 요청 한 건(요청 스택의 프레임)."""
    from_id: int
    to_id: int
    request_id: str
    kind: str
    body: str = ""        # [정밀 복구] 이 위임의 원문 — 전체 체인 영속·끊김 시 가장 깊은 워커를 원문으로 재개


class CommunicationManager:
    """단일 활성 베턴 + 요청 스택.

    - 활성(alive) Organt은 항상 1명. Request 시 sender sleep / receiver wake.
    - Response는 스택 역순(LIFO)으로 close.
    - 모든 요청이 닫히면 흐름이 시작점(origin)으로 복귀하고 종료된다.
    """

    def __init__(self, origin_id: int, redo_limit: int = 2):
        self.origin = origin_id
        self.alive = origin_id
        self._stack: List[Frame] = []
        self.history: list = []
        self.done = False
        self.redo_limit = redo_limit
        self._redo_counts: dict = {}
        self._delivered: set = set()   # '완료 응답'까지 닫힌 (위임자→owner) Work 쌍 → 재위임=Redo 판별
        self.escalations: list = []
        self.escalated_to_origin = False
        self._engagement = None        # 전역 점유 장부(SYS가 attach) — 없으면 종전(흐름 내) 규칙만
        self._scope = None

    @property
    def open_requests(self) -> List[Frame]:
        return list(self._stack)

    @property
    def engagement(self):
        return self._engagement

    @property
    def scope(self):
        return self._scope

    def attach_engagement(self, engagement, scope) -> None:
        """전역 점유 장부에 이 흐름을 연결한다(SYS가 흐름 예약 직후, 첫 프레임 전에 호출).
        복원된 스택(부팅 복구 등)이 있으면 그 참여자들을 재등록한다 — 장부는 인메모리라
        재시작 후엔 비어 있고, 복구 흐름이 여기서 점유를 되살린다."""
        self._engagement = engagement
        self._scope = str(scope)
        for f in self._stack:
            self._engage_frame(f)

    def _engage_frame(self, frame: "Frame") -> None:
        if self._engagement is None:
            return
        for b in (frame.from_id, frame.to_id):
            if b != self.origin:                       # origin(User/SMS)은 봇이 아니다
                self._engagement.engage(b, self._scope)

    def _release_closed(self, frame: "Frame") -> None:
        """프레임이 닫힌 뒤, 더는 어떤 열린 프레임에도 없는 봇의 점유를 해제한다.
        LIFO 사슬 불변식: pop 후 frame.from_id는 남은 top의 to_id와 같아(또는 흐름 종료)
        '일하다 만 봇'이 풀리는 일은 없다 — 응답을 마친 봇만 즉시 회사 풀로 돌아간다."""
        if self._engagement is None:
            return
        parts = self._participants()
        for b in (frame.from_id, frame.to_id):
            if b != self.origin and b not in parts:
                self._engagement.release(b, self._scope)

    def is_alive(self, organt_id) -> bool:
        return self.alive == organt_id

    def check_request(self, from_id: int, to_id: int, kind: str = "work") -> None:
        """Request 가능 여부를 검증한다(상태 변경 없음). 불가하면 CommError."""
        if self.done:
            raise CommError("흐름이 이미 종료되었습니다.")
        if from_id != self.alive:
            raise CommError(f"활성 Organt만 요청할 수 있습니다(현재 활성={self.alive}).")
        if from_id == to_id:
            raise CommError("자기 자신에게는 Request할 수 없습니다.")
        # [전역 점유 — 흐름 간 배타성] 타 흐름이 점유한 동료에게는 Kind 불문 요청 불가(한 직원은
        # 한 번에 한 흐름). 흐름 '안'의 Info(되묻기·합의)는 이 검사와 무관하게 종전대로 허용된다.
        if (self._engagement is not None and self._scope is not None and to_id != self.origin
                and self._engagement.busy_elsewhere(to_id, self._scope)):
            held = self._engagement.holder(to_id)
            raise BusyInOtherFlow(
                f"{to_id} 는 지금 다른 흐름({held})에 참여 중입니다 — 한 직원은 한 번에 한 흐름에만 "
                f"참여합니다. 같은 직군의 다른 동료에게 맡기거나 recruit로 채용하세요.",
                to_id=to_id, holder_scope=held)
        if to_id in self._ancestors():
            # 상위 동료는 '내 응답'을 기다리며 멈춰 있다 → 되물으면 재진입(세션 충돌). 금지.
            raise CommError(
                f"{to_id} 는 당신의 응답을 기다리며 멈춰 있습니다(재진입 불가). "
                f"그 동료의 산출물을 Read 하거나, 멈춰있지 않은 다른 동료에게 물으세요.")
        if self._is_work(kind) and to_id in self._participants():
            raise CommError(f"{to_id} 는 미완 Work 보유/흐름 참여 중 → Work Request 거부(겹침·순환 방지).")

    def request(self, from_id: int, to_id: int, request_id, kind: str = "work", body: str = "") -> Frame:
        self.check_request(from_id, to_id, kind)
        frame = Frame(from_id, to_id, str(request_id), kind, body=body)
        self._stack.append(frame)
        self._engage_frame(frame)        # 전역 점유 등록(요청자·수신자 — 베턴 점유와 같은 지점)
        self.alive = to_id  # receiver wake, sender sleep
        self.history.append(("request", from_id, to_id, str(request_id), kind))
        return frame

    def respond(self, from_id: int, result: str = "accept", text: str = "") -> Frame:
        if self.done:
            raise CommError("흐름이 이미 종료되었습니다.")
        if not self._stack:
            raise CommError("응답할 열린 요청이 없습니다.")
        if from_id != self.alive:
            raise CommError(f"활성 Organt만 응답할 수 있습니다(현재 활성={self.alive}).")
        frame = self._stack.pop()       # 역순(LIFO) close
        self.alive = frame.from_id      # 요청자 wake
        self.history.append(("respond", from_id, frame.from_id, frame.request_id, result))
        if result == "accept" and self._is_work(frame.kind):
            # 이 (위임자→owner) Work가 '완료 응답'까지 닫혔다 → 다음 같은 위임은 새 위임이 아니라
            # 직전 산출물의 Redo다(docs §5). 되묻기(clarify)는 미완이라 'accept'가 아니므로 안 잡힌다.
            self._delivered.add((frame.from_id, frame.to_id))
        if not self._stack:             # 모든 요청 닫힘 → 시작점 복귀·종료
            self.done = True
            self.alive = self.origin
        self._release_closed(frame)      # 응답을 마친 봇은 즉시 회사 풀로(타 흐름이 쓸 수 있게)
        return frame

    def _participants(self) -> set:
        """현재 흐름에 참여 중인(미완 Work 보유) Organt 집합."""
        s = {self.origin}
        for f in self._stack:
            s.add(f.from_id)
            s.add(f.to_id)
        return s

    def _ancestors(self) -> set:
        """응답을 기다리며 멈춰있는(=재진입 불가) 상위 Organt들(스택의 요청자들)."""
        return {f.from_id for f in self._stack}

    def direct_delegator(self, organt_id):
        """organt_id가 지금 응답을 빚지고 있는 '직속 위임자'(top 프레임의 요청자). 없으면 None.
        이 동료에게는 재진입 대신 '확인요청 반환'(베턴을 질문과 함께 돌려줌)이 허용된다."""
        if self._stack and self._stack[-1].to_id == organt_id:
            return self._stack[-1].from_id
        return None

    def is_busy(self, organt_id) -> bool:
        """미완 Work 보유(또는 흐름 참여 중)인가 → Work Request 금지 대상."""
        return organt_id in self._participants()

    def delivered_work(self, delegator: int, owner: int) -> bool:
        """delegator가 owner에게 Work를 위임해 '완료 응답'까지 받은 적이 있는가. True면 같은
        owner에게 또 보내는 Work는 '새 위임'이 아니라 Redo(직전 산출물 보완)다 — docs §5."""
        return (delegator, owner) in self._delivered

    def reset_task_tracking(self) -> None:
        """새 Task(=새 산출물 단위)가 열리면 '완료/Redo' 추적을 비운다 — Redo는 '같은 Task의 같은
        산출물'을 다시 맡길 때만 성립한다(다른 Task의 같은 동료는 새 위임이지 Redo가 아님)."""
        self._delivered.clear()
        self._redo_counts.clear()

    @staticmethod
    def _is_work(kind) -> bool:
        """Kind가 Work인지 (protocol.Kind 또는 'work'/'Work' 문자열 모두 인식)."""
        if isinstance(kind, Kind):
            return kind == Kind.WORK
        return str(kind).strip().lower() == "work"

    def redo(self, from_id: int, to_id: int, request_id, body: str = "") -> Frame:
        """직전 응답이 불만족 → 같은 대상에 재요청(Redo). 한계 초과 시 RedoLimitExceeded."""
        if from_id != self.alive:
            raise CommError(f"활성 Organt만 redo할 수 있습니다(현재 활성={self.alive}).")
        key = (from_id, to_id)
        count = self._redo_counts.get(key, 0) + 1
        if count > self.redo_limit:
            raise RedoLimitExceeded(f"redo 한계({self.redo_limit}) 초과 → 상신 필요.")
        self._redo_counts[key] = count
        self.history.append(("redo", from_id, to_id, str(request_id), count))
        return self.request(from_id, to_id, request_id, kind="work", body=body)

    def escalate(self, reason: str = "") -> Frame:
        """top 요청을 강제 close하고 상신(위로). 타임아웃/죽은 Organt로 인한 교착 방지."""
        if self.done:
            raise CommError("흐름이 이미 종료되었습니다.")
        if not self._stack:
            raise CommError("상신할 열린 요청이 없습니다.")
        frame = self._stack.pop()       # 강제 close
        self.alive = frame.from_id      # 요청자에게 상신(위로)
        self.escalations.append((frame.request_id, reason))
        self.history.append(("escalate", frame.to_id, frame.from_id, frame.request_id, reason))
        if not self._stack:
            self.done = True
            self.alive = self.origin
            self.escalated_to_origin = True
        self._release_closed(frame)     # 강제 close(복구 경로)도 점유 해제 대칭 유지
        return frame

    def report_up_to(self, reporter_id: int, owner_id: int, reason: str = "") -> List[dict]:
        """[상류 선행작업 되감기 — 임의 깊이·대상 일반형] reporter(현재 활성)가 상류 owner에게
        '네 선행작업이 끝나야 내가 진행 가능'을 보고한다(질문이 아니라 Work 선행 이슈).

        A→B→C 에서 C가 A에게 Work를 요청하는 건 'A가 선행작업을 안 끝낸 채 위임했다'는 신호다 —
        직접 호출(잘못된 호출)을 막고 보고 체계로 거슬러 올린다. owner의 위임 프레임에 닿을 때까지
        LIFO로 되감되(중간 동료는 **relay만** — 자기 일 아닌 걸 떠안지 않음), owner '위'(origin쪽)
        프레임은 건드리지 않는다(**부분 되감기** — owner가 루트가 아니어도 됨). 되감으며 닫은
        (owner→…→reporter) 서브체인을 돌려줘, owner 해결 후 그 경로로 재하강·재개하게 한다.

        하드코딩 없음(임의 깊이·임의 대상): A→B→C→D→E 에서 E.report_up_to(B) → C·D relay,
        alive=B, A→B 프레임 유지, 서브체인 [B→C, C→D, D→E] 반환. E.report_up_to(A) 면 끝까지
        올라가 흐름이 origin 복귀(done). (reporter,owner) 쌍이 무엇이든 같은 루프가 처리한다.

        주의: 되감은 동료(reporter·relay)는 **점유 해제하지 않는다** — 곧 재하강으로 재개될
        '일시정지'이지 '완료'가 아니므로 이 흐름에 계속 묶여 있어야 한다(타 흐름 탈취 방지).
        """
        if self.done:
            raise CommError("흐름이 이미 종료되었습니다.")
        if reporter_id != self.alive:
            raise CommError(f"활성 Organt만 보고할 수 있습니다(현재 활성={self.alive}).")
        if reporter_id == owner_id:
            raise CommError("자기 자신에게는 보고할 수 없습니다.")
        if owner_id not in self._ancestors():
            raise CommError(
                f"{owner_id}는 응답을 기다리는 상류 위임자가 아닙니다 — 상류 보고(되감기) 대상이 아닙니다.")
        sub_chain: List[dict] = []
        # owner가 from_id인 프레임을 닫는 순간 alive=owner가 된다 — 그때까지 top부터 relay-close.
        # (해제 안 함: 재하강 재개 대상이라 흐름에 묶인 채 둔다.)
        while self.alive != owner_id:
            frame = self._stack.pop()
            sub_chain.append({"from": int(frame.from_id), "to": int(frame.to_id),
                              "kind": str(getattr(frame, "kind", "work")),
                              "body": getattr(frame, "body", "") or ""})
            self.alive = frame.from_id
            self.history.append(("report_relay", int(frame.to_id), int(frame.from_id),
                                 frame.request_id, reason))
        sub_chain.reverse()                 # owner→…→reporter 순(재하강 replay용)
        if not self._stack:                 # owner가 origin이었다 → 흐름이 시작점에 닿아 종료
            self.done = True
            self.alive = self.origin
        self.history.append(("report_up", int(reporter_id), int(owner_id), len(sub_chain), reason))
        return sub_chain

    def restore_chain(self, frames: list) -> int:
        """[정밀 복구 — 내부 상태 복원(2026-06-23, 사용자)] 끊긴 위임 체인(A→B→C)을 *채팅 재발행 없이*
        comm 스택으로 그대로 재구성한다. frames=[{from,to,kind,body}, ...] (위→아래 순 = active_chain).

        스택을 원래대로 쌓고 alive=가장 깊은 워커(체인 끝)로 둔다 — 그 워커부터 재개하면 끝났을 때
        respond가 C→B→A로 자연 unwind돼 **각자 범위가 보존**된다: C는 C 일, B는 B의 통합(C 산출물),
        A는 A의 통합(B 산출물). 종전 평탄화(리더→C 직접 1요청)는 B를 빼먹어 C/리더가 B 일까지 떠안았다
        (사용자: '범용적 잘못된 구현'). 이건 A→B→C를 채팅으로 다시 치는 게 아니라 **상태 복원**이다 —
        끊긴 C에서 바로 재개. 반환=가장 깊은 워커(재개 대상)."""
        if self.done:
            raise CommError("이미 종료된 흐름은 체인 복원 불가")
        self._stack = [
            Frame(int(f.get("from")), int(f.get("to")), str(f.get("request_id") or "recover"),
                  str(f.get("kind") or "work"), body=(f.get("body") or ""))
            for f in (frames or [])
        ]
        if self._stack:
            self.alive = self._stack[-1].to_id    # 가장 깊은 워커(체인 끝)부터 재개
            self.done = False
        self.history.append(("restore_chain", len(self._stack), self.alive))
        return self.alive


# ══ [팀·역량 라우팅 Rule — guide_tools에서 §7 rule/communication로 이관] ══
# '누구에게 위임하나'(능력표 _CAPS·직군·전역 점유)를 판정하는 소통 Rule. 잘못된 병합을 원래대로 복원.
# flow는 duck-typed 인자(Flow 임포트 불필요).
def _kw(*kws):
    """키워드 중 하나라도 문자열에 있으면 True인 술어 생성(능력 need/cover 판정용)."""
    return lambda s: any(k in s for k in kws)


# 능력 표 — (표시명, need(goal 소문자)→bool, cover(labels 소문자 합본)→bool). 고신호만(과채용 최소):
# 그 능력이 *작업의 실질 축*일 때만 need=True. cover는 관대(누군가 plausibly 덮으면 갭 아님).
# 일반화 동기(2026-06-22 사용자 '데브옵스·DBA 채용이 안 보인다'): 단일 AI/ML만 보던 탓에 반복 수요인
# 공공데이터 수집이 게이트에 안 걸려 흡수됐고(실데이터를 합성·가짜로 위장하는 사고의 *상류* 원인),
# 배포 인프라는 아무도 전담 안 해 리더에 귀속됐다(P-028 배포 1인 루프). 기능으로 식별(직군 타이틀 X).
_CAPS = [
    # AI/ML 모델링 — 모델 학습·예측이 핵심인데 AI/ML 직군이 없을 때(백엔드는 cover 아님 — 별도 전문성).
    ("AI/ML(모델 학습·예측)",
     lambda t: (_kw("학습시키", "머신러닝", "딥러닝", "신경망", "ml 모델", "예측 모델", "ai 모델")(t)
                or ("ai" in t and _kw("학습", "예측", "모델")(t))),
     _kw("ai", "머신", "딥러닝", "인공지능", "ml", "데이터 과학", "데이터 사이언", "data scien", "machine learn")),
    # 실데이터 수집·파이프라인 — 실/공공 데이터를 받아와 쓰는 게 전제일 때(백엔드/AI가 흡수하던 영역이라
    # 백엔드는 cover 아님 — 전담 데이터 직군 강제 → 데이터엔지니어↔AI엔지니어 핸드오프 협업도 생긴다).
    ("실데이터 수집·파이프라인",
     lambda t: (_kw("공공데이터", "공공 데이터", "실데이터", "실제 데이터", "오픈데이터", "open data")(t)
                and _kw("받아", "수집", "연동", "활용", "파이프라인", "크롤", "가져", "fetch", "적재")(t)),
     _kw("데이터 엔지니", "데이터엔지니", "data eng", "데이터 수집", "데이터 파이프", "etl", "데이터 분석")),
    # 데이터 영속·DB — 계정·기록·랭킹 등 지속 저장이 핵심일 때. 기본 CRUD는 백엔드가 덮으니 백엔드·DBA가
    # 둘 다 없을 때만 갭(과채용 방지 — 백엔드 있으면 발동 안 함).
    ("데이터 영속·DB",
     _kw("데이터베이스", "데이터 베이스", "database", "영속 저장", "계정", "로그인", "회원가입",
         "랭킹 저장", "기록 저장", "쿼리 최적"),
     _kw("dba", "데이터베이스", "데이터 베이스", "백엔드", "backend", "서버 개발")),
    # 배포·인프라(DevOps) — 배포 파이프라인·운영 자동화가 *명시적으로* 요구될 때만(평범한 웹 배포는 표준
    # 파이프라인이 처리 → 안 걸림). 키워드를 좁혀 과채용 방지.
    ("배포·인프라(DevOps)",
     _kw("ci/cd", "cicd", "파이프라인 구축", "도커", "컨테이너 오케", "쿠버네티스", "kubernetes",
         "오토스케일", "무중단", "로드밸런", "인프라 구축", "운영 자동화", "sre"),
     _kw("devops", "데브옵스", "인프라", "sre", "배포 엔지니", "플랫폼 엔지니")),
]


def _capability_gaps(goal_text, labels):
    """목표가 요구하는 전문 능력 중 팀(라벨들)이 *아무도 보유 못 한* 것 — 능력명 리스트. 리더가 자기 직군
    밖 도메인을 흡수(언더스태핑)하는 걸 set_goal에서 잡기 위함. 기능 식별(직군 타이틀 하드코딩 아님)."""
    t = str(goal_text or "").lower()
    have = " ".join(str(l or "").lower() for l in (labels or []))
    return [name for name, need, covered in _CAPS if need(t) and not covered(have)]


def _needed_caps_coverage(goal_text, labels):
    """목표가 *요구하는* 능력(need True)별 '덮는 팀원 수' {능력명: 수}. 깊이 게이트가 '필요 능력이 다 1명뿐'
    (그 도메인 품질이 한 사람 지능에 인질)인지 보는 데 쓴다 — 갭(0)은 staffing이 먼저 잡으므로 여기선 1명 이상 전제."""
    t = str(goal_text or "").lower()
    out = {}
    for name, need, covered in _CAPS:
        if need(t):
            out[name] = sum(1 for l in (labels or []) if covered(str(l or "").lower()))
    return out


def _offdomain_capability_hit(flow, to, body):
    """[직군밖 사전 차단 — P4 직군밖 거부 부활(2026-06-22)] Work body가 요구하는 능력(_CAPS need) 중 수신자(to)
    직군이 못 덮고 *다른* 팀원(리더 제외)이 덮는 것 → {능력명: [멤버]}. 비면 직군밖 아님(또는 덮는 전문가가
    없어 staffing 영역). 종전 [직군밖]는 받은 봇이 거부하는 사후 채널인데 1회만 쓰였다(봇은 받으면 그냥 흡수)
    — 이건 *위임 전에* 능력표로 잡아 그 전문가에게 리다이렉트(P-022 백엔드가 AI·data 흡수 차단). 의식적 예외는
    body '[직군초과: 사유]'. 능력표 밖 도메인(사운드↔VFX 등)은 봇-side [직군밖] 반려가 백스톱."""
    if "[직군초과" in (body or ""):
        return {}
    tl = (flow._info(to) or "").lower()
    bn = [name for name, need, covered in _CAPS if need((body or "").lower()) and not covered(tl)]
    if not bn:
        return {}
    hit = {}
    for name, need, cov in _CAPS:
        if name in bn:
            ms = [m for m in flow.current.team if m != to and m != flow.leader
                  and cov((flow._info(m) or "").lower())]
            if ms:
                hit[name] = ms
    return hit


# 채용 대기 인력(직군 미배정). recruit(role=…)로 런타임에 '게임 기획자·UX 디자이너' 등 필요한 직군으로
# 채용해 합류시킨다. 로스터에서 라벨이 '예비'인 봇들이며, 첫 '전원 기획'엔 안 들어가고 필요할 때 합류한다.
_SPARE_LABEL = "예비"


def _is_spare(flow, oid) -> bool:
    return (flow._info(oid) or "").strip().startswith(_SPARE_LABEL)


def _norm_job(name: str) -> str:
    return " ".join((name or "").split()).casefold()


# 겸직 라벨 구분자: '백엔드·QA' = 주직군 + 부직군. 겸직은 예외(예비 0명 또는 유사 직무)에서만,
# 봇당 최대 2개 — 더하기만 하던 시절의 '직군 5~6개 스택'(라이브 관측)으로 회귀하지 않기 위한 한도.
_JOB_SEP = "·"


def _jobs_of(label) -> List[str]:
    """라벨 → 보유 직군 목록('백엔드·QA' → ['백엔드','QA']). 단일 직군이면 1개짜리 리스트."""
    return [j.strip() for j in str(label or "").split(_JOB_SEP) if j.strip()]


def _job_tokens(name: str):
    return {t.casefold() for t in (name or "").split() if t}


def _free_alternatives(flow, me_id, to) -> str:
    """[전역 점유] 타 흐름에 점유된 to 대신 '지금 가용한 같은 직군 동료'와 채용 옵션을 안내문으로.
    재시도(폴링) 대신 구조적 선택지를 줘서, 점유 거부가 막다른 길이 아니라 분기점이 되게 한다."""
    eng, scope = flow.comm.engagement, flow.comm.scope
    jobs = {_norm_job(j) for j in _jobs_of(flow._info(to) or "")} - {""}
    alts = []
    for b in flow.pool:
        if b in (to, me_id) or _is_spare(flow, b):
            continue
        if jobs and not (jobs & {_norm_job(j) for j in _jobs_of(flow._info(b) or "")}):
            continue
        if eng is not None and scope is not None and eng.busy_elsewhere(b, scope):
            continue
        alts.append(f"{flow._info(b)}(id {b})")
    spares = [s for s in flow.pool if _is_spare(flow, s)]
    parts = []
    if alts:
        parts.append("지금 가용한 같은 직군 동료: " + ", ".join(alts[:4]))
    if spares:
        parts.append(f"또는 recruit(role=…)로 예비 {len(spares)}명 중 채용")
    return ("; ".join(parts) if parts else
            "지금은 같은 직군의 가용 동료가 없습니다 — 다른 직군 동료로 진행 가능한 부분을 먼저 하거나, "
            "불가하면 그 사정을 보고에 남기세요")


# ── [협업 라우팅 헬퍼 — guide_tools에서 이관] 멤버 해석·중복제거·변형직군 매칭·응답 실질성 ──
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


def _find_variant_job(name: str, existing) -> Optional[str]:
    """기존 직군과 '이름은 다른데 토큰을 공유'하면 변형(중복 생성) 의심으로 그 기존 직군을 돌려준다.
    recruit가 자유 텍스트 직군명을 받다 보니 흐름마다 'VFX 전문가'/'VFX 아티스트' 같은 변형이 새 역할로
    계속 불어났다(중복 생성 오류의 뿌리). 무엇이 '정답 이름'인지는 시스템이 정하지 않는다(하드코딩 금지)
    — 같은 이름(공백·대소문자 무시)은 기존 역할 재사용이라 통과시키고, 변형만 멈춰 세워 에이전트가
    '재사용'인지 '진짜 새 직군'인지 명시하게 한다."""
    mine_n, mine_t = _norm_job(name), _job_tokens(name)
    if not mine_t:
        return None
    if any(_norm_job(ex) == mine_n for ex in existing):
        return None                        # 같은 이름이 이미 있음 → 그대로 재사용(변형 아님), 즉시 통과
    for ex in sorted(existing):            # 정렬: 같은 입력엔 같은 안내(메시지 결정성)
        if mine_t & _job_tokens(ex):
            return ex
    return None


# 협의로 '인정되는' Info인지 — 순수 응답확인 핑('응답 가능하신가요?')은 합의로 치지 않는다(빈 핑 차단).
# 짧은데 핑 문구가 거의 전부일 때만 비실질(긴 메시지는 핑 문구가 섞여도 실질로 본다).
_HOLLOW_PING = ("응답 가능", "응답가능", "응답 되시", "응답되시", "계신가요", "준비되셨", "들리시",
                "확인 가능하신", "ready?", "available?", "are you there", "are you available")


def _is_substantive(body: str) -> bool:
    b = (body or "").strip()
    if not b:
        return False
    low = b.lower()
    return not (len(b) <= 30 and any(h in low for h in _HOLLOW_PING))


# ── [협업 실행 헬퍼 — guide_tools에서 이관] 그룹핑·스레드 멤버십·병렬 포크수집 ──
async def _fork_collect(flow, me_id, members, body_of, kind=Kind.INFO):
    """[병렬 Info fork-join] '독립 의견 수집'(표결·회의 1라운드)을 동시에 돈다 — Communication.md
    13–14행("여럿(병렬)은 이 제약을 완화하는 Feature로 둔다")의 구현. 완화는 정확히 이 구간뿐:
    - 가지(branch)는 comm 프레임을 열지 않는다 → 가지 봇은 '활성'이 아니므로 request가 규약
      에러로 자연 차단된다(가지의 중첩 요청 금지가 프롬프트가 아니라 구조로 강제 — 답만 한다).
    - 회사 풀 관점은 전역 점유로 일관: 수집 동안 가지 봇은 점유돼 타 흐름이 못 집어가고, 끝나면
      즉시 풀로 돌아간다. 타 흐름 점유/이 흐름에서 위임 보유 중인 멤버는 건너뛴다(부분 조인 —
      일부 멤버 때문에 수집 전체가 막히지 않는다).
    - 행 안전: 각 가지는 워커 침묵 워치독이 종결을 보장 → 조인이 영원히 안 닫히는 일이 구조적으로
      없다. 동시 폭은 ORGANT_FORK_FAN(기본 3)으로 묶는다(토큰 속도 운영 노브, 1이면 직렬과 동일).
    kind: 가지의 작업 종류 — Info(의견 수집, 기본)면 훅이 가지의 선구현(Write/Edit)을 종전대로
    차단한다(flow.fork_kind로 프레임 없는 가지에 게이트 연결; Work 가지는 휴면 — 호출부 없음).
    수집 동안 flow.fork_active를 올려 신규 요청/중첩 수집을 [대기]로 막는다 — CLI가 같은 턴에
    병렬 도구 호출을 내도(vote+request 등) 가지와 같은 동료를 이중으로 깨우는 일이 구조적으로 없다.
    반환: 멤버 순서 보존 [(member, res|None, 제외/실패 사유)]."""
    eng, scope = flow.comm.engagement, flow.comm.scope
    sem = asyncio.Semaphore(max(1, int(os.environ.get("ORGANT_FORK_FAN", "3"))))

    async def _branch(m):
        if flow.comm.is_busy(m):
            return (m, None, "(이 흐름에서 진행 중인 위임 보유 — 이번 수집에서 제외)")
        if eng is not None and scope is not None and eng.busy_elsewhere(m, scope):
            return (m, None, f"(타 흐름({eng.holder(m)}) 참여 중 — 이번 수집에서 제외)")
        if eng is not None and scope is not None:
            eng.engage(m, scope)
        flow.fork_kind[m] = kind
        try:
            async with sem:
                return (m, await flow.wake(m, body_of(m), kind), "")
        except Exception as e:
            return (m, None, f"(수집 실패: {e})")
        finally:
            flow.fork_kind.pop(m, None)
            if eng is not None and scope is not None and not flow.comm.is_busy(m):
                eng.release(m, scope)

    flow.fork_active = getattr(flow, "fork_active", 0) + 1
    try:
        return list(await asyncio.gather(*(_branch(m) for m in members)))
    finally:
        flow.fork_active -= 1


def _group_of(flow, team):
    return [(f"<@{i}>", flow._info(i)) for i in team]


async def _add_members(g, thread_id, member_ids):
    """Task 스레드에 팀원 추가(멤버십=팀). Guide에 메서드 없으면 건너뜀."""
    fn = getattr(g, "add_thread_members", None)
    if fn:
        await fn(thread_id, member_ids)


async def _say(flow, who, text):
    """[Communication] 회의·표결 발언을 '그 봇 본인 명의'로 스레드에 남긴다 — 독립 의견이 리더 명의
    묶음으로 게시돼 '중앙 공지'처럼 보이던 착시 제거(협업 가시성=실체). 실패는 조용히(best-effort).
    flow는 duck-typed(current·guide)."""
    g = flow.guide
    try:
        if flow.current:
            await g.post(int(flow.current.thread_id), who, text)
    except Exception:
        pass


async def vote(flow, me_id, args):
    """[Communication Rule 로직] vote — 팀 표결(독립 수집→집계). @tool 래퍼가 _ok로 감쌈(평문 반환)."""
    from .._util import _speech_clip, _react
    from .task import _ckpt
    g = flow.guide
    if flow.current is None:
        return ("오류: 진행 중인 Task가 없습니다. create_task 먼저 여세요.")
    opts = [o.strip() for o in str(args.get("options", "")).split(";") if o.strip()]
    if len(opts) < 2:
        return ("오류: options에 선택지 2개 이상을 ';'로 구분해 주세요.")
    voters = _resolve_members(args.get("members", ""), flow, flow.current.team) or \
             [m for m in flow.current.team if m != me_id]
    voters = [v for v in voters if v != me_id and not _is_spare(flow, v)]
    if not voters:
        return ("오류: 표결할 멤버가 없습니다.")
    if (any(not x.done() for x in getattr(flow, "inflight_tasks", ()))
            and flow.comm.alive != me_id and not flow.comm.done):
        return ("[대기] 직전 위임이 아직 진행 중입니다 — 표결은 그 결과를 받은 뒤 여세요.")
    if getattr(flow, "fork_active", 0) > 0:
        return ("[대기] 다른 의견 수집이 진행 중입니다 — 그 결과를 받은 뒤 여세요(중첩 수집 금지).")
    if flow.comm.done or flow.comm.alive != me_id:
        return (f"지금은 표결을 열 수 없습니다(활성={flow.comm.alive}) — 진행 중인 요청의 "
                   f"응답을 받은 뒤 다시 시도하세요.")
    question = str(args.get("question", "")).strip()

    detached = {"on": False}

    async def _run_vote():
        # [병렬 fork-join] 표는 서로 '독립'(앵커링 방지)이라 동시 수집이 의미를 바꾸지 않고
        # 시간만 줄인다 — 수집이 싸지면 표결을 아껴 쓰지 않게 된다(협동 빈도↑ = 품질).
        def body_of(v):
            return (f"[표결 — 독립 의견] 안건: {question}\n선택지: {' / '.join(opts)}\n"
                    f"동료들의 표는 보이지 않습니다(앵커링 방지). 당신의 전문가 관점에서 "
                    f"하나를 고르고 근거를 2줄 이내로. 반드시 형식: [표] 선택지명\n근거")
        tally, reasons = {o: 0 for o in opts}, []
        dom_picks = {o: set() for o in opts}   # 옵션 → 그 옵션을 고른 '도메인'들(같은 직군 중복 제거)
        for v, res, note in await _fork_collect(flow, me_id, voters, body_of):
            if res is None:
                reasons.append(f"{flow._info(v) or v}: {note}")
                continue
            m = re.search(r"\[표\]\s*([^\n]+)", res or "")
            pick = (m.group(1).strip() if m else "")
            chosen = next((o for o in opts if o in pick or pick in o), None)
            if chosen:
                # [동질 모델 — 표는 도메인(관점) 단위 집계] 같은 Claude·같은 직군 표는 같은 관점이라
                # N표가 아니라 1관점이다. 봇 수가 아니라 '다른 관점 수'로 세야 표결이 다양성을 반영
                # (같은 직군 3명이 같은 선택 = 3표가 아니라 그 직군 1표) — 봇 수 편향 제거. 도메인이
                # 갈리면(동질 모델이라 드묾) 각 옵션에 그 도메인을 1회씩 센다.
                _vd = {_norm_job(j) for j in _jobs_of(flow._info(v) or "")} - {""}
                _vdk = sorted(_vd)[0] if _vd else f"·{v}"
                if _vdk not in dom_picks[chosen]:
                    dom_picks[chosen].add(_vdk)
                    tally[chosen] += 1
            # [판정자 사본도 침묵 절단 금지] 리더는 이 근거로 표결을 '판정'한다 — 채널
            # 발언(400 안전망+잘림 표기)과 같은 내용이어야 한다. 종전 [:150] 하드컷은
            # 판정자가 동강난 근거로 결정하게 만들던 같은 부류의 결함(잘림 사건의 잔재).
            reasons.append(f"{flow._info(v) or v}: {(pick or '무효')} — {_speech_clip(res, 400)}")
            await _say(flow, v, f"[표] {(pick or '무효')} — {_speech_clip(res, 400)}")  # 본인 명의 발언
            if v in flow.current.team and v != flow.leader:
                flow.current.participated.add(v)        # 표결 참여 = 실질 협의 인정
        board = " / ".join(f"{o}: {n}관점" for o, n in tally.items())
        if flow.current is not None:
            record = f"[표결] {question}\n{board}\n" + "\n".join(reasons)
            flow.current.collab_notes = _speech_clip(
                (getattr(flow.current, 'collab_notes', '') + '\n\n' + record).strip(), 6000)
            _ckpt(flow)
        return (f"[표결 집계 — 도메인(관점) 단위] {question}\n{board}\n\n[각자의 선택·근거]\n"
                   + "\n".join(reasons)
                   + "\n\n(집계는 **도메인 단위** — 같은 직군 N명의 같은 선택은 동질 모델이라 1관점으로 "
                   + "합산(봇 수가 아니라 다른 관점 수). 참고일 뿐, 최종 판정은 당신(리더).)")

    inner = asyncio.ensure_future(_run_vote())
    flow.inflight_tasks.add(inner)
    inner.add_done_callback(flow.inflight_tasks.discard)
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        if not inner.done():
            detached["on"] = True
            if flow.log:
                flow.log("delegation_detached", to="vote", seg=flow.leader_segment)

            def _hand(t):
                try:
                    flow.detached_results.append(f"표결 완료 → {_speech_clip(t.result()['content'][0]['text'], 4000)}")
                except Exception:
                    pass
            inner.add_done_callback(_hand)
        raise


async def meet(flow, me_id, args):
    """[Communication Rule 로직] meet — guide_tools에서 이관(평문 반환, @tool이 _ok 래핑)."""
    from .._util import _speech_clip, _react, _dbg
    from .task import _ckpt
    g = flow.guide
    if flow.current is None:
        return ("오류: 진행 중인 Task가 없습니다. create_task 먼저 여세요.")
    members = _resolve_members(args.get("members", ""), flow, flow.current.team) or \
              [m for m in flow.current.team if m != me_id]
    members = [m for m in members if m != me_id and not _is_spare(flow, m)]
    if not members:
        return ("오류: 회의할 멤버가 없습니다.")
    if (any(not x.done() for x in getattr(flow, "inflight_tasks", ()))
            and flow.comm.alive != me_id and not flow.comm.done):
        return ("[대기] 직전 위임이 아직 진행 중입니다 — 회의는 그 결과를 받은 뒤 여세요.")
    if getattr(flow, "fork_active", 0) > 0:
        return ("[대기] 다른 의견 수집이 진행 중입니다 — 그 결과를 받은 뒤 여세요(중첩 수집 금지).")
    if flow.comm.done or flow.comm.alive != me_id:
        return (f"지금은 회의를 열 수 없습니다(활성={flow.comm.alive}) — 진행 중인 요청의 "
                   f"응답을 받은 뒤 다시 시도하세요.")
    topic = str(args.get("topic", "")).strip()
    try:
        rounds = max(1, min(3, int(str(args.get("rounds", "2")).strip() or "2")))
    except ValueError:
        rounds = 2

    async def _run_meet():
        minutes = []
        # 1라운드 = 독립 의견 fork(동시 수집) — 첫 입장은 서로를 안 보는 게 앵커링 없는
        # 진짜 다양성이고, 동시 수집이라 회의 비용도 준다(회의가 싸져야 자주 연다 = 협동성).
        def body_r1(m):
            return (f"[회의 1라운드 — 독립 의견] 주제: {topic}\n(이 라운드에선 동료 발언이 "
                    f"보이지 않습니다 — 앵커링 방지)\n당신({flow._info(m)})의 전문 관점 "
                    f"입장을 3~5줄(최대 1000자)로, 근거와 함께.")
        for m, res, note in await _fork_collect(flow, me_id, members, body_r1):
            cut = _speech_clip(res or note)   # 회의록·채널 발언은 같은 내용(기록 일치)
            line = f"[1R] {flow._info(m) or m}: {cut}"
            minutes.append(line)
            await _say(flow, m, f"[회의 1R] {cut}")  # 본인 명의 발언
            if res is not None and m in flow.current.team and m != flow.leader:
                flow.current.participated.add(m)        # 회의 발언 = 실질 협의 인정
        # 2라운드+ = 직렬 상호 토론(서로의 발언을 보며 동의/반박/보완) — 품질의 원천인
        # 순차 문맥은 병렬화 대상이 아니다(여기는 종전 그대로).
        for r in range(2, rounds + 1):
            for m in members:
                if flow.comm.done or flow.comm.alive != me_id:
                    break
                log_txt = "\n".join(minutes[-8:]) or "(아직 발언 없음)"
                body = (f"[회의 {r}라운드] 주제: {topic}\n지금까지의 발언:\n{log_txt}\n\n"
                        f"당신({flow._info(m)})의 차례입니다 — 앞 발언에 동의/반박/보완하며 "
                        f"당신 전문 관점의 입장을 3~5줄(최대 1000자)로. 맹목적 동의 금지(근거 필수).")
                try:
                    frame = flow.comm.request(me_id, m, "meet", Kind.INFO)
                except BusyInOtherFlow as e:
                    # 멤버 단위 사유(라운드 사이에 타 흐름이 데려감) — 회의를 끊지 않고 그
                    # 멤버만 건너뛴다(부분 진행). 베턴 경합(아래)과 달리 시스템 문제가 아니다.
                    minutes.append(f"[{r}R] {flow._info(m) or m}: (타 흐름({e.holder_scope}) "
                                   f"참여 중 — 이 라운드 불참)")
                    continue
                except CommError as e:
                    minutes.append(f"(회의 중단 — 베턴 경합: {str(e)[:60]})")
                    break
                try:
                    res = await flow.wake(m, body, Kind.INFO)
                except Exception as e:
                    res = f"(발언 실패: {e})"
                try:
                    flow.comm.respond(m, "accept", res)
                except CommError:
                    pass
                cut = _speech_clip(res)
                line = f"[{r}R] {flow._info(m) or m}: {cut}"
                minutes.append(line)
                await _say(flow, m, f"[회의 {r}R] {cut}")  # 본인 명의 발언
                if m in flow.current.team and m != flow.leader:
                    flow.current.participated.add(m)    # 회의 발언 = 실질 협의 인정
        if flow.current is not None:
            record = f"[회의] {topic} ({rounds}R)\n" + "\n".join(minutes)
            flow.current.collab_notes = _speech_clip(
                (getattr(flow.current, 'collab_notes', '') + '\n\n' + record).strip(), 6000)
            _ckpt(flow)   # 합의는 크래시-세이프(재개 위임에도 동봉되도록 스냅샷에 포함)
        return (f"[회의록] 주제: {topic} ({rounds}라운드, {len(members)}명)\n"
                   + "\n".join(minutes)
                   + "\n\n(수렴·확정은 당신(리더)의 몫 — 합의점을 정리해 set_goal/결정에 반영하세요.)")

    inner = asyncio.ensure_future(_run_meet())
    flow.inflight_tasks.add(inner)
    inner.add_done_callback(flow.inflight_tasks.discard)
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        if not inner.done():
            if flow.log:
                flow.log("delegation_detached", to="meet", seg=flow.leader_segment)

            def _hand(t):
                try:
                    flow.detached_results.append(f"회의 완료 → {_speech_clip(t.result()['content'][0]['text'], 4000)}")
                except Exception:
                    pass
            inner.add_done_callback(_hand)
        raise


async def parallel_work(flow, me_id, args):
    """[Communication Rule 로직] parallel_work — guide_tools에서 이관(평문 반환, @tool이 _ok 래핑)."""
    from .._util import _speech_clip, _react, _dbg
    from .task import _ckpt
    g = flow.guide
    # [RFC-006 Work-fork v1] 검증된 fork 인프라(_fork_collect: 점유·부분 조인·FAN·detach-safe
    # 코어)에 Work 의미론(쓰기 리스·owner·실작업 판정)을 입힌다 — alive-집합 전면 개편 없이
    # '병렬 실행 + 직렬 통합'(RFC-005 P1)을 연다. 가지는 comm 프레임을 열지 않으므로 재위임
    # 불가(구조 강제) — 실측 근거: P-009·P-010 워커의 중첩 request 0회(막히면 보고→리더 직렬).
    # [병렬 비활성화 — 단일흐름 안정성(2026-06-22 사용자 결정)] 병렬 fork는 가지 에이전트의 작업공간
    # cwd 불일치 + 게이트#9(비-fork 전문가 idle 오발) + 쓰기리스로 Write를 잃어 산출물 0 churn을
    # 유발했다(P-029 규명). 전제가 '단일흐름 안정성'이므로 병렬 Work를 끄고 직렬(request)로 돌린다 —
    # 통합·검증은 어차피 직렬이라 손실 없음. 테스트는 _parallel_enabled로 실경로 검증(경로 수정 후 해제).
    if not getattr(flow, "_parallel_enabled", False):
        return ("[병렬 비활성화] 병렬 Work는 현재 비활성화돼 있습니다 — 작업공간/게이트 정합 문제로 "
                   "가지의 산출물이 유실되는 불안정이 확인됐습니다(P-029). **독립 영역도 request(Work)로 "
                   "한 명씩 직렬 위임**하세요(단일흐름 안정성 우선 — 통합·검증은 어차피 직렬).")
    if flow.current is None:
        return ("오류: 진행 중인 Task가 없습니다. create_task 먼저 여세요.")
    goal = (flow.current.status.goal or "").strip()
    if not goal:
        return ("오류: Goal 확정 전엔 병렬 위임 불가 — set_goal 먼저(분할은 합의된 목표 위에서).")
    if getattr(flow, "fork_active", 0) > 0:
        return ("[대기] 다른 수집/병렬이 진행 중입니다 — 조인 후 시도하세요(중첩 병렬 금지).")
    if (any(not x.done() for x in getattr(flow, "inflight_tasks", ()))
            and flow.comm.alive != me_id and not flow.comm.done):
        return ("[대기] 직전 위임이 아직 진행 중입니다 — 결과를 받은 뒤 병렬을 여세요.")
    try:
        items = json.loads(args.get("assignments") or "")
        assert isinstance(items, list) and items
    except Exception:
        return ('형식 오류: assignments는 JSON 배열 — 예: [{"to":"12","files":"public/app.js","body":"..."}]')
    fan = max(1, int(os.environ.get("ORGANT_FORK_FAN", "3")))
    if len(items) < 2:
        return ("병렬은 2건부터입니다 — 1건은 request(Work)로 위임하세요.")
    if len(items) > fan:
        return (f"병렬 폭 초과({len(items)} > {fan}) — 가장 독립적인 {fan}건만 먼저, 나머지는 조인 후.")
    ws = str(getattr(flow, "workspace", "") or "")
    plan = []
    for it in items:
        try:
            to = int(str(it.get("to")).strip())
        except Exception:
            return (f"형식 오류: to가 봇 id가 아닙니다: {it.get('to')!r}")
        if to == me_id:
            return ("자기 자신에게는 병렬 위임 불가 — 자기 몫은 조인 후 직접.")
        if to not in flow.current.team:
            return (f"요청 거부: {flow._info(to) or to}는 이 Task 팀이 아닙니다 — 팀에 더한 뒤 위임하세요.")
        if _is_spare(flow, to):
            return (f"요청 거부: {flow._info(to) or to}는 직군 미배정('예비') — recruit로 직군 부여 먼저.")
        files = [f.strip() for f in str(it.get("files") or "").split(",") if f.strip()]
        if not files:
            return (f"형식 오류: {flow._info(to) or to}의 files가 비었습니다 — 병렬의 전제는 영역 분리(리스).")
        body = str(it.get("body") or "").strip()
        if not body:
            return (f"형식 오류: {flow._info(to) or to}의 body(지시)가 비었습니다.")
        paths = [os.path.realpath(os.path.join(ws, f)) for f in files]
        plan.append((to, paths, body))
    tos = [p[0] for p in plan]
    if len(set(tos)) != len(tos):
        return ("같은 동료에게 두 영역 동시 배정 — 한 건으로 합치세요.")
    # [토큰 중립 조건 ⓐ — 기계 강제] 영역 상호 배타: 일치/포함이면 거부(겹침은 통합 충돌→Redo→토큰 손실).
    for i in range(len(plan)):
        for j in range(i + 1, len(plan)):
            for a in plan[i][1]:
                for b in plan[j][1]:
                    if a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep):
                        return (f"영역 겹침 거부: {flow._info(plan[i][0])} ↔ {flow._info(plan[j][0])} "
                                   f"({os.path.basename(a)}) — 겹치는 작업은 직렬(request)로.")
    notes = getattr(flow.current, "collab_notes", "")
    m2 = {to: (paths, body) for to, paths, body in plan}

    def body_of(m):
        paths, body = m2[m]
        files_txt = ", ".join(os.path.relpath(p, ws) if ws else p for p in paths)
        t = (f"[병렬 Work — 이 영역의 책임자는 당신] 이 Task의 Goal: {goal}\n"
             f"**당신의 쓰기 영역(리스): {files_txt}** — 이 파일들에만 씁니다. 다른 가지가 다른 "
             f"영역을 동시 작업 중이므로 영역 밖은 Read 참고만 하고, 필요한 변경은 보고의 "
             f"[리스크]에 적으세요. 동료 재위임은 불가(병렬 가지) — 막히면 막힌 지점을 보고하면 "
             f"리더가 직렬로 풉니다. 직군 밖이면 첫 줄 `[직군밖] 필요직군` 반려.\n"
             f"직접 구현하고 run으로 검증한 뒤, 보고 계약([결과]/[변경]/[검증]/[리스크])으로 간결히.\n"
             f"[요청 맥락] {body}")
        if notes:
            t += f"\n[팀 협의 기록(회의·표결) — 준수]\n{_speech_clip(notes, 6000)}"
        return t

    acts0 = {to: flow.act_by.get(to, 0) for to in tos}
    if getattr(flow, "write_lease", None) is None:
        flow.write_lease = {}
    for to, paths, _b in plan:
        flow.write_lease[to] = paths
    if flow.log:
        flow.log("parallel_work", n=len(tos), to=",".join(map(str, tos)), seg=flow.leader_segment)

    async def _run_parallel():
        try:
            results = await _fork_collect(flow, me_id, tos, body_of, kind=Kind.WORK)
        finally:
            for to in tos:
                flow.write_lease.pop(to, None)   # 조인=리스 해제(겹침 게이트는 가지 동안만)
        out = []
        for m, res, note in results:
            acted = flow.act_by.get(m, 0) - acts0.get(m, 0)
            if res is not None and flow.current and m in flow.current.team and m != flow.leader:
                flow.current.participated.add(m)
            if flow.current:
                flow.current.work_delegated += 1
            mark = "" if acted > 0 else " ⚠실작업 0(계획만 — 같은 영역 직렬 재위임 고려)"
            await _say(flow, m, f"[병렬 보고] {_speech_clip(res or note, 1500)}")
            out.append(f"[{flow._info(m) or m}]{mark}\n{_speech_clip(res or note, 4000)}")
        if flow.current and not flow.current.owner:
            flow.current.owner = tos[0]   # 기존 규칙(첫 Work 수신자=owner)과 일관 — 통합 기준점
            if flow.act_by.get(tos[0], 0) > acts0.get(tos[0], 0) and any(
                    m == tos[0] and r is not None for m, r, _n in results):
                flow.current.owner_delivered = True
        if flow.log:
            flow.log("parallel_join", n=len(results), seg=flow.leader_segment)
        _ckpt(flow)
        return (f"[병렬 조인 — {len(results)}건]\n" + "\n\n".join(out)
                   + "\n\n(통합·교차 검증·마감은 직렬로 — 겹치는 후속 작업은 request(Work) 한 명에게.)")

    inner = asyncio.ensure_future(_run_parallel())
    flow.inflight_tasks.add(inner)
    inner.add_done_callback(flow.inflight_tasks.discard)
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        if not inner.done():
            if flow.log:
                flow.log("delegation_detached", to="parallel", seg=flow.leader_segment)

            def _hand(t):
                try:
                    flow.detached_results.append(
                        f"병렬 조인 → {_speech_clip(t.result()['content'][0]['text'], 4000)}")
                except Exception:
                    pass
            inner.add_done_callback(_hand)
        raise


async def recruit(flow, me_id, role, args):
    """[Communication Rule 로직] recruit — guide_tools에서 이관(평문 반환, @tool이 _ok 래핑)."""
    from .._util import _speech_clip, _react, _dbg
    from .task import _ckpt
    g = flow.guide
    role_name = (args.get("role") or "").strip()
    spec = (args.get("member") or "").strip()
    # [전문화 정책 — 범용 직군 금지(사용자 결정)] 범용(풀스택 등)은 모든 일을 흡수해 전문 채용을
    # 억제하고(라이브: AI·서버·데이터가 한 봇에 22건 집중) 병렬의 병목이 된다. 전문 직군으로 나눠 뽑는다.
    if role_name and any(g in _norm_job(role_name)
                         for g in ("풀스택", "풀 스택", "fullstack", "full stack", "full-stack",
                                   "제너럴", "generalist", "만능", "올라운드")):
        return (f"채용 거부(전문화 정책): '{role_name}' 같은 범용 직군은 두지 않습니다 — 범용은 모든 "
                   f"일을 흡수해 전문 채용을 막고 병렬의 병목이 됩니다(1봇 1직업 전문화가 회사 원칙). "
                   f"필요한 전문 직군으로 나눠 뽑으세요(예: 백엔드 / 프론트엔드 / AI 엔지니어 / 데이터 엔지니어).")
    # [직군 중복 생성 게이트 — 근본] recruit가 자유 텍스트 직군명을 받다 보니 흐름마다 변형 이름
    # ('VFX 전문가' 있는데 'VFX 아티스트')으로 '같은 도메인 직군'이 새 Discord 역할로 계속 불어났다.
    # 비교 풀은 현재 팀 라벨 + '서버의 커스텀 역할 전체'(직군 역할은 서버 영속이라, 토큰 유실/오프라인
    # 봇의 직군도 보인다). 변형이 감지되면 생성하지 않고 멈춰 세운다 — 재사용(기존 이름 그대로)이나
    # 명시적 신설(new_role='yes')은 에이전트가 정한다(시스템이 정답 이름을 정하는 하드코딩 아님).
    if role_name:
        existing_jobs = {j for v in flow.bot_info.values()
                         if v and not str(v).startswith(_SPARE_LABEL)
                         for j in _jobs_of(v)}   # 겸직 라벨은 구성 직군으로 풀어 비교
        fn_roles = getattr(g, "get_custom_role_names", None)
        if fn_roles and getattr(flow, "guild_id", None):
            try:
                existing_jobs |= set(await fn_roles(flow.guild_id) or [])
            except Exception:
                pass
        dup = _find_variant_job(role_name, existing_jobs)
        if dup and _norm_job(args.get("new_role") or "") not in ("yes", "y", "true", "1"):
            if flow.log:
                flow.log("recruit_variant_blocked", asked=role_name, existing=dup)
            return (f"직군 중복 의심으로 보류: '{role_name}'은(는) 이미 있는 직군 '{dup}'의 변형으로 "
                       f"보입니다(같은 도메인을 다른 이름으로 또 만들면 직군이 계속 불어납니다). 같은 일이면 "
                       f"role='{dup}' 그대로 다시 호출해 기존 직군으로 채용하세요. 정말 '{dup}'과(와) 다른 "
                       f"일을 하는 새 직군이 필요하면 new_role='yes'를 함께 줘 명시적으로 신설하세요.")
    if flow.current is None:
        # [예비 담당자 '자기 직군 우선'] Task 열기 전에 담당자가 자기 직군부터 정하는 건 허용한다 — 자기
        # 자신 + role 지정일 때만. 이래야 '예비'인 채로 create_project/create_task를 열어 화면(상태블록·동료
        # 프롬프트)에 '예비'로 박히는 걸 막는다(사용자가 본 '담당자가 예비로 들어옴'의 직접 원인). 다른 사람
        # 채용 등은 종전대로 Task가 먼저 있어야 한다.
        self_pick = _resolve_members(spec, flow, flow.pool) if spec else []
        if role_name and ((not spec) or (self_pick and self_pick[0] == me_id)):
            # 1봇 1직업: 이 분기는 '예비(무직)' 담당자용이다 — 이미 직군이 있는 봇이 자기 직군을
            # 덮어쓰면(디자이너→게임 기획자) 전문화 기억이 영속 오염된다(라이브 관측). 같은 직군
            # 재확인만 통과시키고, 다른 직군은 거부한다(필요하면 예비를 그 직군으로 뽑는 것).
            cur = (flow._info(me_id) or "").strip()
            new_label = role_name
            if cur and not _is_spare(flow, me_id):
                cur_jobs = _jobs_of(cur)
                if any(_norm_job(j) == _norm_job(role_name) for j in cur_jobs):
                    return (f"이미 '{role_name}' 직군을 보유하고 있습니다 — 그대로 진행하세요(변경 없음).")
                # 겸직 예외(사용자 정책): ① 풀에 예비가 한 명도 없거나 ② 새 직군이 기존 직군과
                # '비슷한 일'(도메인 토큰 공유)일 때만, **기존 직군을 유지한 채** 새 직군을 더한다
                # (교체 아님 — 전문화 기억 보존). 봇당 최대 2개(직군 스택 누적 재발 방지). 그 외에는
                # 1봇 1직업 원칙 — 예비를 그 직군으로 새로 뽑는 게 정도.
                spares_left = [s for s in flow.pool if _is_spare(flow, s)]
                similar = any(_job_tokens(j) & _job_tokens(role_name) for j in cur_jobs)
                if spares_left and not similar:
                    return (f"자기 직군 추가 거부: 당신은 이미 '{cur}' 직군입니다 — **1봇 1직업** 원칙이라 "
                               f"무관한 직군('{role_name}') 겸직은 예비가 없거나 비슷한 일일 때만 허용됩니다"
                               f"(전문화 보호). '{role_name}'이 필요하면 Task를 연 뒤 recruit(role='{role_name}')로 "
                               f"'예비'를 그 직군으로 채용하세요(예비 {len(spares_left)}명).")
                if len(cur_jobs) >= 2:
                    return (f"겸직 한도 초과: 당신은 이미 직군 2개('{cur}')를 보유하고 있습니다 — 봇당 "
                               f"겸직은 최대 2개입니다. '{role_name}'은 예비나 다른 동료에게 맡기세요.")
                new_label = f"{cur}{_JOB_SEP}{role_name}"
            flow.bot_info[me_id] = new_label
            if getattr(flow, "persist_role", None):
                try:
                    flow.persist_role(me_id, new_label)
                except Exception:
                    pass
            fn = getattr(g, "assign_job_role", None)
            if fn and getattr(flow, "guild_id", None):
                try:
                    await fn(flow.guild_id, me_id, new_label)
                except Exception:
                    pass
            what = "겸직 추가" if _JOB_SEP in new_label else "확정"
            return (f"자기 직군 {what}: 당신(id {me_id})의 직군 = '{new_label}' — 한 직원으로 "
                       f"참여합니다. 이어서 create_project → create_task로 팀을 꾸려 시작하세요.")
        return ("오류: 진행 중인 Task가 없습니다. 먼저 create_task로 Task를 여세요. (단 '예비' 담당자가 자기 "
                   "직군을 정하는 recruit(member=자신, role=…)는 Task 전에도 됩니다 — 자기 직군부터 정하세요.)")
    # 충원 루프 하드 차단: 최근 요청이 연속 2회+ 실패(시스템 일시불안정)면 채용을 막는다 — 지금 새로
    # 뽑아도 같은 불안정으로 똑같이 실패한다('백엔드 6명' 사태의 구조적 차단; 안내가 아니라 거부).
    # 기존 동료에게 다시 요청해 한 명이라도 응답이 오면 consec_fail이 리셋돼 다시 채용 가능.
    if getattr(flow, "consec_fail", 0) >= 2:
        return (f"채용 보류: 최근 요청이 연속 {flow.consec_fail}회 무응답/실패 — 시스템 일시 불안정입니다. "
                   f"지금 새로 뽑아도 같이 실패하니 채용을 막습니다(무한 충원 루프 방지). 기존 동료에게 잠시 뒤 "
                   f"다시 요청해 한 명이라도 응답이 오면 그때 충원하거나, 계속 안 되면 사용자에게 보고하고 멈추세요.")
    cand = _resolve_members(spec, flow, flow.pool) if spec else []
    if not cand:
        # member 미지정(또는 못 찾음): 직군 채용이면 '예비' 인력에서 자동 선발(아직 프로젝트팀에 없는 예비)
        spares = [m for m in flow.pool if _is_spare(flow, m) and m not in flow.project_team]
        if role_name and spares:
            cand = [spares[0]]
        else:
            return (f"채용할 인력을 못 찾음 — member로 기존 동료(id/역할)를 지정하거나, role로 새 직군을 "
                       f"적어 '예비'를 채용하세요. 남은 예비: {len(spares)}명 / 현재 풀: {flow._names(flow.pool)}")
    mid = cand[0]
    # 예비(직군 미배정)는 'role=직군'을 줘야만 채용된다 — 말로만 배정 차단(직군은 구조적으로 부여).
    if _is_spare(flow, mid) and not role_name:
        return (f"채용 거부: {flow._info(mid) or mid}는 '예비'(직군 미배정)입니다 — role='직군명'을 함께 "
                   f"지정해 어떤 직군으로 채용할지 정하세요(예: recruit(member='{mid}', role='게임 기획자')). "
                   f"직군 없이는 합류·위임 불가(말로만 배정 금지 — 직군이 실제로 부여돼야 일을 맡길 수 있음).")
    # [같은 직군 채용도 자유] role 중복/실패상태로 채용을 거부하지 않는다 — 반복 채용('백엔드 6명')의 진짜
    # 원인은 '동료 무응답(서브프로세스 행)'이었고 그건 워커 턴 타임아웃으로 끊었다(8분 내 인프라실패 처리).
    # 따라서 필요하면 같은 직군을 더 뽑아도 된다. '무응답=인프라'라는 판단·안내는 요청 실패 메시지로만 한다.
    hired = ""
    if role_name:
        cur = flow._info(mid)
        if _is_spare(flow, mid) or not cur:
            flow.bot_info[mid] = role_name                    # 예비/무직 → 그 직군으로 (런타임만, 이 흐름)
            hired = f" — '{role_name}' 직군으로 채용(잠정 — 첫 실작업 시 영속)"
            # [일로 직업 획득 — 영속 이연] 예비를 직군으로 뽑아도 *지금은 영속하지 않는다*(jobs.json·Discord
            # 보류). 그 봇이 *첫 실작업(Write/Edit/run)*을 하는 순간에만 영속한다(권한 훅이 승격) — '직업=기억'을
            # 문자 그대로. 끝까지 일 안 하면 영속 안 돼 다음 흐름에 예비로 사라진다(0-기억 직군 양산의 근본 차단).
            # 충돌(같은 봇 이중채용)도 무해 — 둘 다 일 안 하면 둘 다 예비로 남는다.
            flow.tentative_roles[mid] = role_name
        elif not any(_norm_job(j) == _norm_job(role_name) for j in _jobs_of(cur)):
            # 이미 다른 직군 보유 — 원칙은 **1봇 1직업**(새 직군은 예비를 뽑는 게 정도). 겸직은 사용자
            # 정책의 예외 둘 중 하나일 때만: ① 풀에 예비가 한 명도 없음(어쩔 수 없음) ② 새 직군이
            # 기존 직군과 '비슷한 일'(도메인 토큰 공유). 허용 시 교체가 아니라 **추가**다 — 기존 전문화
            # 기억(주직군)을 유지한 채 부직군을 더하고, 봇당 최대 2개(직군 5~6개 스택 재발 방지).
            cur_jobs = _jobs_of(cur)
            spares_left = [s for s in flow.pool if _is_spare(flow, s)]
            similar = any(_job_tokens(j) & _job_tokens(role_name) for j in cur_jobs)
            if spares_left and not similar:
                return (f"채용 거부: {cur}(id {mid})는 이미 '{cur}' 직군입니다 — **1봇 1직업** 원칙이라 "
                           f"무관한 직군('{role_name}') 겸직은 예비가 없거나 비슷한 일일 때만 허용됩니다"
                           f"(전문화 기억 보호). '{role_name}'이 필요하면 recruit(role='{role_name}')로 "
                           f"'예비'를 그 직군으로 새로 뽑으세요(예비 {len(spares_left)}명).")
            if len(cur_jobs) >= 2:
                return (f"겸직 한도 초과: {flow._info(mid) or mid}(id {mid})는 이미 직군 2개('{cur}')를 "
                           f"보유 — 봇당 겸직은 최대 2개입니다. '{role_name}'은 예비나 다른 동료에게 맡기세요.")
            new_label = f"{cur}{_JOB_SEP}{role_name}"
            flow.bot_info[mid] = new_label
            hired = f" — '{role_name}' 겸직 추가(보유: {new_label})"
            if getattr(flow, "persist_role", None):
                try:
                    flow.persist_role(mid, new_label)
                except Exception:
                    pass
        # 이미 그 직군을 보유하고 있으면 라벨 변경 없이 그대로 합류.
        flow.current.status.group = _group_of(flow, flow.current.team)
        # 이름은 그대로 두고 '직군 라벨 전체'를 Discord 역할(권한)로 동기화 — best-effort. 단 *잠정 채용*
        # (예비→직군, 첫 실작업 전)은 보류한다 — 일로 획득하는 순간 SYS가 부여(영속 이연, 양산 차단).
        fn = getattr(g, "assign_job_role", None)
        if fn and getattr(flow, "guild_id", None) and mid not in flow.tentative_roles:
            try:
                await fn(flow.guild_id, mid, flow.bot_info.get(mid) or role_name)
            except Exception:
                pass
    if mid not in flow.project_team:
        flow.project_team.append(mid)
    if mid not in flow.current.team:
        flow.current.team.append(mid)
        flow.current.status.group = _group_of(flow, flow.current.team)
        await flow.refresh()
        await _add_members(g, flow.current.thread_id, [mid])   # 스레드에 합류(멤버십=팀)
    return (f"{flow._info(mid) or mid} 합류{hired}(사유: {args.get('reason', '')}). "
               f"현재 팀: {flow._names(flow.current.team)}")



async def request(flow, me_id, role, args):
    """[Rule 로직] request — guide_tools에서 이관(평문 반환, @tool이 _ok 래핑)."""
    from .._util import _looks_transient
    from .._util import _dbg, _ok, _react, _speech_clip
    from ..protocol import Kind
    from .task import _LOOP_ESCALATE_CROSS, _ckpt, _is_verifier
    import anyio
    import asyncio
    import re
    import time
    g = flow.guide
    to = int(args["to_id"])
    kind = Kind.WORK if str(args["kind"]).strip().lower().startswith("w") else Kind.INFO
    body = args["body"]
    tag = f"[REQ] {me_id}({flow._info(me_id)})→{to}({flow._info(to)}) {getattr(kind, 'value', kind)}"
    if flow.current is None:
        _dbg(f"{tag} ✗거부:Task없음")
        return _ok("오류: 진행 중인 Task가 없습니다. (리더가 create_task 먼저 여세요.)")
    # 직군 미배정(예비) 봇에게는 위임/질의 불가 — 말로 '너는 X야' 하고 일을 시키는 걸 구조적으로 막는다.
    # 먼저 recruit(role='직군')로 실제 직군을 부여해야 그 봇이 일할 수 있다(말로만 배정 차단).
    if _is_spare(flow, to):
        _dbg(f"{tag} ✗거부:직군 미배정(예비)")
        return _ok(f"요청 거부: {flow._info(to) or to}는 아직 직군 미배정('예비')입니다 — 말로 직군을 정하지 말고 "
                   f"recruit(member='{to}', role='직군명')으로 직군을 실제로 부여한 뒤 요청하세요(직군이 부여돼야 일을 맡길 수 있음).")
    # 위임자에게 되묻기(확인요청 반환): 직속 위임자에게 Info로 물으면 '재진입 불가' 에러 대신
    # 베턴을 위임자에게 질문과 함께 돌려준다 — 위임자가 답하고 그 일을 다시 맡긴다(협업 가능).
    if kind == Kind.INFO and to == flow.comm.direct_delegator(me_id) and to != me_id:
        flow.pending_clarify = {"from": me_id, "to": to, "q": body}
        flow.comm.history.append(("clarify", me_id, to, "pending", Kind.INFO))
        _dbg(f"{tag} ↩확인요청→위임자")
        return _ok(f"확인요청을 직속 위임자({flow._info(to)})에게 전달했습니다. 지금 이 턴을 즉시 "
                   f"마치고(추가 도구 호출·추측 진행 금지) 짧게 반환하세요 — 위임자가 답한 뒤 이 작업을 "
                   f"당신에게 다시 맡깁니다.")
    if to not in flow.current.team:
        if to in flow.project_team:
            # 프로젝트 팀원이면 이 Task에 자동 합류 — Task 내 관련 인원을 최소화할 이유는 없다.
            flow.current.team.append(to)
            flow.current.status.group = _group_of(flow, flow.current.team)
            await flow.refresh()
            _dbg(f"{tag} +Task자동합류(프로젝트팀원)")
        elif to in flow.pool:
            # [원인 교정 — 정보가 있는 거부] 리더가 회사 풀(전체 로스터)과 프로젝트 팀을 혼동해
            # 팀 밖 동료를 반복 호출하던 라이브 관측(7회 우회, SIGTERM 기억구멍이 증폭)의 뿌리:
            # 거부가 '안 된다'만 말하고 '그 직군이 팀에 누구인지'를 안 알려줘 같은 실수가 반복됐다.
            # 올바른 대안(팀 내 같은 직군)과 현재 팀 명단을 동봉해 첫 거부에서 바로 교정되게 한다.
            same = [m for m in flow.project_team
                    if m != me_id and not _is_spare(flow, m)
                    and ({_norm_job(j) for j in _jobs_of(flow._info(to) or "")}
                         & {_norm_job(j) for j in _jobs_of(flow._info(m) or "")})]
            alt = (" 같은 직군의 **팀 내 동료**: "
                   + ", ".join(f"{flow._info(m)}(id {m})" for m in same)
                   + " — 이들에게 요청하세요(재시도 금지)." if same else
                   " 팀에 그 직군이 없습니다 — 정말 필요하면 recruit(member=…, role=…)로 합류시킨 뒤 요청하세요.")
            _dbg(f"{tag} ✗거부:프로젝트밖")
            return _ok(f"요청 거부: {to}({flow._info(to)})는 이 프로젝트 팀이 아닙니다 — 회사 풀에는 "
                       f"있지만 이 프로젝트 구성원이 아닙니다(팀은 create_project 때 당신이 구성했습니다)."
                       f"{alt} 현재 프로젝트 팀: {flow._names(flow.project_team)}")
        else:
            return _ok(f"요청 거부: {to}는 채용 풀에 없습니다. 풀: {flow._names(flow.pool)}")
    if flow.wake is None:
        return _ok("오류: 시스템 준비 안 됨")
    # 직렬화: 베턴이 내 차례가 될 때까지 대기(거부 아님). 서로 다른 동료로의 병렬 요청은 순차 처리되며,
    # 첫 요청이 길게(중첩 협의·긴 구현) 걸려도 베턴은 결국 돌아오므로 위임이 끊기지 않는다. 데드라인은
    # 교착 안전장치 — 게임처럼 한 동료가 10분+ 작업하는 경우까지 넉넉히(1시간) 둬 '활성=동료' 반려가
    # 안 뜨게 한다(이전 600초는 긴 작업 중 병렬요청이 타임아웃돼 무서운 '거부' 노이즈를 냈다).
    # 직전 위임이 detach 상태로 완주 중이면(도구 호출은 포기됐지만 위임은 계속) 새 요청을 길게
    # 재우지 않고 즉시 안내한다 — 리더가 '보류' 헛돌이 대신 턴을 마치게(시스템이 완주 후 다시 깨움).
    if (any(not t.done() for t in getattr(flow, "inflight_tasks", ()))
            and flow.comm.alive != me_id and not flow.comm.done):
        return _ok("[대기] 직전 위임이 아직 진행 중입니다 — 추가 요청을 보내지 말고 이 턴을 간결히 "
                   "마치세요. 위임이 완료되면 시스템이 그 결과와 함께 당신을 다시 깨웁니다.")
    # [fork 동시성 가드] 의견 수집(표결·회의 1R)이 도는 동안엔 새 요청을 보내지 않는다 — fork 중엔
    # 베턴(alive)이 리더에 머물러, CLI가 같은 턴에 병렬 도구 호출(vote+request)을 내면 수집 가지와
    # 같은 동료를 이중으로 깨워 '같은 봇 두 턴'(세션 충돌)이 될 수 있다(직렬 vote 시절엔 alive 이동이
    # 자연 차단). 수집은 조인이 보장돼 짧으므로 대기 안내가 정답.
    if getattr(flow, "fork_active", 0) > 0:
        return _ok("[대기] 의견 수집(표결/회의)이 진행 중입니다 — 수집 결과를 받은 뒤 요청하세요.")
    deadline = time.monotonic() + 3600
    while flow.comm.alive != me_id and not flow.comm.done and time.monotonic() < deadline:
        await anyio.sleep(0.05)
    # 같은 턴에 '같은 동료에게 같은 요청'을 다발로 보낸 병렬 중복은 합친다(idempotent): 동료를 다시
    # 깨우지 않고 직전 응답을 그대로 재사용한다 → 반사적 중복 wake 차단(직렬화는 유지, 중복만 제거).
    dupkey = (flow.leader_segment, me_id, to, str(getattr(kind, "value", kind)), body)
    if dupkey in flow.req_results:
        if flow.log:
            flow.log("dup_parallel_merged", frm=me_id, to=to,
                     kind=str(getattr(kind, "value", kind)), seg=flow.leader_segment)
        _dbg(f"{tag} ⇉병렬중복 합침(동료 재호출 없이 같은 응답 재사용)")
        return _ok(f"[{to} 응답] {_speech_clip(flow.req_results[dupkey], 4000)}\n"
                   f"(같은 턴에 이미 보낸 동일 요청 — 동료를 다시 호출하지 않고 같은 응답을 재사용)")
    # 대기 한도까지 베턴이 안 돌아옴(동료가 비정상적으로 오래 작업) — 규약위반이 아니므로 무서운 '거부'
    # 안내를 사용자에게 띄우지 않고 조용히 '보류'로 소프트 반환(리더는 응답 받은 뒤 다시 시도).
    if flow.comm.alive != me_id and not flow.comm.done:
        _dbg(f"{tag} ⏸보류:대기 한도 초과(활성={flow.comm.alive})")
        return _ok(f"[보류] {flow._info(to) or to}가 아직 작업 중이라 지금은 보내지 않았습니다 — 그 동료의 "
                   f"응답을 받은 뒤 다시 요청하세요(오류 아님).")
    # 검증→점유는 await 없이 인접 실행 → 형제 요청과 경합하지 않음(원자적).
    try:
        flow.comm.check_request(me_id, to, kind)
    except BusyInOtherFlow as e:
        # [전역 점유] 규약 위반이 아니라 '그 동료가 지금 다른 흐름에서 일하는 중' — 무서운 '거부'
        # 대신 가용 대안(같은 직군 동료·채용)을 안내한다. 같은 동료 재시도(폴링)는 금지 문구로 차단.
        if flow.log:
            flow.log("req_busy_elsewhere", frm=me_id, to=to, holder=str(e.holder_scope or ""),
                     kind=str(getattr(kind, "value", kind)), seg=flow.leader_segment)
        _dbg(f"{tag} ⏸점유:타 흐름({e.holder_scope})")
        return _ok(f"[동료 점유] {flow._info(to) or to}는 지금 다른 흐름({e.holder_scope})에서 일하는 "
                   f"중입니다 — 같은 동료에게 재시도하며 기다리지 마세요(폴링 금지). "
                   f"{_free_alternatives(flow, me_id, to)}.")
    except CommError as e:
        if flow.log:   # 관측: 거부 시점의 베턴 상태(alive)·요청자를 영속 기록 → 원인 규명
            flow.log("req_rejected", frm=me_id, to=to, kind=str(getattr(kind, "value", kind)),
                     alive=flow.comm.alive, seg=flow.leader_segment, reason=str(e)[:70])
        _dbg(f"{tag} ✗거부:규약 ({e})")
        return _ok(f"요청 거부(규약): {e}")
    # Work 위임은 Goal 확정 뒤에만 — '목표 합의(set_goal) → 분배' 순서를 구조적으로 강제(선분배 금지).
    # Info(합의용)는 언제든 허용 → Goal을 정하는 논의 자체는 막지 않는다.
    goal = (flow.current.status.goal or "").strip()
    if kind == Kind.WORK and not goal:
        _dbg(f"{tag} ✗거부:Goal미확정")
        return _ok("Work 위임 거부: 이 Task의 Goal이 아직 확정되지 않았습니다. 먼저 동료와 request(Info)로 "
                   "목표를 합의하고 set_goal로 확정한 뒤 Work로 맡기세요(목표는 팀 합의의 산물 — 선분배 금지).")
    me_is_leader = (me_id == flow.leader)
    # [회로차단기 정지(2026-06-23 S1a 보강) — 경보는 '멈추게'도 해야 한다] loop_escalated가 켜졌는데도
    # 검증 cross-check Work가 또 들어오면(=사람이 아직 판정 안 함) 새 검증 워커를 띄우지 않는다. 종전
    # 회로차단기는 경보만 1회 띄우고 흐름은 그대로 루프→사람 부재 시 밤새 토큰을 태웠다(라이브 P-031: 경보
    # 후에도 검증 계속). 검증 위임을 *보류*하고 '① complete_task 마감 / ② 사용자 방향 제시'를 기다린다.
    # owner 수정 Work·complete_task는 안 막으므로 데드락이 아니다(리더가 언제든 마감으로 빠져나갈 수 있음).
    # 정상 e2e는 12회 전에 수렴하므로 이 블록에 닿지 않는다(병리적 루프에서만 작동).
    if (kind == Kind.WORK and flow.current and getattr(flow.current, "loop_escalated", False)
            and _is_verifier(flow._info(to) or "") and int(to) != (flow.current.owner or -1)):
        if flow.log:
            flow.log("loop_escalated_block", to=to, cross=flow.current.cross_checks)
        return _ok(
            f"[수렴 경보 — 검증 보류] 이 Task는 교차검증 {flow.current.cross_checks}회로 *사람 판정 대기 중*입니다 "
            f"(이미 사용자에게 에스컬레이트됨). 추가 검증을 띄우지 마세요 — 같은 문제를 반복 검증하는 루프입니다. "
            f"**① 검증이 충분하면 complete_task로 마감**하거나, **② 사용자가 방향을 제시할 때까지 기다리세요**. "
            f"(코드를 *고친* 뒤의 재검증·다른 작업은 사용자 개입으로 경보가 해제된 뒤 가능합니다.)")
    # [검증 종료상태 — 재검증 dedup(2026-06-23 전수감사, 사용자 '검증 집계'; 리뷰F1 교정)] *이미 이 산출물을
    # 독립검증한 그 검증자*(to in cross_checkers)에게, *코드가 변경되지 않았는데*(writes 불변) 또 검증을 맡기려
    # 하면 막는다 — 복구마다·결함 못 고친 채 "최종 검증"을 반복 요청하던 무한 루프(P-031 ~13회, 1346 run) 차단.
    # ※ 리뷰F1: 'to in cross_checkers'로 좁혀 — *아직 검증 안 한* 검증자에게 새 작업·새 검증을 시키는 건 통과
    # (검증자에게 새 Work 주는 것까지 막던 회귀 차단). 코드를 *고친 뒤*(writes 증가)·*첫* 검증도 통과.
    if (kind == Kind.WORK and flow.current and _is_verifier(flow._info(to) or "")
            and int(to) in getattr(flow.current, "cross_checkers", set())
            and getattr(flow.current, "last_verify_writes", -1) >= 0
            and sum(int(v) for v in (flow.writes_by_role or {}).values()) == flow.current.last_verify_writes
            and not getattr(flow, "reverify_checked", False)):
        if flow.log:
            flow.log("reverify_dedup", to=to, cross=flow.current.cross_checks)
        return _ok(
            f"재검증 보류(이 검증자는 이미 이 코드를 검증함 — 변경 0): {flow._info(to) or to}는 이미 이 산출물을 "
            f"독립 교차검증했고(팀 교차검증 {flow.current.cross_checks}회), 그 뒤 **코드가 한 줄도 안 바뀌었습니다**"
            f"(Write/Edit 0). 같은 검증자에게 같은 코드를 또 검증시키는 건 무한 '최종 검증' 루프입니다 — 둘 중 "
            f"하나로 진행하세요: ① 검증이 충분하면 **complete_task로 마감**(교차검증 게이트는 이미 통과). ② 검증에서 "
            f"나온 결함이 있으면 그 owner에게 Work로 ***고치게* 한 뒤**(코드가 바뀌면) 다시 검증하세요. (아직 검증 "
            f"안 한 *다른* 검증자에게 맡기거나, 검증자에게 *새 작업*을 주는 건 막지 않습니다.)")
    # [비-리더 교차도메인 Work 게이트 — 구조적 조율 단일화(2026-06-22, 사용자: '주어진 일과 무관한 일을
    #  다른 도메인에 시키는 이상한 협업'은 구조 문제다)] 비-리더는 *받은 일*을 한다 — 같은 도메인 동료에게
    # 분담(서브태스킹)하거나 검증자(QA)에게 검증을 맡기는 건 자유고, 막히거나 궁금한 건 request(Info)로
    # 어느 도메인 전문가에게든 *자문*(자유·권장)한다. 그러나 *다른 도메인의 새 Work*를 직접 여는 것은
    # 리더의 조율 역할이다(SINGLE FLOW·중앙 조율). 프롬프트로 '하지 마'가 아니라 구조로 막고 리더로 보낸다.
    # 검증·자문을 막는 게 아니라 '의미없는 교차도메인 Work 위임'만 막는다(사용자 설계 방향).
    if (kind == Kind.WORK and goal and not me_is_leader and to != flow.leader
            and not getattr(flow, "crossdomain_checked", False)):
        my_jobs = {_norm_job(j) for j in _jobs_of(flow._info(me_id) or "")} - {""}
        to_jobs = {_norm_job(j) for j in _jobs_of(flow._info(to) or "")} - {""}
        to_verifier = _is_verifier(flow._info(to) or "")
        cap_hit = _offdomain_capability_hit(flow, to, body)   # 같은 도메인이라도 내 도메인 밖 능력 요구면 hit
        # [회귀 교정(2026-06-23, 사용자: '대화가 난장판') — 'not same_domain' 블랭킷 차단 제거] 단지 도메인이
        # 다르다는 이유만으로 정상 교차도메인 협업(적임자에게 위임)까지 막아 리더 조율 큐로 보냈고, 그게
        # '[SYS 조율 — 막혀 배정]'·'[직군초과]' 난장판의 뿌리였다(라이브: crossdomain_blocked 140건 중 다수가
        # caps=[] = 능력 미스매치 없는 false-positive — 프론트엔드→데이터엔지니어 정상 협업까지 차단). 설계
        # 의도는 '의미없는 교차도메인 위임만 차단'인데 구현이 *모든* 교차도메인 Work를 막았다. 진짜 능력
        # 미스매치(cap_hit — 그 능력을 가진 다른 전문가가 있는데 못 가진 이에게 맡김)일 때만 리더로 돌린다.
        if cap_hit and not to_verifier:
            if flow.log:
                flow.log("work_crossdomain_blocked", frm=me_id, to=to, my=sorted(my_jobs),
                         to_jobs=sorted(to_jobs), caps=list(cap_hit.keys()), seg=flow.leader_segment)
            _dbg(f"{tag} ✗보류→리더조율큐:비리더 교차도메인")
            # [리더 조율 강제(2026-06-23, 사용자)] 막힌 교차도메인 Work를 그냥 거부하지 않고 '리더 조율
            # 큐'에 적재한다 — 워커가 이를 '핑계'로 보고하고 리더가 묵살·재발사하던 라이브 루프(P-030
            # backend2↔PM 핑퐁)를 끊기 위함. sys_core continue 루프가 이 큐를 리더 다음 턴에 'SYS 확인
            # 사실'로 주입해 리더가 *직접* 그 도메인 전문가에게 위임하게 한다. 같은 (요청자→대상)은 중복 적재 X.
            try:
                if not any(c.get("requester") == me_id and c.get("to") == to
                           for c in flow.pending_coordination):
                    flow.pending_coordination.append({
                        "requester": me_id, "req_role": flow._info(me_id) or str(me_id),
                        "to": to, "to_role": flow._info(to) or str(to),
                        "to_jobs": sorted(to_jobs), "body": (body or "")[:500]})
            except Exception:
                pass
            return _ok(
                f"위임 보류(교차도메인 — **리더 조율 큐로 이관됨**): 당신({flow._info(me_id)})은 다른 도메인의 "
                f"새 작업을 직접 맡길 수 없어, 이 요청을 **리더에게 조율 사안으로 올렸습니다** — 리더가 그 도메인 "
                f"전문가에게 직접 배정합니다. 지금 이 턴은 **당신 도메인의 일을 계속**하세요(막힌 그 부분은 리더가 "
                f"처리하니 기다리거나 다른 동료에게 떠넘기지 마세요). 질문·QA 검증은 그대로 자유입니다.")
    # [직군밖 사전 차단 — 리더 라우팅] 능력표로 *위임 전에* 능력 미스매치를 잡아 그 전문가에게 리다이렉트
    # (흡수의 씨앗 차단). 리더는 조율 권한이 있어 직접 적임자에게 보낸다(비-리더는 위 교차도메인 게이트가
    # 이미 리더로 돌렸다). 상세·근거는 _offdomain_capability_hit 참고. offdomain_checked는 테스트 우회 플래그.
    if kind == Kind.WORK and goal and me_is_leader and not getattr(flow, "offdomain_checked", False):
        _hit = _offdomain_capability_hit(flow, to, body)
        if _hit:
            if flow.log:
                flow.log("work_offdomain_blocked", to=to, caps=list(_hit.keys()), seg=flow.leader_segment)
            _who = "; ".join(f"{n} → {flow._names(ms)}" for n, ms in _hit.items())
            return _ok(
                f"위임 거부(직군밖 — 능력 미스매치): 이 작업은 **{', '.join(_hit)}** 능력이 필요한데 "
                f"{flow._info(to) or to}의 직군 밖입니다. 그 능력을 가진 전문가가 팀에 있습니다 — {_who}. "
                f"**그 전문가에게 위임**하세요(범용·비전문이 떠안으면 흡수 — placeholder 품질). 정말 {to}가 "
                f"맡아야 할 합당한 이유가 있으면 body에 '[직군초과: <사유>]'를 적어 다시 보내세요.")
    # Work Response → Accept/Redo (docs Communication.md §5). 이미 이 owner가 '완료 응답'까지 낸
    # 산출물을 같은 위임자가 또 Work로 보내면, 그건 '새 위임'이 아니라 직전 산출물의 Redo다.
    # → 새 프레임이 아니라 redo()로 처리한다(한계까지만, 초과 시 반복 위임 거부). 이로써 '되풀이
    #   위임'이 구조적으로 '직전 결함을 고치는 보완'으로만 성립한다(반사적 중복요청 차단·정당한 보완 허용).
    is_redo = kind == Kind.WORK and flow.comm.delivered_work(me_id, to)
    owner_body = body
    if is_redo:
        try:
            frame = flow.comm.redo(me_id, to, "pending", body=body)    # 베턴 점유 + Redo 카운트(한계 시 RedoLimitExceeded)
        except RedoLimitExceeded:
            _dbg(f"{tag} ✗재위임 한도초과")
            # [품질>토큰 — 리더 셀프 마무리 권유 제거] 종전 안내("직접 Write/Edit로 마무리")는
            # Redo 실패의 끝에서 중앙집권·비전문 마감을 권하는 셈이었다(탈중앙·전문화 역행).
            return _ok(f"재위임 거부(Redo 한도 초과): {to}({flow._info(to)})는 이미 이 산출물을 여러 번 "
                       f"보완했습니다. 같은 사람에게 같은 식으로 또 떠넘기지 마세요 — 품질 경로는: "
                       f"① 검증자(타 멤버)의 결함 보고로 **무엇이 왜 미달인지 정밀화**해 마지막 1회를 명확히 맡기거나 "
                       f"② 같은 직군의 **다른 전문가**(없으면 recruit)에게 결함 보고와 함께 맡기거나 "
                       f"③ goal이 이미 충족이면 complete_task, 끝내 미달이면 사용자에게 정직하게 보고하세요"
                       f"(리더가 비전문 직접 마무리로 덮지 말 것).")
        owner_body = (f"[보완 요청(Redo) — 직전 산출물이 목표에 못 미쳐 되돌아왔습니다] 고칠 구체적 결함: {body}\n"
                      f"[이 Task의 Goal] {goal}\n결함만 정확히 고치고 run으로 재검증해 그 증거와 함께 보고하세요.")
    else:
        frame = flow.comm.request(me_id, to, "pending", kind, body=body)   # 베턴 점유(alive→to) + 원문(정밀복구)
        if kind == Kind.WORK:
            # 위임의 '계약'은 리더가 매번 새로 쓰는 스펙이 아니라 팀 합의로 확정된 Goal이다(스펙 리파인
            # 루프=재요청의 뿌리를 끊는다). owner가 그 목표를 끝까지(구현+검증) 책임진다.
            owner_body = (f"[위임 — 이 목표를 끝까지 책임지는 owner는 당신입니다] 이 Task의 Goal: {goal}\n"
                          f"직접 구현하고 run으로 '목표가 충족됨'을 검증한 뒤(리더에게 되넘기지 말 것), "
                          f"그 실행 증거와 함께 간결히 보고하세요.\n"
                          f"큰 목표는 **수직 슬라이스 우선**: '끝까지 관통하는 최소 동작 버전'을 먼저 만들어 "
                          f"검증하고 그 위에 살을 붙이세요 — 마지막 통합 몰빵 금지(오차를 일찍 드러내는 것이 "
                          f"빠른 길입니다. RFC-005: 검증 신호는 연속적이어야 한다).\n"
                          f"보고는 다음 골격으로(보고 계약 — 받은 쪽이 산출물을 재탐색하지 않아도 되게): "
                          f"[결과] 한 줄 결론(완료/부분/실패) / [변경] 파일·핵심 변경 목록 / "
                          f"[검증] 방법→결과 / [리스크] 남은 것·주의점.\n"
                          f"단, 이 Goal에 **당신 직군의 전문성으로 만드는 게 아닌 범주**가 섞여 있으면 — "
                          f"코드로 흉내낼 수 있다고 당신 일인 게 아닙니다('할 수 있다'와 '그 분야 전문성으로 "
                          f"잘한다'는 다릅니다 — 비전문 자급은 placeholder일 뿐) — 어설프게 떠안지 말고 보고 "
                          f"**첫 줄**에 `[직군밖] 필요직군명` 을 적어 반려하세요. 리더가 그 직군을 채용하거나 "
                          f"실제 제작 자원으로 충족합니다(전문화 원칙: '구현 가능'이 아니라 '전문성 정합'으로 판단).\n"
                          f"[요청 맥락] {body}")
            iface = (getattr(flow.current, "interfaces", "") or "").strip()
            if iface:
                # [협업 — 인터페이스 직접 전달·합의(2026-06-22 사용자: '전문가끼리 서로 대화하는가')] 종전엔
                # interfaces가 Task에만 저장되고 owner에게 전달 안 돼(여기 누락) owner가 계약을 못 보고 추측
                # → 통합 불일치(P-028 API 미스매치). 이제 계약을 owner에게 주고, 맞물리는 부분은 그 도메인
                # owner에게 *직접 request(Info)*로 확인하게 한다(리더 중계·추측 금지).
                owner_body += (f"\n[도메인 간 인터페이스 계약 — 준수]\n{_speech_clip(iface, 1500)}"
                               f"\n[직접 합의 — 리더 중계 금지] 당신 작업이 다른 도메인과 맞물리면(데이터 포맷·"
                               f"API·이벤트 타이밍 등) 추측하거나 리더에게 되묻지 말고 **그 도메인 owner에게 "
                               f"직접 request(Info)**로 계약을 확인·합의하세요 — 전문가끼리 직접 소통합니다.")
            notes = getattr(flow.current, "collab_notes", "")
            if notes:
                # [스펙 증발 방지] 회의·표결의 합의는 리더 머릿속이 아니라 위임 계약에 실린다 —
                # 라이브 P-009: 9직군이 회의로 정한 스펙(상태머신·SLA·타이밍 계약)이 구현자에게
                # 전달되지 않아(스코프 단절·리더 요약 의존) 결과물 품질로 이어지지 못함.
                owner_body += f"\n[팀 협의 기록(회의·표결) — 구현·검증 시 이 합의를 준수]\n{_speech_clip(notes, 6000)}"   # 저장 한도(6000)와 일치 — 전달에서 합의가 또 잘리지 않게(품질>토큰)
            # [RFC-008 P0 — 검증 위임에 루브릭 자동 주입] owner 인도 후 '다른 멤버'에게 가는 Work =
            # 검증 위임 → owner 산출물 도메인의 직무 기준을 루브릭으로 동봉. 라이브 P-010 1차에서 루브릭이
            # complete_task 거부 메시지에만 있어 0회 발동(검증이 카운트되면 게이트를 안 탐) — 검증자에게
            # 직접 주입해야 'owner 도메인 기준 채점'이 실제로 일어난다. '돌아가는가'가 아니라 '충분한가'.
            if (getattr(flow.current, "owner_delivered", False) and flow.current.owner
                    and to != flow.current.owner and callable(getattr(flow, "craft_of", None))):
                owner_job = (flow._info(flow.current.owner) or "").strip()
                rub = [flow.craft_of(j) for j in owner_job.split("·") if j.strip()]
                rub = [r for r in rub if r]
                if rub:
                    # [발견2 완화] owner 인도 후 타 멤버 Work가 '검증'인지 '후속 구현'인지 구조로 완벽히
                    # 구분 불가(의도의 문제) — 메시지가 양쪽을 다 커버해 오발동을 무해화한다: 검증 위임이면
                    # 채점, 후속 구현이면 같은 기준을 '참고'(통합 시 품질 인식). 어느 쪽이든 owner 도메인
                    # 기준이 주입되는 건 손해가 아니다('충분한가'의 눈을 공유).
                    owner_body += (f"\n[산출물 품질 기준 — '{owner_job}' 도메인. 이 요청이 **검증**이면 산출물을 "
                                   f"'사용자처럼 실제로 사용·플레이'하며 아래 각 항목을 충족/미달로 채점하고 미달은 "
                                   f"구체적 결함으로 보고하세요(돌아가는가 아니라 '충분한가'). 이 요청이 **후속 "
                                   f"구현/통합**이면 아래 기준을 참고해 같은 품질 수준을 맞추세요:\n"
                                   + _speech_clip("\n---\n".join(rub), 2500))
    thread_id = flow.current.thread_id
    # Owner = 그 일을 Work로 받은 동료(수신=소유). 선배정이 아니라 요청으로 owner가 떠오른다 —
    # 이 Task에 아직 owner가 없을 때 첫 Work-request 수신자가 책임자가 된다(중앙집권 방지).
    if kind == Kind.WORK and not flow.current.owner:
        flow.current.owner = to
        flow.current.status.owner = flow._info(to) or f"<@{to}>"
        await flow.refresh(flow.current)
        _ckpt(flow)                       # 크래시-세이프: owner 확정 영속(복구 때 같은 담당이 잇게)
    req = await g.send_request(thread_id, me_id, to, kind, body)
    frame.request_id = str(req)                              # 실제 메시지 id로 기록 갱신
    if kind == Kind.WORK and flow.current:
        flow.current.work_delegated_to.add(to)   # 누가 위임했든(리더든 peer든) 'Work를 실제로 받은' 멤버 기록
        if to == flow.current.owner:
            # [정밀 복구] owner에게 보낸 Work 원문 보관(레벨1 fallback).
            flow.current.last_work_body = body
        if me_id == flow.leader:
            flow.current.work_delegated += 1   # 리더의 구현 위임 카운트 — 0이면 '자문만 받고 독식'(권한 훅이 차단)
        # [정밀 복구 — 체인 깊이 영속] 모든 Work 위임마다 체크포인트 → 스냅샷의 active_chain이 *현재 깊이*를
        # 반영. 끊김 시 가장 깊은 활성 워커(체인 끝)를 그 원문으로 재개(리더로 안 튐). 깊은 전문가 협업 보존.
        _ckpt(flow)
    _dbg(f"{tag} ✓전송 req={req}{' (Redo)' if is_redo else ''}")
    if flow.log:   # 관측: 모든 요청을 '보낸 순서'대로 영속 기록(중첩 PostToolUse 타이밍에 안 묻힘)
        flow.log("req_sent", frm=me_id, to=to, kind=str(getattr(kind, "value", kind)),
                 seg=flow.leader_segment, redo=is_redo, body=body[:60])
    # Task 정의 '실질 협의' 참여 기록 — 보낸 쪽·받은 쪽 모두(누가 물었든: 리더든 peer든). 빈 핑은 제외.
    # → set_goal 게이트가 'peer 협의도 합의로 인정'하고 '빈 핑은 불인정'하게 만든다(허브 완화·실질 강제).
    if kind == Kind.INFO and flow.current and _is_substantive(body):
        for x in (me_id, to):
            if x in flow.current.team and x != flow.leader:
                flow.current.participated.add(x)
        # [협업 — 전문가 간 직접 대화(2026-06-22 사용자 설계)] 양쪽 다 비-리더 팀원이면 owner↔owner 직접
        # Info(리더 경유 아님) — 쌍으로 기록. 인터페이스 계약을 '리더 중계·추측'이 아니라 *당사자끼리*
        # 합의했는지 마감 게이트(iface_dialogue)가 본다.
        if (me_id != flow.leader and to != flow.leader
                and me_id in flow.current.team and to in flow.current.team):
            flow.current.peer_info_pairs.add(frozenset((me_id, to)))
    # ── 위임 완주 보장(detach-safe) ─────────────────────────────────────────
    # 여기서부터의 '깨우기→응답 처리→프레임 close'는 별도 태스크(_deliver)로 돌고, 도구 호출
    # 자체는 shield로 감싼다. CLI가 (자체 한도 등으로) 이 도구 호출을 포기·취소해도 위임은
    # 끝까지 완주하고 규약(베턴·게이트·기록)이 일관되게 닫힌다 — 라이브 관측: 위임 포기가
    # '이중 활성'(리더+사슬 동시 작업)과 리더의 '비동기 작업 중' 오인을 만들던 결함의 차단.
    # 완주 결과는 flow.detached_results로 남아 SYS가 이어가기 리더에게 전달한다.
    detached = {"on": False}

    async def _deliver():
        runs_before = flow.current.run_count if flow.current else 0
        acts_before = flow.act_count   # 위임 도중 owner(단일흐름이라 깨운 동료만 활성)가 실제로 일했는지 측정
        mine_before = flow.act_by.get(me_id, 0) if getattr(flow, "act_by", None) is not None else 0
        _body_local = owner_body
        result = ""
        _nest_guard = 0
        while True:
            try:
                result = await flow.wake(to, _body_local, kind)     # 동료 깨워 응답(중첩 베턴)
                if _looks_transient(result):                        # 일시 오류면 한 번 더(답으로 취급 X)
                    result = await flow.wake(to, _body_local, kind)
            except Exception as e:
                result = f"(동료 처리 중 오류: {e})"
            # [중첩 위임 — 동기처럼 완주(논블로킹 핸드오프)] `to`가 자기 턴에서 다른 동료에게 핸드오프했으면
            # SYS가 그 하위 위임을 호출 *밖*에서 완주시키고 `to`를 그 결과로 이어간다 — 블록킹 도구호출 없이
            # 중첩이 직렬로 완주(75초 미닿음). 판정은 `to`의 출력 문자열('[위임됨' 등 — 봇 표현은 못 믿음)이
            # 아니라 **handoff_inflight[to]에 실제로 하위 위임이 등록됐다는 사실**로 한다(견고). _nest_guard는
            # 폭주 백스톱(같은 `to`가 끝없이 재위임만 하는 병적 경우) — 정상 사슬은 한참 못 미친다.
            _sub = (getattr(flow, "handoff_inflight", None) or {}).pop(to, None)
            _nest_guard += 1
            if _sub is None or _nest_guard > 50:
                if _sub is not None and flow.log:
                    flow.log("handoff_nest_guard", to=to, depth=_nest_guard)
                break
            try:
                _sr = await _sub
                _srt = _sr["content"][0]["text"] if isinstance(_sr, dict) else str(_sr)
            except Exception as e:
                _srt = f"(하위 위임 오류: {e})"
            _body_local = ("[당신이 맡긴 위임의 결과가 도착했습니다 — 이어서 통합·검증·완성하세요(추가 위임이 "
                           f"더 필요하면 한 번에 하나씩, 끝나면 보고로 응답):\n{_speech_clip(_srt, 4000)}")
        # 깨운 동료가 '나(위임자)에게 확인요청'을 남기고 턴을 마쳤으면, 그 질문을 응답으로 표면화 →
        # 내가 답을 정해 다시 맡긴다(되묻기가 에러가 아니라 협업으로 흐름). 이는 '완료'가 아니므로
        # delivered로 기록하지 않는다(되묻기 후 재위임은 Redo가 아니라 '첫 구현').
        was_clarify = False
        if (flow.pending_clarify and flow.pending_clarify.get("to") == me_id
                and flow.pending_clarify.get("from") == to):
            q = flow.pending_clarify["q"]
            flow.pending_clarify = None
            was_clarify = True
            result = (f"[확인요청 from {flow._info(to)}] {q}\n"
                      f"(→ 답을 정한 뒤, 이 작업을 {flow._info(to)}에게 request(Work)로 다시 맡기세요)")
        failed = _looks_transient(result)
        # [직군밖 반려 — 전문화의 구조 채널] 도메인 적합성은 시스템이 키워드로 판정하지 않는다 —
        # 그 분야 전문가(수신 owner)가 판정한다(자기정의 원칙). owner가 첫 줄에 '[직군밖] 필요직군'
        # 을 적으면: 실패도 미완도 아닌 '올바른 반려'로 분류하고, 소유를 해제하며, 리더에게 채용을
        # 구조적으로 지시한다 — 관계없는 직군이 일을 흡수해 어설픈 산출물을 내던 경로(라이브:
        # ML이 백엔드에 묶여 감)의 차단.
        refused_m = re.match(r"^\s*\[직군밖\]\s*([^\n]*)", result or "")
        refused = bool(kind == Kind.WORK and not was_clarify and not failed and refused_m)
        if refused and flow.current is not None and flow.current.owner == to:
            flow.current.owner = 0                 # 소유 해제 — 채용된 전문가가 새 owner가 되게
            flow.current.status.owner = ""
            flow.current.owner_incomplete = False
            _ckpt(flow)
        # owner가 '위임 도중 실제로 일했나' — 단일흐름이라 깨운 동료(+그 하위)만 활성이므로 wake 전후
        # act_count(run/Write/Edit) 증가 = owner 작업. 거짓이면 owner는 깨어났지만 착수 전/계획만 하고
        # 곧장 반환한 것(허위완료의 씨앗). 이걸로 '검증된 인도'와 '빈 응답'을 가른다.
        # '요청자 자신'의 활동(detach 뒤 리더가 모델 쪽에서 돌린 폴링 run 등)은 빼고 잰다 —
        # 위임 측정창의 인도 신호(owner_acted)가 이중 활성 잔재로 오염되지 않게(허위완료 차단 정확성).
        mine_delta = (flow.act_by.get(me_id, 0) - mine_before) if getattr(flow, "act_by", None) is not None else 0
        owner_acted = (flow.act_count - acts_before) > mine_delta
        # 진짜 행(무활동)으로 끊긴 인프라 타임아웃인데 owner가 그 전에 실제로 작업을 했다면, 한 작업은
        # 작업공간에 남아 있다 → '실패'로 끝내 유실시키지 말고 '이어가기'(미완)로 처리한다. (하트비트
        # 타임아웃이 일하는 워커는 안 자르므로 드문 경우지만, 안전망으로 작업 유실·허위완료를 막는다.)
        infra_timeout = (kind == Kind.WORK and not was_clarify
                         and "api error: timeout" in (result or "").lower())
        resumable_timeout = infra_timeout and owner_acted
        # 동료가 'turn 한도'로 미완 반환했나(Work) — 그러면 이 Task는 완료로 못 닫고(complete_task 거부),
        # 같은 owner에게 '이어서(continuation)' 재위임해 끝내야 한다(허위완료→다음 Task churn 차단). 미완은
        # delivered(accept)로 안 쳐서 respond 마커를 'incomplete'로 두면, 재위임이 Redo 한도에 안 걸린다
        # (이어가기는 '직전 결함 보완'이 아니라 '남은 작업 마저 하기'이므로 횟수 제한 없이 계속 가능).
        incomplete = (kind == Kind.WORK and not was_clarify and not failed and not refused
                      and "턴 한도 도달" in (result or "")) or resumable_timeout
        # 미완 게이트(owner_incomplete)는 '의미 있는 신호'로만 갱신한다: 미완 신호면 True, owner가
        # '실작업을 담은 정상 응답'으로 마무리하면 False(이어가기 완료 = 게이트 자동 해제). 크래시(failed)
        # ·실작업 없는 응답은 완료의 증거가 아니므로 직전 상태를 유지한다 — 타임아웃 미완이 후속 크래시/
        # 빈 응답으로 풀려 미완인 채 complete가 통과되는 구멍 차단.
        if kind == Kind.WORK and not was_clarify and flow.current:
            if incomplete:
                flow.current.owner_incomplete = True
            elif not failed and owner_acted:
                flow.current.owner_incomplete = False
        is_owner_work = (kind == Kind.WORK and not was_clarify and not failed and not incomplete
                         and not refused
                         and flow.current is not None and to == flow.current.owner)
        # owner가 Work를 받고도 실작업(run/Write) 0회로 곧장 반환 = 착수 전/계획만 = '인도 아님'.
        premature = is_owner_work and not owner_acted
        if premature and flow.current is not None:
            # 미착수도 '구조적 미완'이다 — 마커를 세워 complete를 막고, 리더 세그먼트가 여기서
            # 끝나도 SYS 자동 이어가기가 같은 owner를 다시 깨운다(판단이 아니라 기계적 행동).
            flow.current.owner_incomplete = True
        if is_owner_work and owner_acted and _is_substantive(result):
            flow.current.owner_delivered = True   # 이 owner가 실작업+응답을 냈다 → complete_task 허용 근거
            _ckpt(flow)              # [인도 사실 영속] 복구가 인도 핸드셰이크를 다시 요구하지 않게(마감 닫힘)
        try:
            await g.send_response(thread_id, to, req, result)
            await _react(g, thread_id, req, "⚠️" if failed else "✅")  # 상태=이모지(해소/실패)
            _dbg(f"{tag} {'⚠실패' if failed else ('…미완' if (incomplete or premature) else '✓응답')} len={len(result)}")
        finally:
            # 프레임 close = 베턴 복귀(누수 방지). 정상이면 alive==to 라 그대로 닫힌다. 미완·미착수(premature)는
            # 'accept'로 안 쳐서 delivered로 기록 안 함 → 같은 owner 재위임이 Redo 한도에 안 걸리고 '실제 첫 인도'로 성립.
            # 크래시(failed)도 'accept'가 아니다 — 인프라 실패가 '완료 인도'로 기록되면 직후 재요청이
            # Redo(보완)로 둔갑해 한도를 태우고 owner에게 '직전 산출물 결함' 프레임으로 잘못 전달된다.
            try:
                flow.comm.respond(to, "clarify" if was_clarify else
                                  ("refused" if refused else
                                   "incomplete" if (incomplete or premature) else
                                   "failed" if failed else "accept"), result)
            except CommError:
                # to의 중첩 하위요청이 응답 없이 끝나(크래시/이탈) 베턴이 to에 '굳은' 비정상 상황 →
                # me_id(요청자)가 다시 alive 될 때까지 위 프레임을 강제 close. 흐름 교착(굳음) 방지.
                _stuck = flow.comm.alive
                if flow.log:
                    flow.log("baton_recover", me=me_id, stuck_alive=_stuck, to=to)
                # [막힘 흡수 차단 — 막힌 사람 기록] 베턴이 막힌 하위 담당에서 위임자에게 되돌아온다. 위임자가
                # '내가 하지'로 그 사람 일을 흡수하지 못하게 막힌 사람을 기록 — 게이트가 '같은 사람 재요청'을
                # 유도(재채용 X). 막힌 사람이 다시 일하면 해제. (origin/리더 자신이 막힌 건 흡수 대상 아님.)
                # *새* victim일 때만 기준치·카운터 초기화 — 같은 사람이 반복해 막히면 카운터가 누적돼 N회 후
                # 게이트가 폴백(통과)하므로, 진짜 죽은 동료에 무한 재요청·무한 차단으로 빌드가 얼지 않는다.
                if (_stuck and _stuck != flow.comm.origin and _stuck != getattr(flow, "leader", None)
                        and getattr(flow, "_stall_victim", None) != _stuck):
                    flow._stall_victim = _stuck
                    flow._stall_victim_acts = (getattr(flow, "act_by", None) or {}).get(_stuck, 0)
                    flow._stall_blocks = 0
                guard = 0
                # origin 프레임(스택 마지막 1장)은 여기서 닫지 않는다 — 핸들러 레벨 복구가
                # 흐름 자체를 종료시키면 안 됨(origin 마감은 SYS의 _close_flow 책임). detach로
                # 프레임 순서가 어긋난 최악 타이밍에 흐름이 통째로 드레인되던 위험 차단.
                while (not flow.comm.done and flow.comm.alive != me_id
                       and len(flow.comm.open_requests) > 1 and guard < 30):
                    flow.comm.escalate("베턴 굳음 안전복구")
                    guard += 1
        if failed:
            if resumable_timeout:
                # owner가 작업을 진행하다 '무활동'으로 끊긴 경우 — 한 작업은 작업공간에 보존돼 있다.
                # 실패로 끝내지 말고 같은 owner에게 '이어서' 재위임(연속). owner_incomplete=True라 complete는
                # 막히고, 프레임 마커가 incomplete라 redo 한도와 무관하게 계속 이어갈 수 있다(유실·허위완료 동시 차단).
                if flow.log:
                    flow.log("owner_resumable_timeout", to=to, seg=getattr(flow, "leader_segment", 0))
                return _ok(f"[{flow._info(to)}] 작업을 진행하던 중 일시 무응답으로 끊겼습니다 — 한 작업은 "
                           f"작업공간에 보존돼 있습니다. **같은 담당자에게 request(Work)로 '이어서 남은 부분을 "
                           f"마저 끝내라'**고 다시 맡기세요(이어가기 — 횟수 제한 없음). 다른 사람으로 바꾸거나 "
                           f"새로 뽑지 마세요(같은 환경이라 같은 문제).")
            # 구조적 사실: 단일흐름은 한 번에 한 명만 일한다 → 요청자는 그 동료가 끝날 때까지 '블록'된다.
            # 따라서 여기서의 '실패'는 그 동료가 느리거나 불응한 게 아니라 그 동료의 LLM 서브프로세스가
            # '크래시'(SIGTERM/143·연결끊김·과부하)한 것 — 즉 인프라/환경 문제다. 새 사람으로 바꾸거나
            # 충원하면 '같은 환경'에서 똑같이 크래시한다(이게 '백엔드 6명' 루프의 뿌리). 그래서 실패엔
            # '재배정·채용'을 절대 권하지 않는다 — 같은 동료 1회 재시도(블립 회복용) 또는 사용자 보고만.
            flow.consec_fail = getattr(flow, "consec_fail", 0) + 1
            if flow.log:
                flow.log("req_failed", to=to, consec=flow.consec_fail, seg=flow.leader_segment)
            if flow.consec_fail >= 2:
                return _ok(f"[{to}] 또 실패 — **연속 {flow.consec_fail}회**. 이건 그 동료가 아니라 **환경(인프라) 일시 "
                           f"불안정**입니다(단일흐름이라 한 명만 도는데 그 서브프로세스가 크래시한 것). **새로 뽑거나 "
                           f"다른 사람으로 바꾸지 마세요 — 같은 환경이라 똑같이 실패합니다.** 진행 상황을 사용자에게 "
                           f"'환경 불안정으로 일시 중단'이라 보고하고 멈추세요(무한 재시도·충원 금지).")
            return _ok(f"[{to}] 응답 실패. 단일흐름에선 한 명만 일하므로 이건 그 동료 탓이 아니라 거의 항상 **인프라/일시 "
                       f"오류(서브프로세스 크래시)**입니다 — **다른 사람으로 바꾸거나 새로 뽑지 마세요(같은 환경이라 똑같이 "
                       f"실패).** 같은 동료에게 한 번만 다시 요청해보고(블립이면 회복), 또 실패하면 사용자에게 보고하고 멈추세요.")
        flow.consec_fail = 0   # 정상 응답 → 연속 실패 카운터 리셋(일시 블립 회복)
        if refused:
            need = (refused_m.group(1) or "").strip() or "해당 전문 직군"
            if flow.log:
                flow.log("work_refused_offdomain", to=to, need=need[:30], seg=flow.leader_segment)
            return _ok(f"[직군밖 반려] {flow._info(to) or to}가 이 일을 **자기 직군 밖**으로 판정했습니다 — "
                       f"필요 직군: {need}.\n**recruit(role='{need}')로 예비를 채용해 그 전문가에게 Work로 "
                       f"맡기세요** — 같은 동료나 관계없는 직군에 다시 떠넘기지 마세요(이 반려는 실패가 아니라 "
                       f"올바른 전문화 신호입니다. 소유는 해제됐고, 채용된 전문가가 새 owner가 됩니다).\n"
                       f"--- 반려 보고 원문 ---\n{_speech_clip(result, 1500)}")
        # owner가 깨어났지만 '실작업 없이'(run/Write/Edit 0회) 곧장 반환 = 아직 착수 전/계획만. 리더가 대신
        # 구현·완료하지 말 것(독점·허위완료의 정확한 진입점). 같은 owner에게 다시 맡겨 '검증된 산출물'을 받게
        # 안내한다. 이 응답은 캐시하지 않는다 → 같은 턴에 재위임해도 합쳐지지 않고 실제로 다시 깨운다.
        if premature:
            _dbg(f"{tag} ⚠owner 미착수(실작업 0)")
            if flow.log:
                flow.log("owner_no_work", to=to, seg=flow.leader_segment)
            return _ok(f"[{to} 응답] {_speech_clip(result, 1500)}\n\n[중요] {flow._info(to) or to}가 아직 산출물을 만들지 "
                       f"않았습니다(run/파일작성 0회 — 착수 전이거나 계획만). **당신이 대신 구현하거나 이 Task를 "
                       f"완료하지 마세요(독점·허위완료 금지).** 같은 owner에게 request(Work)로 다시 맡겨 'run으로 "
                       f"검증한 실제 산출물'을 받은 뒤 진행하세요. 정말 끝까지 무응답이면 recruit/재배정으로.")
        # 위임 응답엔 owner가 '직접 돌린 실행 증거(시스템 캡처)'를 붙여 돌려준다 — 위임자가 말이 아니라
        # 증거로 '검증 후 수락'할 수 있게(반사적 재요청 대신). owner가 이번에 run을 돌렸을 때만.
        receipt = ""
        if (kind == Kind.WORK and not was_clarify and flow.current
                and flow.current.run_count > runs_before and flow.current.evidence):
            receipt = f"\n[owner 실행 증거(시스템 캡처)] {_speech_clip(flow.current.evidence, 1000)}"
        # [발견1 교정 2026-06-13] 검증 대상 산출물이 '존재'하면(owner 위임 인도 OR 리더가 직접
        # 구현=leader_writes>0) 그 후 타 멤버 응답을 교차 검증 참여로 센다 — 리더 독식 Task(owner==0)도
        # 제3자 검증 대상('누가 만들었든 제3자 검증'은 보편 이치). 종전엔 owner_delivered만 봐서 리더
        # 독식이 검증 면제되던 구멍.
        product_ready = (flow.current.owner_delivered
                         or (not flow.current.owner and getattr(flow.current, "leader_writes", 0) > 0))
        if flow.current and product_ready and to != flow.current.owner:
            flow.current.cross_checks += 1
            # [독립 검증 = 다른 도메인 — 동질 모델] 같은 Claude·같은 직군 검증자는 에코(같은 관점→같은
            # 맹점). owner와 도메인이 다른 검증자만 '독립'으로 따로 센다(owner 미상이면 리더 기준).
            _own = flow.current.owner or flow.leader
            _od = {_norm_job(j) for j in _jobs_of(flow._info(_own) or "")} - {""}
            _vd = {_norm_job(j) for j in _jobs_of(flow._info(to) or "")} - {""}
            if _od and _vd and not (_od & _vd):
                flow.current.cross_check_offdomain += 1
                # [검증 종료상태 — 리뷰F2 교정] *독립(off-domain)* 검증 시점의 저작수만 기록한다(same-domain
                # 검증엔 갱신 안 함 — 종전엔 same-domain 검증이 마커를 올려 *변경된* 코드의 정당한 off-domain
                # 재검증을 막던 staleness). + 이 검증자를 기록(리뷰F1: 재검증 dedup이 '이미 검증한 그 검증자'에게
                # 만 적용되게 — 검증자에게 새 작업 시키는 것까지 막던 회귀 차단).
                flow.current.last_verify_writes = sum(int(v) for v in (flow.writes_by_role or {}).values())
                flow.current.cross_checkers.add(int(to))
            _ckpt(flow)              # [교차검증 사실 영속] 복구가 교차검증을 다시 요구하지 않게(마감 닫힘)
            # [회로차단기 — 수렴 경보(2026-06-23 협업재설계 S1)] 교차검증이 임계를 넘는데 안 닫히면 = 수렴이
            # 아니라 *루프*다. 봇은 '해결 불가'(플랫폼 한계 등)를 스스로 판정 못 해 무한 검증한다(라이브 P-031
            # 콜드스타트 41회). 봇에게 없는 메타인지를 시스템이 대신 — 사용자에게 *1회* 에스컬레이트해 판정을 넘긴다.
            if (flow.current.cross_checks >= _LOOP_ESCALATE_CROSS
                    and not getattr(flow.current, "loop_escalated", False)):
                flow.current.loop_escalated = True
                if flow.log:
                    flow.log("loop_circuit_breaker", task=flow.current.task_id, cross=flow.current.cross_checks)
                try:
                    await flow.guide.post(
                        flow.user_channel, 0,
                        f"[수렴 경보 — 사람 판정 필요] 이 Task가 교차검증을 {flow.current.cross_checks}회 했는데도 "
                        f"아직 안 닫힙니다. 봇들이 *같은 문제를 반복해 잡고* 있는데, 흔히 코드로 못 고치는 *한계*"
                        f"(플랫폼 제약 등)입니다 — 봇은 '해결 불가'를 스스로 판정 못 해 무한 검증합니다. 결정해주세요: "
                        f"**① 현 상태 수용·마감** / **② 다른 방향 제시**.")
                except Exception:
                    pass
                _ckpt(flow)
        flow.req_results[dupkey] = result   # 같은 턴 병렬 중복요청이 재사용할 응답 캐시(동료 재호출 방지)
        return _ok(f"[{to} 응답] {_speech_clip(result, 4000)}{receipt}")


    async def _deliver_tracked():
        payload = await _deliver()
        if detached["on"]:
            try:
                txt = payload["content"][0]["text"]
            except Exception:
                txt = str(payload)[:400]
            flow.detached_results.append(f"{flow._info(to) or to} → {_speech_clip(txt, 4000)}")
        return payload

    inner = asyncio.ensure_future(_deliver_tracked())
    flow.inflight_tasks.add(inner)
    inner.add_done_callback(flow.inflight_tasks.discard)
    if getattr(flow, "_handoff", False):
        # [논블로킹 핸드오프 — 단일흐름 안정성(2026-06-22 사용자 설계)] 동료의 *턴 전체*를 도구호출 안에서
        # 기다리지 않는다. 기다리면 75초 넘을 때 CLI가 도구호출을 포기→CancelledError→detach→백그라운드
        # 비동기 churn(P-029: 6위임 전부 detach·'처리 중 턴종료' 누수·빈 산출물). 대신 위임을 인플라이트로
        # 등록하고 *즉시* 반환 — 동료 작업은 SYS 이어가기 루프(_drain_inflight)와 _deliver 중첩 루프가 호출
        # *밖*에서 완주시켜 결과로 요청자를 잇는다. 베턴은 이미 to로 넘어가 요청자는 비활성 → 재위임 불가
        # (규약이 막음). 도구호출이 1초라 75초가 닿지 않고, 베턴 1개라 비동기 다중실행이 구조적으로 불가 = 단일흐름.
        detached["on"] = True
        flow.handoff_inflight[me_id] = inner
        return _ok("[위임됨 — SYS가 동료를 끝까지 완주시켜 *결과로 당신을 이어줍니다*(비동기 아님 · 한 번에 "
                   "한 위임). **'처리 중' 같은 말이나 재위임·추가 행동 없이 이 턴을 여기서 마치세요** — "
                   "결과가 도착하면 SYS가 자동으로 당신을 재개합니다.]")
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError:
        if not inner.done():
            detached["on"] = True       # 도구 호출만 죽고 위임은 계속 — 결과는 detached로 전달
            if flow.log:
                flow.log("delegation_detached", to=to, seg=flow.leader_segment)
        raise

