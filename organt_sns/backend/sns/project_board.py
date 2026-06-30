"""프로젝트 산출물(Article)·작업(Task) 보드 — 채팅을 안 읽어도 '결과물'과 '작업 단위'를 본다.

  · build_deliverables: 배포 링크·저장소(repo) 링크·기타 산출 링크를 한곳에. 라이브(GuideMessage 봇 본문)
    + 쇼케이스(Event task_complete/deploy)에서 URL을 추출·분류·중복제거. (사용자 요청 1: '배포/ repo 링크 창')
  · build_tasks: 프로젝트 내 Task를 단위로 — 담당·상태·교차검증·배포·산출 링크. 디스코드처럼 task별 관리.
    라이브는 task 스레드가 없으니(open_task가 thread_id=channel_id) CollabTask(쇼케이스)와
    '완료 보고' 이벤트를 합쳐 작업 단위를 복원한다. (사용자 요청 2: 'task별 관리')

  뷰(article 액션)가 둘을 묶어 한 응답으로 — 프런트의 '산출물·작업' 패널이 그대로 렌더.
"""
import re

from .models import Agent, Event, GuideMessage
from .guide_format import to_native

_URL = re.compile(r"https?://[^\s)>\]\"'}`*]+")   # 마크다운(**·`)·코드 경계도 끊어 깨끗이
# 배포 호스트(라이브 URL) — onrender가 데모 기본. 저장소(repo)와 구분해 '라이브'로 강조.
_DEPLOY_HOSTS = ("onrender.com", "vercel.app", "netlify.app", "herokuapp.com", "railway.app",
                 "fly.dev", "pages.dev", "github.io", "surge.sh", "web.app", "firebaseapp.com",
                 "cloudfront.net", "deno.dev")
_REPO_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
# 산출물이 *아닌* 링크 — 의존성 CDN·폰트·로컬·예시 호스트는 산출물 카드에서 제외(지성 있는 필터).
_NOISE_HOSTS = ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com", "esm.sh", "cdn.skypack.dev",
                "fonts.googleapis.com", "fonts.gstatic.com", "ajax.googleapis.com", "polyfill.io",
                "image.tmdb.org", "img.youtube.com", "via.placeholder.com", "placehold.co",
                "localhost", "127.0.0.1", "0.0.0.0", "example.com", "example.org",
                "schema.org", "w3.org", "tailwindcss.com", "bootstrapcdn.com")
# 미완/템플릿 꼬리 — 의미 있는 ID 없이 끝나는 임베드·이미지 사이즈 스텁(코드에서 긁힌 예시)
_NOISE_TAILS = ("/embed", "/embed/", "/watch", "/w500", "/w780", "/w1280", "/original", "/api/")


def classify_link(url):
    """URL을 deploy(라이브)·repo(저장소)·link(기타)로 분류 — 산출물 카드 종류."""
    u = (url or "").lower()
    if any(h in u for h in _REPO_HOSTS):
        return "repo"
    if any(h in u for h in _DEPLOY_HOSTS):
        return "deploy"
    return "link"


def _clean_url(u):
    u = (u or "").strip()
    return u.rstrip(".,;:!?)]}'\"> *`")        # 끝 마크다운(**·`·*)·문장부호 제거


def _is_deliverable(url):
    """이 URL이 *실제 산출물*인가 — 의존성 CDN·로컬·예시·템플릿 리터럴·코드조각은 제외(지성 필터)."""
    if not url or len(url) < 12:               # http://a.bc 미만 = 의미 없음
        return False
    if any(t in url for t in ("${", "{{", "}}", "<", ">", "`", "[", "]", " ")):  # 템플릿/코드 조각
        return False
    u = url.lower()
    host_path = re.sub(r"^https?://", "", u).rstrip("/")
    host = host_path.split("/", 1)[0]
    if "." not in host or host.startswith(".") or host.endswith("."):
        return False                            # 호스트 형태 아님
    if any(h in u for h in _NOISE_HOSTS):       # 의존성 CDN·폰트·예시·로컬
        return False
    if any(host_path.endswith(t) or host_path.endswith(t.rstrip("/")) for t in _NOISE_TAILS):
        return False                            # 미완 임베드/이미지 템플릿 꼬리
    return True


def _site_root(url):
    """배포 URL은 사이트 루트로 정규화 — 같은 사이트의 /·/health·여러 페이지가 카드 1개로."""
    m = re.match(r"https?://[^/\s]+", url)
    return m.group(0) if m else url


def _repo_root(url):
    """저장소 URL은 host/owner/repo로 정규화 — /blob·/tree 등 같은 repo가 카드 1개로."""
    parts = [p for p in re.sub(r"^https?://", "", url).split("/") if p]
    return ("https://" + "/".join(parts[:3])) if len(parts) >= 3 else url


def _label(url, kind):
    """카드에 보일 짧은 라벨 — repo는 owner/repo, 그 외는 호스트(+짧은 경로)."""
    m = re.sub(r"^https?://", "", url or "").rstrip("/")
    if kind == "repo":
        parts = [p for p in m.split("/") if p]
        if len(parts) >= 3:                  # github.com/owner/repo → owner/repo
            return "/".join(parts[1:3])
    return m[:48]


def build_deliverables(proj):
    """프로젝트의 산출 링크 모음 — deploy 먼저, 그다음 repo, 기타. 중복제거·최신 우선."""
    found = {}                               # url → {type,url,label,ts,by_role}

    def add(url, ts, role):
        url = _clean_url(url)
        if not _is_deliverable(url):         # 의존성·템플릿·예시·코드조각 걸러냄
            return
        kind = classify_link(url)
        if kind == "deploy":
            url = _site_root(url)            # 같은 사이트(/·/health·페이지들) → 카드 1개
        elif kind == "repo":
            url = _repo_root(url)            # 같은 repo(/blob·/tree) → 카드 1개
        if url in found:                     # 중복: 가장 이른 시각·역할 보존
            if ts and (not found[url]["ts"] or ts < found[url]["ts"]):
                found[url]["ts"] = ts
            if role and not found[url]["by_role"]:
                found[url]["by_role"] = role
            return
        found[url] = {"type": kind, "url": url, "label": _label(url, kind),
                      "ts": ts or 0, "by_role": role}

    # 쇼케이스 — task_complete 보고의 result, deploy 이벤트 본문/payload
    for e in proj.events.filter(kind__in=["task_complete", "deploy"]).select_related("actor"):
        p = e.payload or {}
        text = " ".join(str(x) for x in (p.get("result"), p.get("body"), p.get("url"), e.summary) if x)
        role = e.actor.role if e.actor else None
        for u in _URL.findall(text):
            add(u, e.ts, role)
    # 라이브 — 봇이 채널에 올린 메시지 본문(배포 URL 게시 등). sender 0(사람/시스템)은 제외.
    ag = {a.bot_id: a for a in Agent.objects.exclude(bot_id=0)}
    for gm in GuideMessage.objects.filter(channel_id=proj.id).exclude(sender_id=0):
        body = " ".join(str(x) for x in (gm.body, (gm.payload or {}).get("result")) if x)
        for u in _URL.findall(body):
            a = ag.get(gm.sender_id)
            add(u, gm.ts, a.role if a else None)

    order = {"deploy": 0, "repo": 1, "link": 2}
    return sorted(found.values(), key=lambda d: (order.get(d["type"], 9), -(d["ts"] or 0)))


def build_tasks(proj, deliverables=None):
    """프로젝트 내 Task 단위 — 담당·상태·교차검증·배포 누계 + 그 작업의 산출 링크(있으면).
    CollabTask(쇼케이스·라이브 ingest)가 1급 소스. 각 Task의 링크는 그 owner가 올린 산출에서 추출.
    deliverables를 넘기면 재추출 안 함(article 뷰가 한 번만 계산해 공유)."""
    deliv = deliverables if deliverables is not None else build_deliverables(proj)
    by_role = {}
    for d in deliv:
        by_role.setdefault(d["by_role"], []).append(d)
    out = []
    for t in proj.tasks.select_related("owner").order_by("task_id"):
        role = t.owner.role if t.owner else None
        title = to_native((t.purpose or t.goal or t.task_id or "").strip())[:140]
        out.append({
            "task_id": t.task_id,
            "title": title or t.task_id,
            "goal": to_native((t.goal or "").strip())[:300],
            "owner_role": role,
            "owner_name": t.owner.name if t.owner else None,
            "owner_id": str(t.owner.bot_id) if t.owner else None,
            "status": t.status or "",
            "cross_checks": t.cross_checks,
            "deploy_count": t.deploy_count,
            # 그 작업 owner가 만든 산출 링크(역할 기준 근사 — task별 링크 직접연결은 라이브에 없음)
            "deliverables": (by_role.get(role) or [])[:4] if role else [],
        })
    return out


def project_status(proj, has_messages=False):
    """프로젝트 한 줄 상태 — 완료(task_complete 있음)·진행 중(메시지 있음)·시작 전."""
    if proj.events.filter(kind="task_complete").exists():
        return "완료"
    return "진행 중" if has_messages else "시작 전"


def project_goal(proj):
    """프로젝트 목표 — goal_set 이벤트 우선, 없으면 첫 Task의 goal/purpose."""
    ge = proj.events.filter(kind="goal_set").order_by("-seq").first()
    if ge:
        g = (ge.payload or {}).get("goal") or (ge.payload or {}).get("body") or ge.summary
        if g:
            return to_native(g.strip())
    t0 = proj.tasks.first()
    if t0:
        return to_native((t0.goal or t0.purpose or "").strip())
    return ""
