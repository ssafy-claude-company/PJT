"""Discord Guide — 소통 Rule의 Discord 구현체(전송기).

SYS가 이 Guide로 Discord와 입출력한다. Guide는 흐름을 모르고 전송/조회만 한다.
docs(Other/Guide/Discord.md):
- [Task-XXX] 상태블록은 **채널**에 게시·갱신(System 봇).
- 그 상태블록 메시지에서 **Thread**를 파생 → 대화(Request/Response)는 Thread 안에서.
- Request/Response는 **보낸 Organt 봇**으로 전송(From=봇). RepliesTo=reply, 식별=메시지 ID.
"""
import asyncio
from contextlib import asynccontextmanager
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

DISCORD_LIMIT = 2000   # Discord 한 메시지 최대 글자수


def _split_for_discord(content: str, limit: int = 1900) -> List[str]:
    """content를 Discord 한도 이하 조각들로 나눈다(줄 경계 우선, 긴 줄은 강제 분할)."""
    content = content if (content and content.strip()) else "​"
    if len(content) <= limit:
        return [content]
    parts: List[str] = []
    buf = ""
    for line in content.split("\n"):
        while len(line) > limit:                 # 한 줄 자체가 한도 초과 → 강제 분할
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(line[:limit])
            line = line[limit:]
        if buf and len(buf) + 1 + len(line) > limit:
            parts.append(buf)
            buf = line
        else:
            buf = line if not buf else f"{buf}\n{line}"
    if buf:
        parts.append(buf)
    return parts


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
        """메시지 전송(견고): 2000자 초과 시 분할, 실패 시 1회 재시도 + 로그.

        반환은 첫 조각의 메시지 ID(없으면 '0'). 길이/일시오류로 '조용히 사라지던' 문제 방지.
        """
        ch = await self._resolve(client, cid)
        first_id = None
        for i, part in enumerate(_split_for_discord(content)):
            sent = await self._send_one(ch, part, reply_to if i == 0 else None)
            if sent and first_id is None:
                first_id = sent
        return first_id or "0"

    async def _send_one(self, ch, content: str, reply_to) -> Optional[str]:
        # 일시 오류(특히 503/DNS resolution failure)는 점증 백오프로 여러 번 재시도 — 네트워크
        # 블립에 Response·보고가 '조용히 유실'되던 문제(완료됐는데 응답 안 보임) 방지.
        for attempt in range(1, 5):
            try:
                if reply_to is not None:
                    ref = await ch.fetch_message(int(reply_to))
                    msg = await ref.reply(content)
                else:
                    msg = await ch.send(content)
                return str(msg.id)
            except Exception as e:   # 실패를 '보이게' 한다(조용한 유실 방지)
                print(f"[discord_guide] 전송 실패(시도 {attempt}/4) {type(e).__name__}: {e} "
                      f"(len={len(content)}, reply_to={reply_to})", flush=True)
                reply_to = None      # 재시도는 일반 전송으로(reply 대상 문제 회피)
                if attempt < 4:
                    await asyncio.sleep(2 ** (attempt - 1))   # 1s, 2s, 4s 백오프
        return None

    @asynccontextmanager
    async def typing(self, channel_id, sender_id=None):
        """채널/스레드에 '…입력 중' 표시(가시성 — Organt가 응답·작업 작성 중임을 사람이 봄).
        보낸 봇 = sender의 Organt(없으면 system). 가이드 문서엔 없지만 Discord 기능을 활용한
        관찰성. 실패해도 작업엔 영향 없게 방어적(타이핑은 부수효과)."""
        try:
            client = self.organts.get(sender_id) if sender_id else None
            ch = await self._resolve(client or self.system, int(channel_id))
            async with ch.typing():
                yield
        except Exception:
            yield   # 타이핑 표시 실패는 무시(전송/작업과 무관)

    # --- Project = 채널 (담당 Organt가 'create_project' 기능으로 요청, System Bot이 실행) ---

    async def create_project_channel(self, guild_id: int, name: str) -> int:
        guild = self.system.get_guild(guild_id)
        if guild is None:
            guild = await self.system.fetch_guild(guild_id)
        # 새 프로젝트는 '항상 새 채널'을 연다 — 같은 이름 기존 채널에 편입되면 안 됨(기존 프로젝트
        # 기여는 그 채널 안에서의 개입 경로로만; 새 요청은 별도 공간). 단일 flow 내 중복 생성은
        # create_project가 project_channel 세팅 여부로 이미 막는다(no-op).
        ch = await guild.create_text_channel(name)
        return ch.id

    async def post(self, channel_id: int, sender_id: int, content: str,
                   reply_to=None) -> str:
        """임의 채널에 보낸봇으로 메시지 게시(답변/보고). sender 미등록 시 system."""
        client = self.organts.get(sender_id, self.system)
        return await self._send(client, int(channel_id), content, reply_to=reply_to)

    async def react(self, channel_id, message_id, emoji: str) -> None:
        """메시지에 이모지 반응을 단다(흐름 상태를 Discord-native하게 표시). best-effort."""
        try:
            ch = await self._resolve(self.system, int(channel_id))
            msg = await ch.fetch_message(int(message_id))
            await msg.add_reaction(emoji)
        except Exception:
            pass

    async def add_thread_members(self, thread_id, member_ids) -> None:
        """Task 스레드에 팀원을 add — 스레드 멤버십 = Task 팀(권한적 표현). best-effort."""
        import discord
        try:
            th = await self._resolve(self.system, int(thread_id))
            for mid in member_ids:
                try:
                    await th.add_user(discord.Object(id=int(mid)))
                except Exception:
                    pass
        except Exception:
            pass

    async def set_nick(self, guild_id: int, user_id: int, nick: str) -> bool:
        """길드에서 한 봇의 '서버 닉네임'을 그 봇의 직군으로 바꾼다 — 멤버 목록에서 '누가 어떤 직군인지'
        디스코드 네이티브하게 보이게(가시성). System 봇에 '닉네임 관리' 권한·상위 역할이 있어야 함.
        권한/계층 문제로 실패할 수 있어 best-effort(실패해도 흐름엔 무관)."""
        try:
            guild = self.system.get_guild(int(guild_id)) or await self.system.fetch_guild(int(guild_id))
            member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
            await member.edit(nick=(nick or "")[:32])   # 디스코드 닉 32자 한도
            return True
        except Exception as e:
            print(f"[discord_guide] 닉네임 설정 실패 user={user_id} nick={nick!r}: "
                  f"{type(e).__name__}: {e}", flush=True)
            return False

    async def set_nicks(self, guild_id: int, id_to_nick: Dict[int, str]) -> int:
        """여러 봇의 서버 닉네임을 한 번에 직군으로 설정(best-effort). 성공 개수 반환."""
        ok = 0
        for uid, nick in (id_to_nick or {}).items():
            if uid in self.organts and await self.set_nick(guild_id, uid, nick):
                ok += 1
        return ok

    # 새 봇 '원터치 초대'용 권한 — 워커가 스레드에 글 쓰고 반응/기록을 읽는 데 필요한 최소 집합
    # (View+Send+Embed+History+Reactions+Send-in-Threads). 봇 생성만 사람이 하고 초대는 링크 한 번.
    INVITE_PERMS = 1024 + 2048 + 16384 + 65536 + 64 + 274877906944  # = 274877992000

    @staticmethod
    def invite_url(app_id: int, perms: int = None) -> str:
        """봇을 서버에 넣는 OAuth2 초대 URL(클릭 한 번이면 합류). app_id는 봇의 user.id(=application id)."""
        p = DiscordGuide.INVITE_PERMS if perms is None else perms
        return (f"https://discord.com/oauth2/authorize?client_id={int(app_id)}"
                f"&scope=bot&permissions={p}")

    async def not_in_guild(self, guild_id: int, user_ids) -> List[int]:
        """user_ids 중 이 길드에 아직 없는(초대 안 된) 봇 id 목록 — '원터치 초대'를 띄울 대상."""
        import discord
        out: List[int] = []
        try:
            guild = self.system.get_guild(int(guild_id)) or await self.system.fetch_guild(int(guild_id))
        except Exception:
            return out
        for uid in user_ids:
            try:
                m = guild.get_member(int(uid))
                if m is None:
                    m = await guild.fetch_member(int(uid))
                if m is None:
                    out.append(int(uid))
            except discord.NotFound:
                out.append(int(uid))
            except Exception:
                pass   # 권한/일시 오류는 '미초대'로 단정하지 않음
        return out

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
