"""HttpSnsGuide — SnsGuide의 HTTPS 클라이언트판(Phase 2 라이브).

  egress가 HTTPS 전용이라 러너는 원격 SNS DB를 직접 못 만진다. DiscordGuide가 디스코드에 HTTPS로
  말하듯, 이건 *guide_bridge API*로 말한다. SnsGuide와 같은 계약(post/send_request/send_response/
  open_task/update_status/edit_message/read_thread/…)을 그대로 구현 — Sys/Flow에 드롭인 가능.

  무상태 서버를 위해 스레드→채널 매핑·id 생성은 여기(클라)서 쥔다(ORM SnsGuide와 동일 로직).
  동기 requests 호출은 asyncio.to_thread로 감싸 이벤트 루프를 막지 않는다.
"""
import asyncio
import itertools
import os
import sys
import time
from contextlib import asynccontextmanager

import requests

# protocol 객체(Request/Response/Kind) — read_thread 재구성용.
_PJT = os.environ.get("ORGANT_PJT", "/home/user/PJT")
if _PJT not in sys.path:
    sys.path.insert(0, _PJT)
try:
    from src.protocol import Request, Response, Kind  # noqa
except Exception:
    Request = Response = Kind = None


class HttpSnsGuide:
    """Rule ↔ 원격 SNS(guide_bridge) 전송기. 러너 프로세스 1개 안에서 Sys/Flow에 주입된다."""

    def __init__(self, base_url, token, timeout=30):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self._ids = itertools.count(int(time.time() * 1000))
        self._thread_channel = {}                          # thread_id → channel_id (클라 보유)
        self._origin_channel = None                        # 이 요청의 채널 — 협업을 여기로 라우팅(러너가 세팅)

    def _new_id(self):
        return next(self._ids)

    # ── HTTP 헬퍼(동기, 재시도) ────────────────────────────────────
    def _post_sync(self, path, payload):
        last = None
        for i in range(3):
            try:
                r = self._s.post(f"{self.base}{path}", json=payload, timeout=self.timeout)
                r.raise_for_status()
                return r.json() if r.content else {}
            except Exception as e:
                last = e
                time.sleep(1.5 * (i + 1))
        raise last

    def _get_sync(self, path, params=None):
        last = None
        for i in range(3):
            try:
                r = self._s.get(f"{self.base}{path}", params=params or {}, timeout=self.timeout)
                r.raise_for_status()
                return r.json() if r.content else {}
            except Exception as e:
                last = e
                time.sleep(1.5 * (i + 1))
        raise last

    async def _post(self, path, payload):
        return await asyncio.to_thread(self._post_sync, path, payload)

    async def _get(self, path, params=None):
        return await asyncio.to_thread(self._get_sync, path, params)

    # ── 채널/스레드 ────────────────────────────────────────────────
    async def create_project_channel(self, guild_id, name):
        # SNS-네이티브: 프로젝트 협업을 '요청이 온 채널'에 그대로 — 디스코드처럼 새 채널을 따로 안 만들고
        # 사용자가 보는 채널에 위임·작업·완료가 라이브로 뜨게 한다. origin 없으면 합성 id(폴백).
        if self._origin_channel:
            return int(self._origin_channel)
        return self._new_id()

    async def open_task(self, channel_id, status):
        tid = self._new_id()
        self._thread_channel[tid] = int(channel_id)
        res = await self._post("/api/guide/ingest/", {
            "op": "open_task", "channel_id": int(channel_id), "thread_id": int(channel_id),
            "sender_id": 0, "msg_type": "status",
            "body": f"[Task-{getattr(status,'task_id','?')}]",
            "payload": {"task_id": getattr(status, "task_id", None)}})
        return str(res.get("msg_id")), tid

    async def update_status(self, channel_id, status_msg_id, status):
        await self._post("/api/guide/ingest/", {
            "op": "update_status", "status_msg_id": int(status_msg_id),
            "body": f"[Task-{getattr(status,'task_id','?')}] {getattr(status,'state','')}",
            "payload": {"task_id": getattr(status, "task_id", None), "state": getattr(status, "state", None)}})
        return status_msg_id

    # ── 메시지 ─────────────────────────────────────────────────────
    async def post(self, channel_id, sender_id, content, reply_to=None):
        res = await self._post("/api/guide/ingest/", {
            "op": "post", "channel_id": int(channel_id), "thread_id": int(channel_id),
            "sender_id": int(sender_id or 0), "msg_type": "plain", "body": str(content),
            "reply_to": (int(reply_to) if reply_to else None)})
        return str(res.get("msg_id"))

    async def send_request(self, thread_id, sender_id, to_id, kind, body):
        ch = self._thread_channel.get(int(thread_id), int(thread_id))
        k = "W" if (str(getattr(kind, "value", kind)).lower().startswith("w")) else "I"
        res = await self._post("/api/guide/ingest/", {
            "op": "send_request", "channel_id": int(ch), "thread_id": int(thread_id),
            "sender_id": int(sender_id), "msg_type": "request",
            "to_id": (int(to_id) if to_id else None), "kind": k, "body": str(body)})
        return str(res.get("msg_id"))

    async def send_response(self, thread_id, sender_id, request_msg_id, body):
        ch = self._thread_channel.get(int(thread_id), int(thread_id))
        res = await self._post("/api/guide/ingest/", {
            "op": "send_response", "channel_id": int(ch), "thread_id": int(thread_id),
            "sender_id": int(sender_id), "msg_type": "response",
            "reply_to": (int(request_msg_id) if request_msg_id else None), "body": str(body)})
        return str(res.get("msg_id"))

    async def read_thread(self, thread_id, limit=50, include_plain=False):
        data = await self._get("/api/guide/thread/", {"thread_id": int(thread_id), "limit": limit})
        out = []
        for m in data.get("rows", []):
            if m["msg_type"] == "request":
                out.append(Request(to_id=m["to_id"], kind=(Kind.WORK if m["kind"] == "W" else Kind.INFO),
                                   body=m["body"], from_id=m["sender_id"], message_id=str(m["msg_id"])))
            elif m["msg_type"] == "response":
                out.append(Response(from_id=m["sender_id"], body=m["body"],
                                    reply_to=str(m["reply_to"]) if m["reply_to"] else None,
                                    message_id=str(m["msg_id"])))
            elif m["msg_type"] == "plain" and include_plain and (m["body"] or "").strip():
                out.append(Request(to_id=None, kind=Kind.WORK, body=m["body"].strip(),
                                   from_id=m["sender_id"], message_id=str(m["msg_id"])))
        return out

    async def edit_message(self, channel_id, message_id, content):
        await self._post("/api/guide/ingest/", {
            "op": "edit_message", "message_id": int(message_id), "body": str(content)})

    # ── 정체성/직군 — SNS 로스터는 스튜디오가 관리. 러너는 건드리지 않음(best-effort no-op) ──
    async def assign_job_role(self, guild_id, user_id, job_name): return True
    async def assign_job_roles(self, guild_id, id_to_job): return len(id_to_job or {})

    # ── 디스코드 전용 — 안전 no-op ─────────────────────────────────
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
    async def get_member_jobs(self, guild_id, user_ids): return {}
    async def get_member_nicks(self, guild_id, user_ids): return {}
    async def get_custom_role_names(self, guild_id): return []
    async def get_guild_bot_nicks(self, guild_id): return None
    async def not_in_guild(self, guild_id, user_ids): return []

    @staticmethod
    def invite_url(app_id, perms=None):
        return f"(SNS 봇 #{app_id} — 초대 불필요)"
