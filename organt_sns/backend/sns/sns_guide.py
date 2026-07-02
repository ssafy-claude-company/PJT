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
    from system.protocol import Request, Response, Kind  # noqa
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

    def set_origin(self, channel_id):
        """[배달 계약] origin 채널 저장(로컬 ORM판은 thread_channel로 라우팅 — 계약 호환용)."""
        self._origin_channel = int(channel_id)

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
        if len(self._thread_channel) > 2000:           # 장수 러너 메모리 누수 방지(HANDOFF §10 MED) — 오래된
            self._thread_channel.pop(next(iter(self._thread_channel)))   # 항목 축출(라우팅 폴백 있어 안전)
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
                                        replies_to=str(m.reply_to) if m.reply_to else None,
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

    async def deploy_creds(self, channel_id):
        """배포 자격증명(BYO) — 채널의 프로젝트 *소유자 금고*에서 복호화한 키. 봇이 전역 env가 아니라
        각 소유자 키로 배포한다. 서버 내부 전용(사람 API로는 안 나감)."""
        if not channel_id:
            return {}
        def _get():
            from .models import Project
            from .social import deploy_creds_for
            proj = Project.objects.filter(id=int(channel_id)).select_related("owner").first()
            return deploy_creds_for(proj.owner) if (proj and proj.owner) else {}
        return await sync_to_async(_get)()

    # ── [배달 계약 구현 — HttpSnsGuide와 같은 계약의 ORM 구현체(단일컨테이너/로컬)] ──────
    # SYS(추상)가 쓰는 수신·claim·진행·완료·재개·살아있음·중지·interject를 같은 Django DB로 구현.
    # 원격판(HttpSnsGuide)은 guide_bridge API로, 이건 ORM 직접으로 — 같은 계약, 다른 구현체.
    def _route_to(self, channel_id):
        p = Project.objects.filter(id=channel_id).first()
        if p and p.leader_id:
            return int(p.leader.bot_id)
        last = (GuideMessage.objects.filter(channel_id=channel_id).exclude(sender_id=0)
                .order_by("-msg_id").first())
        return int(last.sender_id) if last else None

    async def get_pending(self):
        def _q():
            _RESUME_AFTER = 180   # picked_ts가 이만큼 멈췄으면 러너 사망 → 다시 큐로(guide_bridge.pending과 동형)
            now = _now()
            responded = set(GuideMessage.objects.filter(msg_type="response").exclude(reply_to=None)
                            .values_list("reply_to", flat=True))
            out = []
            for m in GuideMessage.objects.filter(msg_type="request", sender_id=0).order_by("msg_id"):
                p = m.payload or {}
                if p.get("done_ts") or p.get("stopped"):
                    continue
                if p.get("picked") and (m.msg_id in responded or (now - (p.get("picked_ts") or now)) < _RESUME_AFTER):
                    continue
                out.append({"msg_id": m.msg_id, "channel_id": m.channel_id, "to_id": m.to_id,
                            "kind": m.kind, "body": m.body, "route_to": self._route_to(m.channel_id)})
            return out
        return await sync_to_async(_q)()

    async def pick(self, msg_id, done=False, touch=False, unpick=False, idle=None):
        def _p():
            from django.db import transaction
            is_claim = not (unpick or done or touch)
            with transaction.atomic():
                m = GuideMessage.objects.select_for_update().filter(msg_id=msg_id).first()
                if not m:
                    return False
                p = dict(m.payload or {})
                if is_claim and p.get("picked"):
                    return False                 # 레이스 패배 — 재처리 금지
                if unpick:                        # 백스톱 컷 재개 — 픽 해제(다시 큐로)
                    p.pop("picked", None); p.pop("done_ts", None); p.pop("picked_ts", None)
                    GuideMessage.objects.filter(msg_id=msg_id).update(payload=p)
                    return True
                if idle is not None:
                    p["idle_s"] = int(idle)
                p["picked"] = True
                if done:
                    p["done_ts"] = _now()
                elif touch:
                    p["picked_ts"] = _now()
                else:
                    p.setdefault("picked_ts", _now())
                GuideMessage.objects.filter(msg_id=msg_id).update(payload=p)
            return True
        return await sync_to_async(_p)()

    async def heartbeat(self, note="local"):
        from .models import EngineHeartbeat
        await sync_to_async(EngineHeartbeat.beat)(note)

    async def check_stop(self, channel_id):
        def _s():
            from .models import StopSignal
            return StopSignal.objects.filter(channel_id=channel_id).delete()[0] > 0
        return await sync_to_async(_s)()

    async def all_stops(self):
        def _a():
            from .models import StopSignal
            chans = list(StopSignal.objects.values_list("channel_id", flat=True))
            if chans:
                StopSignal.objects.filter(channel_id__in=chans).delete()
            return [int(c) for c in chans]
        return await sync_to_async(_a)()

    async def mark_stopped(self, channel_id):
        def _m():
            responded = set(GuideMessage.objects.filter(channel_id=channel_id, msg_type="response")
                            .exclude(reply_to=None).values_list("reply_to", flat=True))
            n = 0
            for m in GuideMessage.objects.filter(channel_id=channel_id, sender_id=0, msg_type="request").order_by("-msg_id"):
                p = m.payload or {}
                if p.get("picked") and not p.get("done_ts") and m.msg_id not in responded:
                    p = dict(p); p["stopped"] = True; p["done_ts"] = _now()
                    GuideMessage.objects.filter(msg_id=m.msg_id).update(payload=p)
                    n += 1
            return n
        return await sync_to_async(_m)()

    async def check_interject(self, channel_id):
        def _i():
            from .models import InterjectSignal
            sigs = list(InterjectSignal.objects.filter(channel_id=channel_id).order_by("id"))
            if sigs:
                InterjectSignal.objects.filter(id__in=[s.id for s in sigs]).delete()
            return [{"target_id": s.target_id, "text": s.text} for s in sigs]
        return await sync_to_async(_i)()

    @staticmethod
    def invite_url(app_id, perms=None):
        return f"(SNS 봇 #{app_id} — 초대 불필요, DB 정체성)"
