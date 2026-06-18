"""산출물 공개 배포 — GitHub repo push + Render 웹서비스 생성/갱신.

Guide의 `deploy` 리더 툴이 호출한다. 자격증명은 환경변수로 주입한다(코드/로그에 박지 않음):
  GH_PAT, GH_USER, RENDER_KEY, RENDER_OWNER
Node 앱(서버가 process.env.PORT 사용)만 지원한다. 같은 name으로 다시 부르면 갱신 배포한다.
"""
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

GITHUB_API = "https://api.github.com"
RENDER_API = "https://api.render.com/v1"
_TERMINAL_FAIL = ("build_failed", "update_failed", "canceled", "deactivated", "pre_deploy_failed")


def _http(method, url, token, data=None, retries=5):
    """응답을 못 받은 경우(네트워크/DNS 실패, 502/503/504 게이트웨이)에만 안전 재시도.
    egress 프록시의 api.render.com DNS 해석이 간헐 실패하므로(요청이 서버에 도달조차 못 함),
    비멱등 POST(배포 트리거)라도 재시도가 안전하다. 서버가 실제 응답한 4xx/유효 5xx는 즉시 반환."""
    body = json.dumps(data).encode() if data is not None else None
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        if body:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or "{}")
        except urllib.error.HTTPError as e:
            if e.code in (502, 503, 504) and attempt < retries - 1:   # 일시적 게이트웨이/프록시(DNS) — 재시도
                last = f"HTTP {e.code}"
                time.sleep(2 * (attempt + 1))
                continue
            try:
                return e.code, json.loads(e.read() or "{}")
            except Exception:
                return e.code, {}
        except Exception as e:                       # 네트워크/DNS 실패(응답 못 받음) — 재시도
            last = str(e)
            time.sleep(2 * (attempt + 1))
    return 0, {"error": last}


def _git(args, cwd):
    cmd = ["git", "-c", "commit.gpgsign=false", "-c", "user.email=deploy@organt.local",
           "-c", "user.name=Organt Deploy", *args]
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def _check_live(url, tries=6):
    """배포된 URL이 실제로 응답하는지 확인(콜드스타트 감안 재시도) → HTTP 코드 또는 None."""
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code           # 4xx/5xx도 서버가 응답한 것(라우팅은 됨)
        except Exception:
            time.sleep(8)           # 콜드스타트/미기동 — 재시도
    return None


def _verify_live_assets(url, workspace, limit=12, tries=3, wait=6, fetch=None):
    """[구조 검증 — 스테일 배포 차단] '배포 성공'을 선언하기 전에, 라이브가 **방금 만든 그 파일**을
    서빙하는지 바이트 대조로 증명한다. URL 200은 '서버가 떠 있다'까지만 보증한다 — 옛 빌드가
    캐시/이전 배포로 서빙되는데 '배포 완료'로 보고되던 부류(라이브 관측: 클라 수정이 라이브에
    안 보임 → 사용자 재보고)를 도구 레벨에서 원천 차단한다. 대조 대상 = 클라이언트가 실제로 받는
    public/* 정적 파일(서버 코드는 비서빙이라 대조 불가). public/ 없는 산출물(순수 API 서버 등)은
    생략. 직후 전파 지연을 감안해 재시도 후에도 다르면 불일치 목록을 반환(비면 통과)."""
    pub = Path(workspace) / "public"
    if not pub.is_dir():
        return []
    if fetch is None:
        def fetch(u):
            req = urllib.request.Request(u, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read()
    names = sorted(p.name for p in pub.iterdir() if p.is_file())[:limit]
    bad = []
    for attempt in range(tries):
        bad = []
        for nm in names:
            local = (pub / nm).read_bytes()
            try:
                live = fetch(f"{url.rstrip('/')}/{nm}")
            except Exception as e:
                bad.append(f"{nm}(조회 실패: {str(e)[:60]})")
                continue
            if live != local:
                bad.append(f"{nm}(라이브 {len(live)}B ≠ 산출물 {len(local)}B)")
        if not bad:
            return []
        if attempt < tries - 1:
            time.sleep(wait)      # 새 인스턴스/엣지 전파 직후의 일시 불일치 — 잠시 뒤 재대조
    return bad


def _measure_usability(url: str) -> str:
    """[품질 우선 — 기계적 사용성 측정(사용자 확정: 토큰<품질)] 배포 성공 후 실제 브라우저로 첫
    로드를 재본다 — 웹 산출물에서 '뜬다(HTTP 200)'와 '쓸 만하다'는 다르다(라이브 P-009: 200인데
    첫 로드 60s+, 브라우저 즉석 모델학습 렉을 200 검사가 통과시킴 — 사용자가 첫 발견).
    도메인 무관(웹이라는 산출물 형태에만 의존), best-effort — 측정 실패가 배포를 막지 않는다."""
    try:
        import time as _t
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            try:
                pg = b.new_page()
                errs = []
                pg.on("console", lambda m: errs.append(m.text[:80]) if m.type == "error" else None)
                pg.on("pageerror", lambda e: errs.append(str(e)[:80]))
                t0 = _t.time()
                try:
                    pg.goto(url, timeout=20000, wait_until="load")
                    note = f"첫 로드 {_t.time() - t0:.1f}s"
                except Exception:
                    note = "첫 로드 **20s 초과(미완)** — 사용자는 빈 화면을 봅니다"
                _t.sleep(2)   # 로드 직후 에러 수집 창
                e_note = f", 콘솔/페이지 에러 {len(errs)}건" + (f" (첫: {errs[0]})" if errs else "")
                return (f"\n[라이브 사용성 측정] {note}{e_note} — 수치가 나쁘면 '배포됨'이지 "
                        f"'완성'이 아닙니다(원인을 고치기 전 완료 보고 금지).")
            finally:
                b.close()
    except Exception as e:
        return f"\n[라이브 사용성 측정 불가(참고): {type(e).__name__}]"


def _onrender_subs(text: str) -> set:
    """문자열에서 참조되는 *.onrender.com 서비스 서브도메인(=서비스명) 집합."""
    return set(re.findall(r"https?://([a-z0-9][a-z0-9-]*)\.onrender\.com", text or ""))


def _referenced_services(projects_path=None) -> set:
    """등록 레지스트리(logs/projects.json)가 아직 참조하는 onrender 서비스명 집합(keep-set).
    '남아있는 채널이 링크로 가리키는' 서비스 — 풀 정리 시 절대 삭제 금지 대상."""
    p = Path(projects_path) if projects_path else (Path(__file__).resolve().parent.parent / "logs" / "projects.json")
    try:
        return _onrender_subs(p.read_text())
    except Exception:
        return set()


def _list_render_services(render_key) -> list:
    """Render 계정의 모든 서비스 → [{id,name,url,created}] (커서 페이지네이션)."""
    out, cursor = [], None
    for _ in range(20):
        u = f"{RENDER_API}/services?limit=100" + (f"&cursor={cursor}" if cursor else "")
        st, data = _http("GET", u, render_key)
        if st != 200 or not isinstance(data, list) or not data:
            break
        for item in data:
            s = item.get("service", item)
            out.append({"id": s.get("id"), "name": s.get("name"),
                        "url": (s.get("serviceDetails") or {}).get("url", ""),
                        "created": s.get("createdAt", "")})
            cursor = item.get("cursor")
        if len(data) < 100:
            break
    return out


def _free_slots(render_key, keep, want_free=2, cap=25) -> list:
    """[풀 자가관리 — 한도로 인한 작업 멈춤 차단] 무료 티어는 서비스 개수 상한(cap)이 있어, 풀이
    차면 신규 배포가 막혀 작업이 통째로 멈춘다(라이브 P-019: '한도 초과 → 사용자 보고로 마감').
    '현 채널이 참조하지 않는' 고아 서비스(옛 테스트·삭제된 채널·이름 중복)를 오래된 것부터 삭제해
    슬롯을 되찾는다 — keep-set(참조 중 링크)은 절대 건드리지 않는다. 슬롯이 이미 충분하면 아무것도
    안 한다(보수적: 한도 임박에서만 동작). 삭제된 서비스명 목록 반환."""
    try:
        svcs = _list_render_services(render_key)
    except Exception:
        return []
    if cap - len(svcs) >= want_free:
        return []
    orphans = [s for s in svcs
               if s["name"] not in keep and not (_onrender_subs(s["url"]) & keep)]
    orphans.sort(key=lambda s: s.get("created") or "")     # 오래된 고아부터
    need = want_free - (cap - len(svcs))
    deleted = []
    for s in orphans[:max(need, 0)]:
        if not s.get("id"):
            continue
        st, _ = _http("DELETE", f"{RENDER_API}/services/{s['id']}", render_key)
        if st in (200, 204):
            deleted.append(s["name"])
    return deleted


def deploy_sync(workspace, name, gh_pat, gh_user, render_key, owner_id, region="singapore"):
    """workspace를 name repo로 push하고 Render 웹서비스로 배포 → 결과 문자열(라이브 URL 포함)."""
    ws = Path(workspace)
    if not ws.exists() or not any(ws.iterdir()):
        return "배포 실패: 작업공간이 비어 있습니다(먼저 구현·검증하세요)."
    pkg = ws / "package.json"
    if not pkg.exists():
        return "배포 실패: package.json이 없습니다. Node 앱만 지원합니다(서버는 process.env.PORT 사용)."
    try:
        scripts = json.loads(pkg.read_text()).get("scripts", {})
    except Exception:
        scripts = {}
    start_cmd = "npm start" if scripts.get("start") else "node server.js"

    # 1) node_modules·.git 제외한 깨끗한 스테이징 사본
    stage = Path("/tmp") / f"deploy_{name}_{int(time.time())}"
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(ws, stage, ignore=shutil.ignore_patterns("node_modules", ".git", "*.log", ".env"))
    (stage / ".gitignore").write_text("node_modules/\n*.log\n.env\n")

    # 2) git init + commit (서명 끔)
    _git(["init", "-q", "-b", "main"], stage)
    _git(["add", "-A"], stage)
    _git(["commit", "-q", "-m", f"deploy {name}"], stage)

    # 3) GitHub repo 보장(있으면 422 → 재사용)
    st, resp = _http("POST", f"{GITHUB_API}/user/repos", gh_pat,
                     {"name": name, "private": False,
                      "description": f"{name} — deployed by Organt Core multi-agent system"})
    if st not in (201, 422):
        return f"배포 실패(GitHub repo): HTTP {st} {resp.get('message', '')}"
    repo_url = f"https://github.com/{gh_user}/{name}"

    # 4) push(force — 재배포 시 최신 상태로 덮어씀)
    push_url = f"https://x-access-token:{gh_pat}@github.com/{gh_user}/{name}.git"
    rc, out = _git(["push", "-q", "-f", push_url, "main:main"], stage)
    shutil.rmtree(stage, ignore_errors=True)
    if rc != 0:
        return f"배포 실패(git push): {out[-300:]}"

    # 5) 기존 서비스 찾기 → 있으면 재배포, 없으면 생성
    st, svcs = _http("GET", f"{RENDER_API}/services?name={name}&limit=10", render_key)
    sid, url = None, ""
    if isinstance(svcs, list):
        for x in svcs:
            s = x.get("service", x)
            if s.get("name") == name:
                sid = s.get("id")
                url = s.get("serviceDetails", {}).get("url", "")
                break
    dep_id = None
    if sid:
        st, dep = _http("POST", f"{RENDER_API}/services/{sid}/deploys", render_key, {})
        dep_id = dep.get("id") if isinstance(dep, dict) else None   # 방금 트리거한 '그' 배포
    else:
        # 신규 서비스 생성 전에 풀이 한도에 임박했으면 '참조 없는 고아'를 정리해 슬롯을 확보한다
        # (참조 중 링크는 보존). 한도가 차서 작업이 멈추던 구멍(P-019)을 배포 경로가 스스로 막는다.
        keep = _referenced_services()
        keep.add(name)
        _free_slots(render_key, keep, want_free=2)
        payload = {"type": "web_service", "name": name, "ownerId": owner_id,
                   "repo": repo_url, "branch": "main", "autoDeploy": "yes",
                   "serviceDetails": {"runtime": "node", "plan": "free", "region": region,
                                      "envSpecificDetails": {"buildCommand": "npm install",
                                                             "startCommand": start_cmd}}}
        st, resp = _http("POST", f"{RENDER_API}/services", render_key, payload)
        if st != 201:
            blob = (json.dumps(resp) + " " + str(st)).lower()   # 한도/요금제로 보이면 고아 더 정리 후 1회 재시도
            if any(w in blob for w in ("limit", "maximum", "quota", "exceed", "free", "plan", "402", "429")):
                if _free_slots(render_key, keep, want_free=3):
                    st, resp = _http("POST", f"{RENDER_API}/services", render_key, payload)
            if st != 201:
                return (f"배포 실패(Render 서비스 생성): HTTP {st} {json.dumps(resp)[:160]} — 이건 산출물 결함이 "
                        "아니라 배포 플랫폼의 용량/네트워크 문제입니다. **다른 플랫폼(Vercel·Railway·Fly 등)은 "
                        "설정돼 있지 않으니 시도하지 마세요 — 구성된 배포 대상은 Render 하나뿐입니다.** 빌드·검증 "
                        "결과는 유효하니 작업을 '실패'로 마감하지 말고 '배포 보류(플랫폼 용량)'로 보고하세요"
                        "(잠시 후 deploy를 다시 부르면 됩니다 — 풀은 자동 정리됩니다).")
        svc = resp.get("service", {})
        sid = svc.get("id")
        dep_id = resp.get("deployId")
        url = svc.get("serviceDetails", {}).get("url", "")

    # 6) '방금 트리거한 배포'가 live 될 때까지 폴링(옛 배포의 live를 거짓 성공으로 읽지 않도록)
    deadline = time.time() + 480   # 빌드 8분까지 동행 — '트리거됨' 비종결 반환(폴링 초대) 최소화
    status = "?"
    while time.time() < deadline:
        if dep_id:
            st, d = _http("GET", f"{RENDER_API}/services/{sid}/deploys/{dep_id}", render_key)
            status = d.get("status", "?") if isinstance(d, dict) else "?"
        else:
            st, deps = _http("GET", f"{RENDER_API}/services/{sid}/deploys?limit=1", render_key)
            status = deps[0]["deploy"]["status"] if isinstance(deps, list) and deps else "?"
        if status == "live":
            served = _check_live(url)          # 라이브 URL이 '실제로 응답'하는지까지 확인
            if served:
                # [완료 = 증명된 완료] 응답(200)만으론 부족하다 — 라이브가 '방금 만든 그 파일'을
                # 서빙하는지까지 바이트 대조로 확인해야 '배포 성공'을 말할 수 있다(스테일 배포가
                # 완료로 보고되던 구멍의 도구 레벨 차단). 불일치면 이 호출 자체가 실패라서,
                # 리더는 구조적으로 이 상태를 '완료'로 보고할 수 없다.
                stale = _verify_live_assets(url, workspace)
                if stale:
                    return (f"배포 실패(스테일 서빙): Render는 live지만 라이브 파일이 산출물과 다릅니다 — "
                            f"{', '.join(stale[:4])}. 옛 빌드가 서빙 중일 수 있습니다 — 캐시 헤더·빌드 "
                            f"로그를 확인하고 다시 배포하세요(이 상태로 '완료' 보고 금지).")
                return (f"배포 성공 ✅ 라이브(HTTP {served} + 산출물 바이트 일치 확인): {url}  "
                        f"(repo: {repo_url})" + _measure_usability(url))
            return f"배포 실패: Render는 live인데 {url} 가 응답하지 않음(서버 기동 실패 가능) — 로그 확인 필요."
        if status in _TERMINAL_FAIL:
            return f"배포 실패(Render {status}) — 빌드 로그 확인 필요. 예정 URL: {url}"
        time.sleep(6)
    return f"배포 트리거됨(빌드 진행 중, status={status}): {url} — 1~2분 후 라이브 예상"
