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
import hmac
import json
import time

from django.conf import settings
from django.db import transaction
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
    # 타이밍-안전 비교(HANDOFF §10 A — `==`는 길이/내용에 따라 조기 반환해 토큰 추측에 미세 단서).
    return bool(got) and hmac.compare_digest(got, token)


def _deny():
    return Response({"detail": "guide bridge 비활성 또는 인증 실패"}, status=status.HTTP_403_FORBIDDEN)


def _cap_body(s, n=200000):
    """무계 입력 방어(HANDOFF §10 MED). 한도는 매우 관대(정상 봇 보고는 수 KB) — 표기 없이 자르지 않는다
    (NEW_SESSION_GUIDE '침묵 절단 금지': 내용이 곧 산출물이면 잘림을 그 자리에 남긴다)."""
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "\n…[길이 한도 초과로 잘림]"


@api_view(["POST"])
@permission_classes([AllowAny])
def ingest(request):
    """두뇌 출력 1건을 GuideMessage로 기록. op별 필드는 SnsGuide 메서드와 1:1."""
    if not _authed(request):
        return _deny()
    d = request.data
    op = d.get("op")
    now = time.time()
    try:
        if op == "edit_message":
            GuideMessage.objects.filter(msg_id=int(d["message_id"])).update(
                body=_cap_body(d.get("body", "")), edited=True)
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
            body=_cap_body(d.get("body", "")), payload=d.get("payload") or {}, ts=now)
    except (KeyError, TypeError, ValueError) as e:
        return Response({"detail": f"필드 오류: {e}"}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"msg_id": m.msg_id}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([AllowAny])
def pending(request):
    """미처리 사용자/스튜디오 요청(sender_id=0). 봇 위임(sender_id=bot)은 흐름 내부라 제외."""
    if not _authed(request):
        return _deny()
    def _route_to(channel_id):
        # 봇 미지정 요청의 기본 담당 — ① 채널 지정 리더, ② 없으면 그 채널 최근 활동 봇(전역 임의 리더 쏠림 방지).
        from .models import Project
        pr = Project.objects.filter(id=channel_id).first()
        if pr and pr.leader_id:
            return int(pr.leader.bot_id)
        last = GuideMessage.objects.filter(channel_id=channel_id).exclude(sender_id=0).order_by("-msg_id").first()
        return int(last.sender_id) if last and last.sender_id else None
    # [잘린 빌드 자동 재개] 러너 churn(컨테이너가 supervisor를 회수)마다 진행 중 빌드가 죽고, 픽됨-미완으로
    # 남아 영영 재개 안 돼 긴 빌드(fps·todo)가 못 끝나던 근본. 러너는 살아있는 동안 picked_ts를 8초마다
    # touch한다 → picked_ts가 _RESUME_AFTER 넘게 멈췄으면 그 러너는 죽은 것 → 다시 큐로 내보내 이어받게.
    # 완료(done_ts)·사용자중지(stopped)·이미 응답받은 요청은 제외(재실행 방지).
    _RESUME_AFTER = 180   # 초 — touch 22회(8초 간격) 누락 = 확실히 사망
    now = time.time()
    responded = set(GuideMessage.objects.filter(msg_type="response").exclude(reply_to=None)
                    .values_list("reply_to", flat=True))
    out = []
    for m in GuideMessage.objects.filter(msg_type="request", sender_id=0).order_by("msg_id"):
        p = m.payload or {}
        if p.get("done_ts") or p.get("stopped"):
            continue
        if p.get("picked"):
            if m.msg_id in responded or (now - (p.get("picked_ts") or now)) < _RESUME_AFTER:
                continue                       # 살아있는(touch 중) 흐름이거나 이미 응답함 → 재개 안 함
        out.append({"msg_id": m.msg_id, "channel_id": m.channel_id, "to_id": m.to_id,
                    "kind": m.kind, "body": m.body, "route_to": _route_to(m.channel_id)})
    return Response({"pending": out})


@api_view(["POST"])
@permission_classes([AllowAny])
def pick(request):
    """요청을 '집음'(중복 처리 방지)·'완료'로 표시."""
    if not _authed(request):
        return _deny()
    try:
        mid = int(request.data["msg_id"])
    except (KeyError, TypeError, ValueError):
        return Response({"detail": "msg_id가 올바르지 않습니다."}, status=status.HTTP_400_BAD_REQUEST)
    # [원자성 — pick 레이스(이중 과금) 차단, HANDOFF §10 A] 종전 read-modify-write는 동시 폴/러너가
    # 같은 payload를 읽어 둘 다 '집음'으로 쓰면 한 요청을 두 번 처리(이중 토큰 과금)할 수 있었다.
    # select_for_update로 행을 잠가 직렬화한다(Postgres 행 잠금 · SQLite no-op라 테스트 무해).
    is_claim = not (request.data.get("unpick") or request.data.get("done") or request.data.get("touch"))
    with transaction.atomic():
        m = GuideMessage.objects.select_for_update().filter(msg_id=mid).first()
        if not m:
            return Response({"detail": "없음"}, status=status.HTTP_404_NOT_FOUND)
        p = dict(m.payload or {})
        if is_claim and p.get("picked"):
            # 다른 러너/폴이 이미 집음 — 레이스 패배. 재처리 금지(claimed=False).
            return Response({"ok": False, "claimed": False, "already_picked": True})
        if request.data.get("unpick"):                # 재처리용 — picked 해제(중단된 요청 다시 큐로)
            p.pop("picked", None)
            p.pop("done_ts", None)
            p.pop("picked_ts", None)
        else:
            p["picked"] = True
            if request.data.get("idle") is not None:
                p["idle_s"] = int(request.data["idle"])   # 실제 무진행(초) — 정직한 '조용'용
            if request.data.get("done"):
                p["done_ts"] = time.time()
            elif request.data.get("touch"):
                p["picked_ts"] = time.time()          # 진행 갱신 — 긴 흐름이 '멎음'으로 오판되지 않게
            else:
                p.setdefault("picked_ts", time.time())    # 멎은 요청 판정용
        GuideMessage.objects.filter(msg_id=mid).update(payload=p)
    return Response({"ok": True, "claimed": True} if is_claim else {"ok": True})


@api_view(["POST"])
@permission_classes([AllowAny])
def stop_channel(request):
    """채널의 '진행 중'(픽됨·미응답·미완) 요청을 '중지됨'으로 종결한다. 흐름이 *안 도는 사이* 누른
    중지가 화면(작업 중 해제)·재처리(재픽 차단)에 반영되게 — 도는 흐름은 러너가 request_cancel로 끊는다.
    러너 전역 stop 스캔이 호출(inflight 아닌 채널의 중지 유실 방지). 멱등."""
    if not _authed(request):
        return _deny()
    try:
        ch = int(request.data["channel"])
    except (KeyError, TypeError, ValueError):
        return Response({"detail": "channel이 올바르지 않습니다."}, status=status.HTTP_400_BAD_REQUEST)
    responded = set(GuideMessage.objects.filter(channel_id=ch, msg_type="response")
                    .exclude(reply_to=None).values_list("reply_to", flat=True))
    n = 0
    for m in GuideMessage.objects.filter(channel_id=ch, sender_id=0, msg_type="request").order_by("-msg_id"):
        p = m.payload or {}
        if p.get("picked") and not p.get("done_ts") and m.msg_id not in responded:
            p = dict(p); p["stopped"] = True; p["done_ts"] = time.time()   # 종결(작업중·멎음 아님)
            GuideMessage.objects.filter(msg_id=m.msg_id).update(payload=p)
            n += 1
    return Response({"stopped": n})


@api_view(["GET"])
@permission_classes([AllowAny])
def thread(request):
    """read_thread 재구성용 원시 행(시간순). 클라가 Request/Response/plain으로 복원한다."""
    if not _authed(request):
        return _deny()
    try:
        tid = int(request.query_params.get("thread_id"))
        limit = max(1, min(500, int(request.query_params.get("limit", 50))))   # clamp(무한 limit 방지)
    except (TypeError, ValueError):
        return Response({"detail": "thread_id가 올바르지 않습니다."}, status=status.HTTP_400_BAD_REQUEST)
    # ORM 슬라이스(HANDOFF §10 A 무한쿼리) — 종전엔 스레드 *전량*을 파이썬으로 로드 후 [-limit:].
    # 최신 limit개만 DB에서 가져와 시간순으로 되돌린다(긴 스레드 메모리·지연 방지).
    rows = list(GuideMessage.objects.filter(thread_id=tid).order_by("-msg_id")[:limit])[::-1]
    return Response({"rows": [
        {"msg_id": m.msg_id, "msg_type": m.msg_type, "sender_id": m.sender_id, "to_id": m.to_id,
         "kind": m.kind, "reply_to": m.reply_to, "body": m.body} for m in rows]})


@api_view(["POST"])
@permission_classes([AllowAny])
def heartbeat(request):
    """러너 생존 신호 — 폴마다 호출. stats가 engine_live를 파생(정적 안내문 대신 실제 가동 표시)."""
    if not _authed(request):
        return _deny()
    from .models import EngineHeartbeat
    EngineHeartbeat.beat(note=(request.data.get("note") or "remote"))
    return Response({"ok": True})


@api_view(["GET"])
@permission_classes([AllowAny])
def stops(request):
    """러너 폴 — '작업 중지' 신호 조회+소거(처리 위임). `?channel=`이면 그 채널만(진행 중인 흐름 단위),
    없으면 전체 목록(디버그/일괄). 원격 배치용. 멱등(재클릭 가능)."""
    if not _authed(request):
        return _deny()
    from .models import StopSignal
    ch = request.query_params.get("channel")
    if ch:
        try:
            ch = int(ch)
        except (TypeError, ValueError):
            return Response({"detail": "channel이 올바르지 않습니다."}, status=status.HTTP_400_BAD_REQUEST)
        n = StopSignal.objects.filter(channel_id=ch).delete()[0]
        return Response({"stopped": n > 0})
    chans = list(StopSignal.objects.values_list("channel_id", flat=True))
    if chans:
        StopSignal.objects.filter(channel_id__in=chans).delete()
    return Response({"channels": chans})


@api_view(["GET"])
@permission_classes([AllowAny])
def interjects(request):
    """러너 폴 — 사람 '진행 중 개입' 신호 조회+소거. `?channel=`이면 그 채널만. {target_id, text} 목록 반환
    (러너가 deliver_human_info로 주입). 소거-on-read = 멱등. 원격 배치용."""
    if not _authed(request):
        return _deny()
    from .models import InterjectSignal
    ch = request.query_params.get("channel")
    qs = InterjectSignal.objects.all()
    if ch:
        try:
            qs = qs.filter(channel_id=int(ch))
        except (TypeError, ValueError):
            return Response({"detail": "channel이 올바르지 않습니다."}, status=status.HTTP_400_BAD_REQUEST)
    sigs = list(qs.order_by("id"))
    if sigs:
        InterjectSignal.objects.filter(id__in=[s.id for s in sigs]).delete()
    return Response({"infos": [{"channel_id": s.channel_id, "target_id": s.target_id, "text": s.text}
                               for s in sigs]})


@api_view(["GET"])
@permission_classes([AllowAny])
def deploy_creds(request):
    """배포 자격증명(BYO) — `?channel=`의 프로젝트 *소유자 금고*에서 복호화한 배포 키를 반환한다. 봇이 전역
    env가 아니라 각 프로젝트 소유자의 키로 배포(자기 키·자기 책임). 복호화는 *서버 내부에서만* — 사람 API로는
    절대 안 나가는 값이다(러너만 이 엔드포인트로 받음, 토큰 인증 필수)."""
    if not _authed(request):
        return _deny()
    ch = request.query_params.get("channel")
    if not ch:
        return Response({"creds": {}})
    try:
        ch = int(ch)
    except (TypeError, ValueError):
        return Response({"detail": "channel이 올바르지 않습니다."}, status=status.HTTP_400_BAD_REQUEST)
    proj = Project.objects.filter(id=ch).select_related("owner").first()
    if not proj or not proj.owner:
        return Response({"creds": {}})
    from .social import deploy_creds_for
    return Response({"creds": deploy_creds_for(proj.owner)})
