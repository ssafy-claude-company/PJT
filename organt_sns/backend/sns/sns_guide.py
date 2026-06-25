"""SnsGuide — Organt Rule의 새 전송 계층(디스코드 대체).

DiscordGuide와 *같은 계약*(post/send_request/send_response/read_thread/open_task/update_status/
create_project_channel/edit_message/assign_job_role/typing …)을 SNS DB(GuideMessage/Project/Agent)로
구현한다. main.py가 `DiscordGuide(...)` 주입하던 자리에 이걸 넣으면 Rule이 SNS 위에서 그대로 돈다.

핵심: read_thread가 *구조화된 Request/Response*를 돌려줘야 Rule이 자기 대화를 되읽어 상태를 복원한다 —
GuideMessage에 sender/to/kind/body/reply_to를 그대로 보존하고 여기서 protocol 객체로 재구성한다.

Rule은 async, Django ORM은 sync → 모든 DB 접근을 sync_to_async로 감싼다. 메시지 기록 시 채널을
'더티'로 표시(_touched)해 두면 웹 계층이 폴링/웹소켓으로 라이브 푸시할 수 있다.
"""
import os
import sys
import time
import itertools
from contextlib import asynccontextmanager

from asgiref.sync import sync_to_async
from django.utils import timezone

# Rule의 프로토콜 객체(Request/Response/Kind) — 재구성용. src를 경로에 올린다.
_PJT = os.environ.get("ORGANT_PJT", "/home/user/PJT")
if _PJT not in sys.path:
    sys.path.insert(0, _PJT)
try:
    from src.protocol import Request, Response, Kind  # noqa
except Exception:  # 러너 밖(마이그레이션 등)에서 import만 될 때 안전
    Request = Response = Kind = None

from .models import GuideMessage, Project, Agent


def _now():
    return time.time()


class SnsGuide:
    """Rule ↔ SNS DB 전송기. 한 러너 프로세스 안에서 Sys/Flow에 주입된다(스레드→채널 맵은 인메모리)."""

    def __init__(self):
        self._ids = itertools.count(int(_now() * 1000))   # 채널/스레드용 단조 int id
        self._thread_channel = {}                          # thread_id → channel_id
        self._touched = set()                              # 라이브 푸시 대상 채널(웹이 폴함)

    def _new_id(self):
        return next(self._ids)

    # ── 기록 헬퍼(sync) ────────────────────────────────────────────
    def _write(self, **kw):
        kw.setdefault("ts", _now())
        m = GuideMessage.objects.create(**kw)
        self._touched.add(kw.get("channel_id"))
        return m.msg_id

    # ── 채널/스레드 ────────────────────────────────────────────────
    async def create_project_channel(self, guild_id, name):
        cid = self._new_id()
        await sync_to_async(self._register_channel)(cid, str(name))
        return cid

    def _register_channel(self, cid, name):
        # 라이브 채널을 Project로 영속 — channel_id(cid)를 payload 없이 pid로 역참조 가능하게 보관.
        Project.objects.get_or_create(
            pid=f"S-{cid % 1000000}", defaults={"name": name[:200], "status": "live"})

    async def open_task(self, channel_id, status):
        tid = self._new_id()
        self._thread_channel[tid] = int(channel_id)
        block = await sync_to_async(self._write)(
            channel_id=int(channel_id), thread_id=int(channel_id), sender_id=0,
            msg_type="status", body=f"[Task-{getattr(status,'task_id','?')}]",
            payload={"task_id": getattr(status, "task_id", None)})
        return str(block), tid

    async def update_status(self, channel_id, status_msg_id, status):
        def _upd():
            GuideMessage.objects.filter(msg_id=int(status_msg_id)).update(
                body=f"[Task-{getattr(status,'task_id','?')}] {getattr(status,'state','')}",
                edited=True, payload={"task_id": getattr(status, "task_id", None),
                                      "state": getattr(status, "state", None)})
            self._touched.add(int(channel_id))
        await sync_to_async(_upd)()
        return status_msg_id

    # ── 메시지 ─────────────────────────────────────────────────────
    async def post(self, channel_id, sender_id, content, reply_to=None):
        # 스레드→채널 해석(send_request/response와 동일) — _say(회의·표결·병렬)가 합성 thread_id로
        # 호출돼도 사용자가 보는 실제 채널에 뜨게 한다(유령 채널로 새는 토의 = '리더 혼자'처럼 보임).
        ch = self._thread_channel.get(int(channel_id), int(channel_id))
        return str(await sync_to_async(self._write)(
            channel_id=int(ch), thread_id=int(channel_id), sender_id=int(sender_id or 0),
            msg_type="plain", body=str(content), reply_to=(int(reply_to) if reply_to else None)))

    async def send_request(self, thread_id, sender_id, to_id, kind, body):
        ch = self._thread_channel.get(int(thread_id), int(thread_id))
        k = "W" if (str(getattr(kind, "value", kind)).lower().startswith("w")) else "I"
        return str(await sync_to_async(self._write)(
            channel_id=ch, thread_id=int(thread_id), sender_id=int(sender_id),
            msg_type="request", to_id=int(to_id) if to_id else None, kind=k, body=str(body)))

    async def send_response(self, thread_id, sender_id, request_msg_id, body):
        ch = self._thread_channel.get(int(thread_id), int(thread_id))
        return str(await sync_to_async(self._write)(
            channel_id=ch, thread_id=int(thread_id), sender_id=int(sender_id),
            msg_type="response", reply_to=(int(request_msg_id) if request_msg_id else None), body=str(body)))

    async def read_thread(self, thread_id, limit=50, include_plain=False):
        """GuideMessage → 구조화 Request/Response 재구성(시간순). Rule이 상태를 되읽는 핵심."""
        def _read():
            qs = GuideMessage.objects.filter(thread_id=int(thread_id)).order_by("msg_id")
            rows = list(qs)[-limit:]
            out = []
            for m in rows:
                if m.msg_type == "request":
                    out.append(Request(to_id=m.to_id, kind=(Kind.WORK if m.kind == "W" else Kind.INFO),
                                       body=m.body, from_id=m.sender_id, message_id=str(m.msg_id)))
                elif m.msg_type == "response":
                    out.append(Response(from_id=m.sender_id, body=m.body,
                                        reply_to=str(m.reply_to) if m.reply_to else None,
                                        message_id=str(m.msg_id)))
                elif m.msg_type == "plain" and include_plain and (m.body or "").strip():
                    out.append(Request(to_id=None, kind=Kind.WORK, body=m.body.strip(),
                                       from_id=m.sender_id, message_id=str(m.msg_id)))
            return out
        return await sync_to_async(_read)()

    async def edit_message(self, channel_id, message_id, content):
        def _e():
            GuideMessage.objects.filter(msg_id=int(message_id)).update(body=str(content), edited=True)
            self._touched.add(int(channel_id))
        await sync_to_async(_e)()

    # ── 정체성/직군 ────────────────────────────────────────────────
    async def assign_job_role(self, guild_id, user_id, job_name):
        def _a():
            Agent.objects.update_or_create(bot_id=int(user_id), defaults={"role": str(job_name)[:60]})
            return True
        return await sync_to_async(_a)()

    async def assign_job_roles(self, guild_id, id_to_job):
        n = 0
        for uid, job in (id_to_job or {}).items():
            if await self.assign_job_role(guild_id, uid, job):
                n += 1
        return n

    # ── 디스코드 전용(여기선 의미 없음) — 안전 no-op ───────────────
    def register_organt(self, user_id, client=None): pass

    @asynccontextmanager
    async def typing(self, channel_id, sender_id=None):
        # DiscordGuide.typing과 같은 계약: async with로 쓰는 컨텍스트 매니저(SNS엔 타이핑 표시 없음 → no-op).
        yield

    async def send_file(self, channel_id, path, sender_id=0, caption=""): return "0"
    async def react(self, channel_id, message_id, emoji): return None
    async def delete_message(self, channel_id, message_id): return None
    async def hide_channel(self, guild_id, channel_id): return None
    async def set_channel_topic(self, channel_id, topic): return True
    async def get_channel_topics(self, guild_id): return {}
    async def set_nick(self, guild_id, user_id, nick): return True
    async def set_nicks(self, guild_id, id_to_nick): return len(id_to_nick or {})
    async def get_member_jobs(self, guild_id, user_ids):
        def _g():
            return {a.bot_id: a.role for a in Agent.objects.filter(bot_id__in=[int(u) for u in user_ids])}
        return await sync_to_async(_g)()
    async def get_member_nicks(self, guild_id, user_ids): return {}
    async def get_custom_role_names(self, guild_id): return []
    async def get_guild_bot_nicks(self, guild_id): return None
    async def not_in_guild(self, guild_id, user_ids): return []

    @staticmethod
    def invite_url(app_id, perms=None):
        return f"(SNS 봇 #{app_id} — 초대 불필요, DB 정체성)"
