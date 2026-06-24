"""guide_bridge — 두뇌(러너)가 HTTPS로 SNS 매체에 말하는 입·출구 (Phase 2: REST SnsGuide).

  egress가 HTTPS 전용이라(외부 Postgres 5432 불가) 러너는 DB를 직접 못 만진다 — DiscordGuide가
  디스코드에 HTTPS로 말하듯, 러너의 HttpSnsGuide가 *이 API*로 말한다. 서버는 무상태(stateless):
  스레드→채널 매핑·id 생성은 클라이언트(러너)가 쥐고, 여긴 시키는 대로 GuideMessage를 쓴다.

    POST /api/guide/ingest/   {op, ...}        두뇌 출력(post/send_request/send_response/open_task/
                                               update_status/edit_message/assign_job_role) → 행 기록
    GET  /api/guide/pending/                   미처리 사용자 요청(sender_id=0,type=request) 폴링
    POST /api/guide/pick/     {msg_id, done?}  요청을 '집음/완료'로 표시(재처리 방지)
    GET  /api/guide/thread/?thread_id=         read_thread 재구성용 원시 행(클라가 Request/Response로 복원)

  인증: Authorization: Bearer <ORGANT_GUIDE_TOKEN>. 토큰 미설정이면 비활성(fail-closed) — 아무도 못 씀.
"""
import json
import time

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import GuideMessage, Agent, Project


def _authed(request):
    token = getattr(settings, "ORGANT_GUIDE_TOKEN", "") or ""
    if not token:
        return False                                  # fail-closed: 토큰 미설정 = 비활성
    got = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return bool(got) and got == token


def _deny():
    return Response({"detail": "guide bridge 비활성 또는 인증 실패"}, status=status.HTTP_403_FORBIDDEN)


@api_view(["POST"])
@permission_classes([AllowAny])
def ingest(request):
    """두뇌 출력 1건을 GuideMessage로 기록. op별 필드는 SnsGuide 메서드와 1:1."""
    if not _authed(request):
        return _deny()
    d = request.data
    op = d.get("op")
    now = time.time()
    if op == "edit_message":
        GuideMessage.objects.filter(msg_id=int(d["message_id"])).update(
            body=str(d.get("body", "")), edited=True)
        return Response({"ok": True})
    if op == "update_status":
        GuideMessage.objects.filter(msg_id=int(d["status_msg_id"])).update(
            body=str(d.get("body", "")), edited=True, payload=d.get("payload") or {})
        return Response({"ok": True, "msg_id": int(d["status_msg_id"])})
    # 신규 행 기록(post/send_request/send_response/open_task)
    m = GuideMessage.objects.create(
        channel_id=int(d["channel_id"]), thread_id=int(d.get("thread_id") or d["channel_id"]),
        sender_id=int(d.get("sender_id") or 0), msg_type=d.get("msg_type", "plain"),
        to_id=(int(d["to_id"]) if d.get("to_id") else None),
        kind=(d.get("kind") or ""), reply_to=(int(d["reply_to"]) if d.get("reply_to") else None),
        body=str(d.get("body", "")), payload=d.get("payload") or {}, ts=now)
    return Response({"msg_id": m.msg_id}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([AllowAny])
def pending(request):
    """미처리 사용자/스튜디오 요청(sender_id=0). 봇 위임(sender_id=bot)은 흐름 내부라 제외."""
    if not _authed(request):
        return _deny()
    out = []
    for m in GuideMessage.objects.filter(msg_type="request", sender_id=0).order_by("msg_id"):
        if (m.payload or {}).get("picked"):
            continue
        out.append({"msg_id": m.msg_id, "channel_id": m.channel_id, "to_id": m.to_id,
                    "kind": m.kind, "body": m.body})
    return Response({"pending": out})


@api_view(["POST"])
@permission_classes([AllowAny])
def pick(request):
    """요청을 '집음'(중복 처리 방지)·'완료'로 표시."""
    if not _authed(request):
        return _deny()
    mid = int(request.data["msg_id"])
    m = GuideMessage.objects.filter(msg_id=mid).first()
    if not m:
        return Response({"detail": "없음"}, status=status.HTTP_404_NOT_FOUND)
    p = dict(m.payload or {})
    p["picked"] = True
    if request.data.get("done"):
        p["done_ts"] = time.time()
    GuideMessage.objects.filter(msg_id=mid).update(payload=p)
    return Response({"ok": True})


@api_view(["GET"])
@permission_classes([AllowAny])
def thread(request):
    """read_thread 재구성용 원시 행(시간순). 클라가 Request/Response/plain으로 복원한다."""
    if not _authed(request):
        return _deny()
    tid = int(request.query_params.get("thread_id"))
    limit = int(request.query_params.get("limit", 50))
    rows = list(GuideMessage.objects.filter(thread_id=tid).order_by("msg_id"))[-limit:]
    return Response({"rows": [
        {"msg_id": m.msg_id, "msg_type": m.msg_type, "sender_id": m.sender_id, "to_id": m.to_id,
         "kind": m.kind, "reply_to": m.reply_to, "body": m.body} for m in rows]})
