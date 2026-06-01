"""Communication: Request/Response를 Discord 메시지로 인코딩/디코딩한다.

규약(Discord.md 포맷):
- To        = 멘션된 Organt (`<@id>`)
- RepliesTo = 이 메시지가 reply면, 그 대상(Request)에 대한 Response
- 식별      = Discord 메시지 ID
- kind/결과 = 본문 태그 `[REQ:<kind>]` / `[RESP:<result>]`

이 모듈은 포맷팅/파싱만 담당하는 순수 로직이다(네트워크 없음).
"""
from dataclasses import dataclass
from typing import List, Optional

REQ_PREFIX = "[REQ:"
RESP_PREFIX = "[RESP:"


@dataclass(frozen=True)
class Request:
    from_id: int
    to_id: Optional[int]
    kind: str                         # 예: "work"
    text: str
    request_id: Optional[str] = None  # 전송 후 Discord 메시지 ID


@dataclass(frozen=True)
class Response:
    from_id: int
    replies_to: str                   # 대상 Request의 메시지 ID
    result: str                       # "accept" | "redo" | "report" ...
    text: str
    response_id: Optional[str] = None


def format_request(to_id: int, kind: str, text: str) -> str:
    """Request를 보낼 메시지 본문으로 만든다(To는 멘션)."""
    return f"{REQ_PREFIX}{kind}] <@{to_id}> {text}".strip()


def format_response(result: str, text: str) -> str:
    """Response를 보낼 메시지 본문으로 만든다(reply로 전송)."""
    return f"{RESP_PREFIX}{result}] {text}".strip()


def _split_tag(content: str, prefix: str):
    """'[PFX:value] rest' → (value, rest). 형식이 아니면 (None, None)."""
    if not content.startswith(prefix) or "]" not in content:
        return None, None
    close = content.index("]")
    value = content[len(prefix):close]
    rest = content[close + 1:].strip()
    return value, rest


def parse_message(*, message_id, author_id, mention_ids: List[int],
                  reply_to_id, content: str):
    """Discord 메시지(primitive)를 Request/Response/None으로 해석한다."""
    c = (content or "").strip()

    # reply + [RESP:...] → Response
    result, rest = _split_tag(c, RESP_PREFIX)
    if result is not None and reply_to_id is not None:
        return Response(from_id=author_id, replies_to=str(reply_to_id),
                        result=result, text=rest, response_id=str(message_id))

    # [REQ:...] + 멘션 → Request
    kind, rest = _split_tag(c, REQ_PREFIX)
    if kind is not None:
        to_id = mention_ids[0] if mention_ids else None
        if to_id is not None:
            rest = rest.replace(f"<@{to_id}>", "").replace(f"<@!{to_id}>", "").strip()
        return Request(from_id=author_id, to_id=to_id, kind=kind, text=rest,
                       request_id=str(message_id))

    return None


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

    def request(self, from_id: int, to_id: int, request_id, kind: str = "work") -> Frame:
        if self.done:
            raise CommError("흐름이 이미 종료되었습니다.")
        if from_id != self.alive:
            raise CommError(f"활성 Organt만 요청할 수 있습니다(현재 활성={self.alive}).")
        if kind == "work" and to_id in self._participants():
            raise CommError(f"{to_id} 는 미완 Work 보유/흐름 참여 중 → Work Request 거부(겹침·순환 방지).")
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
