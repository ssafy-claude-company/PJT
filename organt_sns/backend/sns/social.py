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
    a = Friendship.objects.filter(a=person, status="accepted").values_list("b_id", flat=True)
    b = Friendship.objects.filter(b=person, status="accepted").values_list("a_id", flat=True)
    return set(a) | set(b)


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def friends(request):
    """GET: 내 친구(수락된). POST {handle}: 친구 '요청' 보내기(상대 수락 필요). 인증 필요."""
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
        existing = Friendship.objects.filter(Q(a=cur, b=other) | Q(a=other, b=cur)).first()
        if existing and existing.status == "accepted":
            return Response({"status": "friends", "detail": "이미 친구예요."})
        if existing:
            if existing.b_id == cur.id:           # 상대가 먼저 보낸 요청 → 바로 수락
                existing.status = "accepted"
                existing.save(update_fields=["status"])
                return Response({"status": "accepted", "detail": "친구가 됐어요."})
            return Response({"status": "requested", "detail": "이미 요청을 보냈어요."})
        Friendship.objects.create(a=cur, b=other, status="pending")
        return Response({"status": "requested", "detail": "친구 요청을 보냈어요."})
    fr = Person.objects.filter(id__in=_friend_ids(cur)).order_by("handle")
    return Response({"friends": [_pub(p) for p in fr]})


@api_view(["GET"])
@permission_classes([AllowAny])
def friend_requests(request):
    """받은/보낸 친구 요청(대기 중). 인증 필요."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    incoming = Friendship.objects.filter(b=cur, status="pending").select_related("a")
    outgoing = Friendship.objects.filter(a=cur, status="pending").select_related("b")
    return Response({"incoming": [_pub(f.a) for f in incoming],
                     "outgoing": [_pub(f.b) for f in outgoing]})


@api_view(["POST"])
@permission_classes([AllowAny])
def accept_friend(request, handle):
    """받은 친구 요청 수락. 인증 필요."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    other = Person.objects.filter(handle=str(handle).lower()).first()
    fr = Friendship.objects.filter(a=other, b=cur, status="pending").first() if other else None
    if not fr:
        return Response({"detail": "받은 요청이 없어요."}, status=404)
    fr.status = "accepted"
    fr.save(update_fields=["status"])
    return Response({"ok": True})


@api_view(["DELETE"])
@permission_classes([AllowAny])
def unfriend(request, handle):
    """친구 삭제 / 보낸 요청 취소 / 받은 요청 거절 — 그 핸들과의 관계 행 제거. 인증 필요."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    other = Person.objects.filter(handle=str(handle).lower()).first()
    if other:
        Friendship.objects.filter(Q(a=cur, b=other) | Q(a=other, b=cur)).delete()
    return Response({"ok": True})


# ── 채널 멤버·초대·워크스페이스 ──────────────────────────────
@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def members(request, pid):
    """GET: 참여중 멤버 + 초대중(대기). POST {handle}: 친구를 채널에 '초대'(상대 수락 필요). 멤버만."""
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
        Membership.objects.get_or_create(person=other, project=proj,
                                         defaults={"role": "member", "status": "invited"})
    ms = proj.members.select_related("person").order_by("role", "created_at")  # 리드 먼저
    active = [{**_pub(m.person), "role": m.role} for m in ms if m.status == "active"]
    invited = [_pub(m.person) for m in ms if m.status == "invited"]
    return Response({"members": active, "invited": invited})


@api_view(["GET"])
@permission_classes([AllowAny])
def invites(request):
    """내가 받은 채널 초대(대기 중). 인증 필요."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    ms = (Membership.objects.filter(person=cur, status="invited").select_related("project")
          .order_by("-created_at"))
    out = [{"pid": m.project.pid, "name": m.project.name, "visibility": m.project.visibility,
            "owner_handle": m.project.owner.handle if m.project.owner else None} for m in ms]
    return Response({"invites": out})


@api_view(["POST", "DELETE"])
@permission_classes([AllowAny])
def invite_respond(request, pid):
    """채널 초대 수락(POST)/거절(DELETE). 인증 필요."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    proj = Project.objects.filter(pid=pid).first()
    m = Membership.objects.filter(person=cur, project=proj, status="invited").first() if proj else None
    if not m:
        return Response({"detail": "받은 초대가 없어요."}, status=404)
    if request.method == "DELETE":
        m.delete()
        return Response({"ok": True, "declined": True})
    m.status = "active"
    m.save(update_fields=["status"])
    return Response({"ok": True, "pid": proj.pid})


@api_view(["GET"])
@permission_classes([AllowAny])
def workspace(request):
    """내 워크스페이스 — 내가 '참여중'인 채널. 인증 필요."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    ms = (Membership.objects.filter(person=cur, status="active").select_related("project")
          .order_by("role", "-project__pid"))
    chans = [{"pid": m.project.pid, "name": m.project.name, "status": m.project.status,
              "role": m.role, "visibility": m.project.visibility} for m in ms]
    return Response({"channels": chans})


# ── 개인 자격증명 금고 — 범용 환경 변수 저장소(플랫폼 무관) ──────────────
# 어떤 이름이든 NAME=VALUE로 암호화 보관. 특정 플랫폼(Render 등)을 고정하지 않는다 —
# 배포 어댑터가 자기에게 필요한 키 이름을 알아서 골라 쓴다(deploy_creds_for(person, names)).
_SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def secrets(request):
    """GET: 내 시크릿 목록(이름+힌트만, 값 절대 미반환). POST: {name,value} 저장(암호화). 인증 필요."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    from .models import PersonSecret
    from .secrets_vault import encrypt, hint as _mk_hint
    if request.method == "POST":
        if cur.is_guest:
            return Response({"detail": "체험 계정은 키를 저장할 수 없어요. 회원가입 후 이용하세요."}, status=403)
        name = (request.data.get("name") or "").strip()
        value = request.data.get("value")
        if not _SECRET_NAME.match(name):
            return Response({"detail": "키 이름은 영문으로 시작, 영문/숫자/밑줄 64자 이내여야 해요."}, status=400)
        if not value or not str(value).strip():
            return Response({"detail": "값이 비었어요."}, status=400)
        value = str(value).strip()
        PersonSecret.objects.update_or_create(
            person=cur, name=name,
            defaults={"value_enc": encrypt(value), "hint": _mk_hint(value)})
    out = [{"name": s.name, "hint": s.hint, "updated_at": s.updated_at.timestamp()}
           for s in cur.secrets.all()]
    return Response({"secrets": out})


@api_view(["DELETE"])
@permission_classes([AllowAny])
def delete_secret(request, name):
    """내 시크릿 1개 삭제. 인증 필요."""
    cur = current_person(request)
    if not cur:
        return Response({"detail": "로그인이 필요해요."}, status=401)
    from .models import PersonSecret
    n = PersonSecret.objects.filter(person=cur, name=name).delete()[0]
    return Response({"deleted": n > 0})


def deploy_creds_for(person, names=None):
    """프로젝트 owner의 자격증명(복호화) — 배포 어댑터가 필요한 키 이름(names)을 주면 그것만,
    없으면 전부. 플랫폼 무관(Render·Vercel·Netlify… 어댑터가 자기 키를 골라 부른다).
    서버 내부 전용 — 절대 일반 API 응답으로 내보내지 않는다."""
    if not person:
        return {}
    from .models import PersonSecret
    from .secrets_vault import decrypt
    qs = PersonSecret.objects.filter(person=person)
    if names:
        qs = qs.filter(name__in=list(names))
    out = {}
    for s in qs:
        v = decrypt(s.value_enc)
        if v:
            out[s.name] = v
    return out
