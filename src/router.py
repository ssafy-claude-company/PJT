"""메시지 수집·라우팅 판정.

System 봇이 대상 채널의 사람 메시지를 모두 수집(audit 대상)하고,
Organt가 멘션된 경우에만 Organt에 라우팅한다.

이 모듈은 '판정'만 담당하는 순수 로직이다.
- 실제 audit 기록(JSONL)은 기능6에서 붙인다.
- Organt(LLM) 처리·응답은 기능4·5에서 붙인다.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RoutingDecision:
    """한 메시지에 대한 처리 판정."""

    collect: bool          # audit 수집 대상인가 (대상 채널의 사람 메시지)
    route_to_organt: bool  # Organt에 전달할 것인가 (Organt가 멘션됨)


class Router:
    """대상 채널 + Organt 멘션 여부로 메시지 처리를 판정한다."""

    def __init__(self, channel_id: int, organt_user_id: Optional[int]):
        self.channel_id = channel_id
        # Organt 봇이 아직 연결 전이면 None일 수 있다(멘션 판정 불가 → 라우팅 안 함).
        self.organt_user_id = organt_user_id

    def decide(self, message) -> RoutingDecision:
        """discord 메시지(또는 같은 형태의 객체)를 받아 판정한다.

        기대 속성:
          - message.channel.id : 메시지가 올라온 채널 ID
          - message.author.bot : 작성자가 봇인지
          - message.mentions   : 멘션된 사용자 목록(각 원소는 .id 보유)
        """
        # 대상 채널이 아니면 아무것도 하지 않는다.
        if message.channel.id != self.channel_id:
            return RoutingDecision(collect=False, route_to_organt=False)
        # 봇(System·Organt 포함)이 보낸 메시지는 수집/라우팅하지 않는다 — 사람 메시지만.
        if message.author.bot:
            return RoutingDecision(collect=False, route_to_organt=False)
        # 대상 채널의 사람 메시지는 모두 수집한다.
        # 그중 Organt가 멘션된 경우에만 Organt에 라우팅한다.
        mentioned = any(user.id == self.organt_user_id for user in message.mentions)
        return RoutingDecision(collect=True, route_to_organt=mentioned)
