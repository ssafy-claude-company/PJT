"""통신 오케스트레이터: 베턴(CommunicationManager)을 실제 Discord 전송과 묶는다.

각 Organt는 자기 봇으로 메시지를 보낸다:
- Request  = 대상 Organt를 멘션한 `[REQ:..]` 메시지
- Response = 그 Request에 대한 reply `[RESP:..]` 메시지
오케스트레이터는 베턴 상태기계로 "살아있는 Organt"를 단일하게 관리한다.

senders는 DiscordIO 형태(send/reply)면 무엇이든 된다(테스트는 가짜 sender 사용).
"""
from typing import Dict

from .communication import (
    CommunicationManager,
    format_request,
    format_response,
)


class CommGateway:
    """System 오케스트레이터 + N Organt 전송기를 베턴으로 구동한다."""

    def __init__(self, senders: Dict[str, object], ids: Dict[str, int], origin: str):
        # senders: {organt이름: DiscordIO유사(send/reply)}, ids: {이름: discord 사용자 ID}
        self.senders = senders
        self.ids = ids
        self.origin = origin
        self.manager = CommunicationManager(ids[origin])
        self._id_to_name = {uid: name for name, uid in ids.items()}

    def alive_name(self):
        """현재 살아있는 Organt 이름."""
        return self._id_to_name.get(self.manager.alive)

    @property
    def done(self) -> bool:
        return self.manager.done

    async def request(self, sender: str, target: str, text: str, kind: str = "work") -> str:
        """sender가 target에게 Request 메시지(멘션)를 보내고 베턴을 넘긴다."""
        content = format_request(self.ids[target], kind, text)
        msg_id = await self.senders[sender].send(content)
        self.manager.request(self.ids[sender], self.ids[target], msg_id, kind)
        return str(msg_id)

    async def respond(self, responder: str, request_msg_id: str, text: str,
                      result: str = "accept") -> str:
        """responder가 해당 Request에 reply로 Response를 보내고 베턴을 되돌린다."""
        content = format_response(result, text)
        msg_id = await self.senders[responder].reply(int(request_msg_id), content)
        self.manager.respond(self.ids[responder], result, text)
        return str(msg_id)
