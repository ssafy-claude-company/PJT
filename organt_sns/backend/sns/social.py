"""소셜·인증 API — 사람 유저 정체성(회원가입/로그인)·친구·채널 멤버·개인 워크스페이스.

  인증: 핸들 + 비밀번호로 가입/로그인 → 토큰 발급. 프론트는 토큰을 저장하고
  `Authorization: Token <토큰>` 헤더로 보낸다. current_person이 토큰으로 사용자를 식별한다.
  (비밀번호는 Django 해시로 저장. 데모지만 헤더-핸들 신뢰 같은 흉내가 아니라 실제 인증.)

  '모든 기능은 로그인 전제' — 소셜·쓰기 엔드포인트는 인증을 요구(미인증 401). 둘러보기용
  쇼케이스 읽기는 공개지만, 프론트 라우터가 앱 전체를 로그인 뒤로 가둔다.
"""
import re

from django.contrib.auth.hashers import make_password, check_password
from django.db.models import Q
from django.utils.crypto import get_random_string
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import Person, Friendship, Membership, Project

_HANDLE = re.compile(r"^[a-z0-9_]{2,30}$")
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}")


def current_person(request):
    """Authorization: Token/Bearer <토큰> → Person. 토큰 없거나 무효면 None."""
    auth = request.headers.get("Authorization") or ""
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in ("token", "bearer"):
        tok = parts[1].strip()
        if tok:
            return Person.objects.filter(token=tok).first()
    return None


def _pub(p):
    """공개 프로필(비번·토큰 제외)."""
    if not p:
        return None
    return {"handle": p.handle, "name": p.name, "color": p.color, "bio": p.bio, "is_guest": p.is_guest}


# ── 접근 제어(소유·멤버십) — 멀티유저 프라이버시의 핵심 ──────────
def is_member(proj, person):
    return bool(person) and Membership.objects.filter(project=proj, person=person).exists()


def is_owner(proj, person):
    return bool(person) and proj.owner_id == person.id


def can_read(proj, person):
    """공개 채널은 누구나, 비공개 채널은 멤버만 읽는다."""
    return proj.visibility == "public" or is_member(proj, person)


def _clean_color(c):
    return c if _HEX.fullmatch(c or "") else ""


def _issue(p):
    if not p.token:
        p.token = get_random_string(40)
        p.save(update_fields=["token"])
    return p.token


def _auth_ok(p):
    return Response({"me": _pub(p), "token": _issue(p)})


# ── 인증 ─────────────────────────────────────────────
@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    """회원가입. POST {handle, name?, password, color?}."""
    handle = (request.data.get("handle") or "").strip().lower()
    if not _HANDLE.match(handle):
        return Response({"detail": "핸들은 영소문자·숫자·_ 2~30자예요."}, status=400)
    pw = request.data.get("password") or ""
    if len(pw) < 4:
        return Response({"detail": "비밀번호는 4자 이상이어야 해요."}, status=400)
    if Person.objects.filter(handle=handle).exists():
        return Response({"detail": "이미 사용 중인 핸들이에요."}, status=409)
    name = (request.data.get("name") or handle).strip()[:60]
    p = Person(handle=handle, name=name, color=_clean_color(request.data.get("color")))
    p.password = make_password(pw)
    p.token = get_random_string(40)
    p.save()
    return _auth_ok(p)


@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):
    """로그인. POST {handle, password}."""
    handle = (request.data.get("handle") or "").strip().lower()
    pw = request.data.get("password") or ""
    p = Person.objects.filter(handle=handle).first()
    if not p or not p.password or not check_password(pw, p.password):
        return Response({"detail": "핸들 또는 비밀번호가 올바르지 않아요."}, status=401)
    return _auth_ok(p)


@api_view(["POST"])
@permission_classes([AllowAny])
def guest(request):
    """둘러보기 체험 계정 — 임의 핸들로 즉시 로그인(비번 없음, 일회성)."""
    for _ in range(6):
        suffix = get_random_string(6, "0123456789abcdefghijklmnopqrstuvwxyz")
        handle = f"guest_{suffix}"
        if not Person.objects.filter(handle=handle).exists():
            p = Person(handle=handle, name=f"체험{suffix[:4]}", is_guest=True, token=get_random_string(40))
            p.save()
            return _auth_ok(p)
    return Response({"detail": "체험 계정 생성 실패. 다시 시도하세요."}, status=500)


@api_view(["POST"])
@permission_classes([AllowAny])
def logout(request):
    """로그아웃 — 토큰 무효화."""
    cur = current_person(request)
    if cur:
        cur.token = ""
        cur.save(update_fields=["token"])
    return Response({"ok": True})


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def me(request):
    """GET: 현재 유저(토큰). POST: 프로필 수정(name/color/bio) — 인증 필요."""
    cur = current_person(request)
    if request.method == "GET":
        return Response({"me": _pub(cur)})
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    if "name" in request.data:
        cur.name = (request.data.get("name") or cur.name).strip()[:60]
    color = _clean_color(request.data.get("color"))
    if color:
        cur.color = color
    if "bio" in request.data:
        cur.bio = (request.data.get("bio") or "")[:160]
    cur.save()
    return Response({"me": _pub(cur)})


# ── 친구·검색 ─────────────────────────────────────────
@api_view(["GET"])
@permission_classes([AllowAny])
def people(request):
    """핸들·이름 검색(친구 추가용). 인증 필요."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    q = (request.query_params.get("q") or "").strip().lower()
    qs = Person.objects.filter(is_guest=False).exclude(id=cur.id)
    if q:
        qs = qs.filter(Q(handle__icontains=q) | Q(name__icontains=q))
    return Response({"people": [_pub(p) for p in qs.order_by("handle")[:20]]})


def _friend_ids(person):
    a = Friendship.objects.filter(a=person).values_list("b_id", flat=True)
    b = Friendship.objects.filter(b=person).values_list("a_id", flat=True)
    return set(a) | set(b)


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def friends(request):
    """GET: 내 친구. POST {handle}: 친구 추가(즉시 양방향). 인증 필요."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    if request.method == "POST":
        h = (request.data.get("handle") or "").strip().lower()
        other = Person.objects.filter(handle=h).first()
        if not other:
            return Response({"detail": "그 핸들의 유저가 없어요."}, status=404)
        if other.id == cur.id:
            return Response({"detail": "자기 자신은 추가할 수 없어요."}, status=400)
        if not Friendship.objects.filter(Q(a=cur, b=other) | Q(a=other, b=cur)).exists():
            Friendship.objects.create(a=cur, b=other)
    fr = Person.objects.filter(id__in=_friend_ids(cur)).order_by("handle")
    return Response({"friends": [_pub(p) for p in fr]})


@api_view(["DELETE"])
@permission_classes([AllowAny])
def unfriend(request, handle):
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    other = Person.objects.filter(handle=str(handle).lower()).first()
    if other:
        Friendship.objects.filter(Q(a=cur, b=other) | Q(a=other, b=cur)).delete()
    return Response({"ok": True})


# ── 채널 멤버·워크스페이스 ──────────────────────────────
@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def members(request, pid):
    """GET: 채널 멤버. POST {handle}: 친구를 채널에 초대. 멤버만(비공개 비노출)."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    proj = Project.objects.filter(pid=pid).first()
    if not proj or not can_read(proj, cur):     # 비공개 채널은 멤버 아니면 존재조차 숨김
        return Response({"detail": "채널 없음"}, status=404)
    if request.method == "POST":
        if not is_member(proj, cur):
            return Response({"detail": "이 채널의 멤버만 초대할 수 있어요."}, status=403)
        h = (request.data.get("handle") or "").strip().lower()
        other = Person.objects.filter(handle=h).first()
        if not other:
            return Response({"detail": "그 핸들의 유저가 없어요."}, status=404)
        Membership.objects.get_or_create(person=other, project=proj, defaults={"role": "member"})
    ms = proj.members.select_related("person").order_by("role", "created_at")  # 리드 먼저(lead<member)
    return Response({"members": [{**_pub(m.person), "role": m.role} for m in ms]})


@api_view(["GET"])
@permission_classes([AllowAny])
def workspace(request):
    """내 워크스페이스 — 내가 멤버/리드인 채널 목록. 인증 필요."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    ms = (Membership.objects.filter(person=cur).select_related("project")
          .order_by("role", "-project__pid"))
    chans = [{"pid": m.project.pid, "name": m.project.name, "status": m.project.status,
              "role": m.role, "visibility": m.project.visibility} for m in ms]
    return Response({"channels": chans})
