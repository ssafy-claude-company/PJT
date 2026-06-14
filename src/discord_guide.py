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
        # [견고화] reply_to는 Discord 메시지 id(정수 스노우플레이크)만 답글 대상이 된다. 비숫자
        # (예: 부팅 복구 합성 id 'recover-open-P-010')면 답글 불가 — int() 폭발로 첫 전송이 낭비되고
        # 에러 로그가 찍히던 것 방지(답글 없이 일반 전송으로 강등). 실 id가 아니어도 흐름은 안 깨진다.
        if reply_to is not None and not str(reply_to).isdigit():
            reply_to = None
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
        관찰성. 실패해도 작업엔 영향 없게 방어적(타이핑은 부수효과).
        주의: yield는 정확히 한 번 — 본문 예외를 except로 받아 다시 yield하면(과거 형태) 제너레이터
        이중-yield로 RuntimeError가 나며 원래 예외를 가린다. 타이핑 시작/종료만 각각 방어한다."""
        # discord.py의 ch.typing() 내장 루프는 네트워크 블립(연쇄 RESUME)에 조용히 죽는다 —
        # 그러면 긴 작업(작업공간 작업만 하는 위임) 동안 화면이 완전히 침묵해 사용자가 '흐름이
        # 멈췄다'고 오인한다(라이브 관측). 자체 견고 루프로 대체: 8초마다 트리거를 재발사하고,
        # 실패하면 채널을 다시 해석해 재시도한다 — 본문이 사는 한 표시가 죽지 않는다.
        async def _typer():
            client = self.organts.get(sender_id) if sender_id else None
            ch = None
            while True:
                try:
                    if ch is None:
                        ch = await self._resolve(client or self.system, int(channel_id))
                    await ch.typing()      # 단발 트리거(discord.py 2.x Typing.__await__) — 약 10초 표시
                except Exception:
                    ch = None              # 다음 주기에 재해석·재시도(블립 내성)
                await asyncio.sleep(8)

        task = None
        try:
            task = asyncio.get_running_loop().create_task(_typer())
        except Exception:
            task = None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()

    # --- Project = 채널 (담당 Organt가 'create_project' 기능으로 요청, System Bot이 실행) ---

    async def get_or_create_channel(self, guild_id: int, name: str) -> int:
        """이름으로 텍스트 채널을 찾고 없으면 만든다 — 시스템 채널(카나리아 등) 용도."""
        guild = self.system.get_guild(int(guild_id)) or await self.system.fetch_guild(int(guild_id))
        for ch in getattr(guild, "text_channels", []):
            if ch.name == name:
                return ch.id
        ch = await guild.create_text_channel(name)
        return ch.id

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

    async def hide_channel(self, guild_id: int, channel_id: int) -> None:
        """채널을 사람 눈에서 숨긴다(@everyone 보기 차단, system 봇만 허용) — 시스템 내부 채널
        (sys-canary 등)이 사이드바·알림에 나타나지 않게. best-effort."""
        import discord
        try:
            guild = self.system.get_guild(int(guild_id)) or await self.system.fetch_guild(int(guild_id))
            ch = await self._resolve(self.system, int(channel_id))
            me = guild.get_member(self.system.user.id) or await guild.fetch_member(self.system.user.id)
            await ch.set_permissions(me, view_channel=True, send_messages=True, read_message_history=True)
            await ch.set_permissions(guild.default_role, view_channel=False)
        except Exception:
            pass

    async def edit_message(self, channel_id, message_id, content: str) -> None:
        """시스템 봇 자신의 메시지를 수정(best-effort) — 앵커 편집형 카나리아 등."""
        ch = await self._resolve(self.system, int(channel_id))
        msg = await ch.fetch_message(int(message_id))
        await msg.edit(content=content[:1900])

    async def delete_message(self, channel_id, message_id) -> None:
        """메시지 삭제(best-effort) — 카나리아 등 시스템 잡음 정리용."""
        try:
            ch = await self._resolve(self.system, int(channel_id))
            msg = await ch.fetch_message(int(message_id))
            await msg.delete()
        except Exception:
            pass

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
        """여러 봇의 서버 닉네임을 한 번에 설정(best-effort). 성공 개수 반환. (닉네임=사람 이름 용도)"""
        ok = 0
        for uid, nick in (id_to_nick or {}).items():
            if uid in self.organts and await self.set_nick(guild_id, uid, nick):
                ok += 1
        return ok

    @staticmethod
    def _is_job_role(r) -> bool:
        """이 역할이 '직군 라벨 역할'인지 — 기본(@everyone)·관리(managed)·권한 역할('관리자' 등 위험 권한
        보유)은 직군이 아니다. assign_job_role이 만드는 직군 역할은 권한 없는 순수 라벨이라 이걸로 가른다."""
        if r.is_default() or getattr(r, "managed", False):
            return False
        p = r.permissions
        return not (p.administrator or p.manage_guild or p.manage_roles)

    async def assign_job_role(self, guild_id: int, user_id: int, job_name: str) -> bool:
        """봇의 직군 라벨('백엔드' 또는 겸직 '백엔드·QA', 최대 2개)을 **Discord 역할(권한)**로 동기화한다 —
        라벨의 구성 직군 역할을 모두 보장(없으면 생성·부여)하고, **라벨에 없는 '직군 역할' 잔재는 제거**한다.
        더하기만 하면 흐름이 거듭될수록 옛 직군이 누적돼(라이브 관측: 봇당 5~6개 스택) '직업 복원'이
        잔재를 집는다. 직군이 아닌 권한 역할('관리자' 등)·관리 역할은 건드리지 않는다.
        System 봇에 '역할 관리' 권한 + 대상보다 높은 역할이 필요(없으면 best-effort 실패)."""
        import discord
        jobs = [j.strip() for j in str(job_name or "").split("·") if j.strip()]   # 겸직 라벨 구분자
        if not jobs:
            return False
        try:
            guild = self.system.get_guild(int(guild_id)) or await self.system.fetch_guild(int(guild_id))
            member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
            want = []
            for job in jobs:
                role = discord.utils.get(guild.roles, name=job)
                if role is None:
                    role = await guild.create_role(name=job, mentionable=True, reason="Organt 직군")
                want.append(role)
            await member.add_roles(*want, reason="Organt 직군 배정")
            wanted = {r.id for r in want}
            stale = [r for r in member.roles if r.id not in wanted and self._is_job_role(r)]
            if stale:
                await member.remove_roles(*stale, reason="Organt 직군 동기화(라벨 밖 잔재 제거)")
            return True
        except Exception as e:
            print(f"[discord_guide] 직군 역할 부여 실패 user={user_id} job={job_name!r}: "
                  f"{type(e).__name__}: {e}", flush=True)
            return False

    async def assign_job_roles(self, guild_id: int, id_to_job: Dict[int, str]) -> int:
        """여러 봇에 직군 역할을 한 번에 부여(best-effort). 성공 개수 반환."""
        ok = 0
        for uid, job in (id_to_job or {}).items():
            if uid in self.organts and await self.assign_job_role(guild_id, uid, job):
                ok += 1
        return ok

    async def get_member_jobs(self, guild_id: int, user_ids) -> Dict[int, str]:
        """각 봇의 현재 Discord 역할 중 '커스텀 직군 역할'(@everyone·봇 통합 역할 제외)을 찾아 id→직군명.
        직군은 assign_job_role이 '직군 이름'으로 만든 역할이고, Discord 역할은 서버에 영속되므로 컨테이너
        재시작/리클레임(디스크 jobs.json까지 사라져도)을 넘어 '직업'을 복원하는 진실원이 된다(사용자 요청:
        '권한 자체로도 유추'). 권한 역할('관리자' 등)은 직군이 아니므로 제외한다. 겸직(최대 2개)은
        '주직군·부직군' 라벨로 합쳐 돌려주고, 그 이상 잔재 스택은 앞 2개만 쓴다(best-effort — 실패는 건너뜀)."""
        out: Dict[int, str] = {}
        try:
            guild = self.system.get_guild(int(guild_id)) or await self.system.fetch_guild(int(guild_id))
        except Exception:
            return out
        for uid in user_ids:
            try:
                m = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                jobs = [r.name for r in m.roles if self._is_job_role(r)]
                if jobs:
                    out[int(uid)] = "·".join(jobs[:2])   # 겸직 라벨 구분자(guide_tools._JOB_SEP와 동일)
            except Exception:
                continue
        return out

    async def get_member_nicks(self, guild_id: int, user_ids) -> Dict[int, str]:
        """각 봇의 현재 '서버 닉네임'(사람 이름)을 읽는다 — 닉네임은 역할처럼 서버에 영속되므로
        재시작/리클레임(디스크가 사라져도)을 넘어 '이름 정체성'을 복원하는 진실원이 된다. 닉네임이
        없는 봇은 결과에서 뺀다(best-effort — 실패는 건너뜀)."""
        out: Dict[int, str] = {}
        try:
            guild = self.system.get_guild(int(guild_id)) or await self.system.fetch_guild(int(guild_id))
        except Exception:
            return out
        for uid in user_ids:
            try:
                m = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                nick = getattr(m, "nick", None)
                if nick:
                    out[int(uid)] = nick
            except Exception:
                continue
        return out

    async def get_custom_role_names(self, guild_id: int) -> List[str]:
        """길드의 커스텀(비관리·비기본) 역할 이름 목록 — 직군 중복(변형) 생성 게이트의 비교 풀.
        직군 역할은 서버에 영속되므로, 현재 로스터에 없는 봇(토큰 유실·오프라인)이 보유한 직군도
        여기서 보인다(예: 'VFX 전문가'가 있는데 recruit가 'VFX 아티스트'를 새로 만드는 사고 방지)."""
        try:
            guild = self.system.get_guild(int(guild_id)) or await self.system.fetch_guild(int(guild_id))
            roles = list(guild.roles) or await guild.fetch_roles()
            return [r.name for r in roles if self._is_job_role(r)]   # 권한 역할('관리자' 등)은 직군 풀 제외
        except Exception:
            return []

    async def get_guild_bot_nicks(self, guild_id: int) -> Optional[Dict[int, str]]:
        """길드의 '모든 봇 멤버' id→닉네임(없으면 제외) — 이름 배정의 충돌 풀로 쓴다. 로스터에 연결된
        봇만 보면, 오프라인/로스터 밖 봇이 이미 쓰는 이름(예: 토큰 유실로 안 뜬 testtest4의 '박지호')을
        새 봇에 중복 배정하는 구멍이 생긴다 — 이름의 진실원은 '서버 전체'다.
        **실패는 None, '진짜 닉 없음'은 {}** — 둘을 구분해야 호출자가 '조회 실패'를 '전원 무명'으로
        오인해 전면 개명(이름 뒤섞기)하지 않는다. fetch_members는 members 인텐트 필요(system 봇만 켬)."""
        try:
            guild = self.system.get_guild(int(guild_id)) or await self.system.fetch_guild(int(guild_id))
            out: Dict[int, str] = {}
            async for m in guild.fetch_members(limit=None):
                if getattr(m, "bot", False) and getattr(m, "nick", None):
                    out[int(m.id)] = m.nick
            return out
        except Exception as e:
            print(f"[discord_guide] 길드 닉네임 풀 조회 실패(개명 스킵 대상): {type(e).__name__}: {e}",
                  flush=True)
            return None

    async def set_channel_topic(self, channel_id: int, topic: str) -> bool:
        """채널 '주제(topic)'를 설정한다 — 프로젝트 레지스트리 요지의 영속 기록용. 토픽은 서버에
        영속되므로 logs/(gitignore)가 컨테이너 리클레임으로 사라져도 채널 자체가 등록 정보를 들고
        있어, 부팅 시 레지스트리를 복원할 수 있다(best-effort)."""
        try:
            ch = await self._resolve(self.system, int(channel_id))
            await ch.edit(topic=(topic or "")[:1024])
            return True
        except Exception as e:
            print(f"[discord_guide] 토픽 설정 실패 ch={channel_id}: {type(e).__name__}: {e}", flush=True)
            return False

    async def get_channel_topics(self, guild_id: int) -> Dict[int, str]:
        """길드의 텍스트 채널 id→topic(빈 토픽 제외) — 부팅 시 프로젝트 레지스트리 복원용."""
        out: Dict[int, str] = {}
        try:
            guild = self.system.get_guild(int(guild_id))
            chans = list(guild.text_channels) if guild else []
            if not chans:
                guild = guild or await self.system.fetch_guild(int(guild_id))
                chans = [c for c in await guild.fetch_channels() if hasattr(c, "topic")]
            for c in chans:
                topic = getattr(c, "topic", None)
                if topic:
                    out[int(c.id)] = topic
        except Exception:
            pass
        return out

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

    async def read_thread(self, thread_id: int, limit: int = 50,
                          include_plain: bool = False) -> List[Union[Request, Response]]:
        """Thread의 구조화 메시지(Request/Response)를 **시간순(과거→최신)**으로 파싱해 반환.
        (discord history는 기본 최신→과거라 뒤집는다 — '마지막 요청' 판정이 순서에 기대므로 중요.)
        include_plain=True면 형식 없는 '평문' 메시지도 Request(to=None)로 감싸 포함한다 — 등록
        프로젝트 채널에선 평문이 곧 개입이므로, 부팅 복구가 평문 개입도 잡을 수 있게."""
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
            if parsed is None and include_plain and (m.content or "").strip():
                parsed = Request(to_id=None, kind=Kind.WORK, body=m.content.strip(),
                                 from_id=m.author.id, message_id=str(m.id))
            if parsed is not None:
                out.append(parsed)
        out.reverse()  # history는 최신→과거 → 시간순으로
        return out
