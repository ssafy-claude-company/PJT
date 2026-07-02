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
