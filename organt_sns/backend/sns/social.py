"""소셜(멀티유저) API — 사람 유저 정체성·친구·채널 멤버.

  데모 정체성: 비밀번호 없이 @핸들 기반. 프론트가 X-Organt-User 헤더로 현재 핸들을 보내면
  그 Person으로 본다(포트폴리오 데모용 — 실서비스면 세션/OAuth로 교체). 누구나 핸들을 만들어
  친구를 맺고 채널을 공유할 수 있어 '멀티유저 소셜 협업'을 그대로 시연한다.
"""
import re

from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import Person, Friendship, Membership, Project

_HANDLE = re.compile(r"^[a-z0-9_]{2,30}$")


def current_person(request):
    h = (request.headers.get("X-Organt-User") or "").strip().lower()
    return Person.objects.filter(handle=h).first() if h else None


def _pub(p):
    return {"handle": p.handle, "name": p.name, "color": p.color, "bio": p.bio} if p else None


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def me(request):
    """GET: 현재 유저(헤더). POST {handle,name,color?,bio?}: 가입/로그인·프로필 수정."""
    if request.method == "GET":
        return Response({"me": _pub(current_person(request))})
    handle = (request.data.get("handle") or "").strip().lower()
    if not _HANDLE.match(handle):
        return Response({"detail": "핸들은 영소문자·숫자·_ 2~30자."}, status=400)
    name = (request.data.get("name") or handle).strip()[:60]
    color = request.data.get("color") or ""
    color = color if re.fullmatch(r"#[0-9a-fA-F]{3,8}", color or "") else ""
    p, _ = Person.objects.get_or_create(handle=handle, defaults={"name": name, "color": color})
    # 본인이면 프로필 수정 허용(데모: 헤더가 본인 핸들일 때)
    cur = current_person(request)
    if cur is None or cur.handle == handle:
        p.name = name or p.name
        if color:
            p.color = color
        if "bio" in request.data:
            p.bio = (request.data.get("bio") or "")[:160]
        p.save()
    return Response({"me": _pub(p)}, status=200)


@api_view(["GET"])
@permission_classes([AllowAny])
def people(request):
    """핸들·이름 검색(친구 추가용). /api/people/?q="""
    q = (request.query_params.get("q") or "").strip().lower()
    qs = Person.objects.all()
    if q:
        qs = qs.filter(Q(handle__icontains=q) | Q(name__icontains=q))
    cur = current_person(request)
    if cur:
        qs = qs.exclude(id=cur.id)
    return Response({"people": [_pub(p) for p in qs.order_by("handle")[:20]]})


def _friend_ids(person):
    a = Friendship.objects.filter(a=person).values_list("b_id", flat=True)
    b = Friendship.objects.filter(b=person).values_list("a_id", flat=True)
    return set(a) | set(b)


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def friends(request):
    """GET: 내 친구 목록. POST {handle}: 친구 추가(즉시 양방향)."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "먼저 프로필을 만들어 주세요."}, status=401)
    if request.method == "POST":
        h = (request.data.get("handle") or "").strip().lower()
        other = Person.objects.filter(handle=h).first()
        if not other:
            return Response({"detail": "그 핸들의 유저가 없어요."}, status=404)
        if other.id == cur.id:
            return Response({"detail": "자기 자신은 추가할 수 없어요."}, status=400)
        if not Friendship.objects.filter(Q(a=cur, b=other) | Q(a=other, b=cur)).exists():
            Friendship.objects.create(a=cur, b=other)
    fids = _friend_ids(cur)
    fr = Person.objects.filter(id__in=fids).order_by("handle")
    return Response({"friends": [_pub(p) for p in fr]})


@api_view(["DELETE"])
@permission_classes([AllowAny])
def unfriend(request, handle):
    cur = current_person(request)
    other = Person.objects.filter(handle=str(handle).lower()).first()
    if cur and other:
        Friendship.objects.filter(Q(a=cur, b=other) | Q(a=other, b=cur)).delete()
    return Response({"ok": True})


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def members(request, pid):
    """GET: 채널 멤버. POST {handle}: 친구를 채널에 초대(멤버 추가)."""
    proj = Project.objects.filter(pid=pid).first()
    if not proj:
        return Response({"detail": "채널 없음"}, status=404)
    if request.method == "POST":
        cur = current_person(request)
        if not cur:
            return Response({"detail": "먼저 프로필을 만들어 주세요."}, status=401)
        h = (request.data.get("handle") or "").strip().lower()
        other = Person.objects.filter(handle=h).first()
        if not other:
            return Response({"detail": "그 핸들의 유저가 없어요."}, status=404)
        Membership.objects.get_or_create(person=other, project=proj, defaults={"role": "member"})
    ms = proj.members.select_related("person")
    return Response({"members": [{**_pub(m.person), "role": m.role} for m in ms]})
