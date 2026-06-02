"""Discord Guide — 소통 Rule의 Discord 구현체(전송기).

SYS가 이 Guide로 Discord와 입출력한다. Guide는 흐름을 모르고 전송/조회만 한다.
docs(Other/Guide/Discord.md):
- [Task-XXX] 상태블록은 **채널**에 게시·갱신(System 봇).
- 그 상태블록 메시지에서 **Thread**를 파생 → 대화(Request/Response)는 Thread 안에서.
- Request/Response는 **보낸 Organt 봇**으로 전송(From=봇). RepliesTo=reply, 식별=메시지 ID.
"""
from typing import Dict, List, Optional, Union

from .protocol import (
    Kind,
    Request,
    Response,
    TaskStatus,
    format_request,
    format_response,
    format_task_status,
    parse,
)


class DiscordGuide:
    """Discord 전송기. system 봇 + Organt 봇들을 들고 채널/스레드/상태블록을 다룬다."""

    def __init__(self, system_client, organt_clients: Optional[Dict[int, object]] = None):
        self.system = system_client
        self.organts: Dict[int, object] = dict(organt_clients or {})  # user_id -> client

    def register_organt(self, user_id: int, client) -> None:
        self.organts[user_id] = client

    async def _resolve(self, client, cid: int):
        ch = client.get_channel(cid)
        if ch is None:
            ch = await client.fetch_channel(cid)
        return ch

    async def _send(self, client, cid: int, content: str, reply_to=None) -> str:
        ch = await self._resolve(client, cid)
        if reply_to is not None:
            ref = await ch.fetch_message(int(reply_to))
            msg = await ref.reply(content)
        else:
            msg = await ch.send(content)
        return str(msg.id)

    # --- Task = 채널 상태블록 + 스레드 ---

    async def open_task(self, channel_id: int, status: TaskStatus):
        """채널에 [Task-XXX] 상태블록을 올리고, 그 블록에서 대화용 Thread를 만든다."""
        ch = await self._resolve(self.system, channel_id)
        block = await ch.send(format_task_status(status))
        thread = await block.create_thread(name=f"Task-{status.task_id}")
        return str(block.id), str(thread.id)

    async def update_status(self, channel_id: int, status_msg_id: str, status: TaskStatus) -> str:
        """채널의 상태블록 메시지를 현재 상태로 갱신(edit)한다."""
        ch = await self._resolve(self.system, channel_id)
        msg = await ch.fetch_message(int(status_msg_id))
        await msg.edit(content=format_task_status(status))
        return status_msg_id

    # --- Thread 내 구조화 소통 (보낸 봇 = Organt) ---

    async def send_request(self, thread_id: int, sender_id: int, to_id: int,
                           kind: Union[Kind, str], body: str) -> str:
        client = self.organts[sender_id]
        return await self._send(client, int(thread_id), format_request(to_id, kind, body))

    async def send_response(self, thread_id: int, sender_id: int,
                            request_msg_id: str, body: str) -> str:
        client = self.organts[sender_id]
        return await self._send(client, int(thread_id), format_response(body),
                                reply_to=request_msg_id)

    async def read_thread(self, thread_id: int, limit: int = 50) -> List[Union[Request, Response]]:
        """Thread의 구조화 메시지(Request/Response)를 시간순으로 파싱해 반환."""
        ch = await self._resolve(self.system, int(thread_id))
        out: List[Union[Request, Response]] = []
        async for m in ch.history(limit=limit):
            ref = m.reference.message_id if getattr(m, "reference", None) else None
            parsed = parse(
                message_id=m.id,
                author_id=m.author.id,
                mention_ids=[u.id for u in getattr(m, "mentions", [])],
                reply_to_id=ref,
                content=m.content,
            )
            if parsed is not None:
                out.append(parsed)
        out.reverse()  # history는 최신→과거 → 시간순으로
        return out
