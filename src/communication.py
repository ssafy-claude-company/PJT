"""Communication Rule — 단일 활성 '베턴'과 요청 스택(순수 로직, 네트워크 없음).

docs(Rule/Communication.md):
- 흐름은 User(SMS)에서 시작한다. Organt는 스스로 흐름을 시작하지 않는다.
- 활성(alive) Organt은 항상 1명. Request 시 sender sleep / receiver wake.
- Work Request는 이미 미완 Work를 가진(흐름에 참여 중인) Organt에게 보낼 수 없다
  (겹침·순환 방지 = busy-guard).
- Response는 스택 역순(LIFO)으로 close. 모든 요청이 닫히면 흐름은 시작점(origin)으로
  복귀하고 종료된다. → 항상 1명만 활성(단일흐름) = 토큰 절약·사이드이펙트 감소.
- Work Response 불만족 시 Redo, 한계 초과 시 위로 상신(escalate).

메시지 인코딩/파싱(`[Request]`/`[Response]` 포맷)은 protocol.py가 담당한다.
"""
from dataclasses import dataclass
from typing import List

from .protocol import Kind


class CommError(Exception):
    """통신 규약 위반(베턴/순서 등)."""


class RedoLimitExceeded(CommError):
    """Redo 한계 초과 → 상신(escalate)해야 함."""


@dataclass
class Frame:
    """열린 요청 한 건(요청 스택의 프레임)."""
    from_id: int
    to_id: int
    request_id: str
    kind: str


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
        self.escalations: list = []
        self.escalated_to_origin = False

    @property
    def open_requests(self) -> List[Frame]:
        return list(self._stack)

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
        if self._is_work(kind) and to_id in self._participants():
            raise CommError(f"{to_id} 는 미완 Work 보유/흐름 참여 중 → Work Request 거부(겹침·순환 방지).")

    def request(self, from_id: int, to_id: int, request_id, kind: str = "work") -> Frame:
        self.check_request(from_id, to_id, kind)
        frame = Frame(from_id, to_id, str(request_id), kind)
        self._stack.append(frame)
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
        if not self._stack:             # 모든 요청 닫힘 → 시작점 복귀·종료
            self.done = True
            self.alive = self.origin
        return frame

    def _participants(self) -> set:
        """현재 흐름에 참여 중인(미완 Work 보유) Organt 집합."""
        s = {self.origin}
        for f in self._stack:
            s.add(f.from_id)
            s.add(f.to_id)
        return s

    def is_busy(self, organt_id) -> bool:
        """미완 Work 보유(또는 흐름 참여 중)인가 → Work Request 금지 대상."""
        return organt_id in self._participants()

    @staticmethod
    def _is_work(kind) -> bool:
        """Kind가 Work인지 (protocol.Kind 또는 'work'/'Work' 문자열 모두 인식)."""
        if isinstance(kind, Kind):
            return kind == Kind.WORK
        return str(kind).strip().lower() == "work"

    def redo(self, from_id: int, to_id: int, request_id) -> Frame:
        """직전 응답이 불만족 → 같은 대상에 재요청(Redo). 한계 초과 시 RedoLimitExceeded."""
        if from_id != self.alive:
            raise CommError(f"활성 Organt만 redo할 수 있습니다(현재 활성={self.alive}).")
        key = (from_id, to_id)
        count = self._redo_counts.get(key, 0) + 1
        if count > self.redo_limit:
            raise RedoLimitExceeded(f"redo 한계({self.redo_limit}) 초과 → 상신 필요.")
        self._redo_counts[key] = count
        self.history.append(("redo", from_id, to_id, str(request_id), count))
        return self.request(from_id, to_id, request_id, kind="work")

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
        return frame
