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
from dataclasses import dataclass
from typing import List

from .protocol import Kind


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
