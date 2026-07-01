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
# [데모 인프라 보호] 슬롯 확보용 '고아 서비스' 자동삭제가 *절대* 건드리면 안 되는 이름(데모 앱·러너 API 등).
# organt-sns 자체는 채널이 참조하지 않아 '고아'로 오인돼 삭제될 수 있다 — 그러면 데모·러너가 통째로 죽는다.
# env ORGANT_PROTECT_SERVICES(쉼표구분)로 추가 가능. 기본은 organt-sns.
_PROTECT = {s.strip() for s in (os.environ.get("ORGANT_PROTECT_SERVICES") or "organt-sns").split(",") if s.strip()}


def _mask_secret(text, *secrets):
    """에러/로그 문자열에서 자격증명(PAT 등)을 마스킹한다 — 토큰 박힌 remote URL이 에러 메시지로 새는 것
    방지(보안 핫픽스 2026-06)."""
    s = str(text or "")
    for sec in secrets:
        if sec and isinstance(sec, str) and len(sec) >= 8:
            s = s.replace(sec, "***")
    return s


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


def _referenced_services(projects_path=None):
    """등록 레지스트리(logs/projects.json)가 아직 참조하는 onrender 서비스명 집합(keep-set).
    '남아있는 채널이 링크로 가리키는' 서비스 — 풀 정리 시 절대 삭제 금지 대상.
    [보수 폴백(보안·정확성 핫픽스 2026-06)] 파일이 *있는데 못 읽으면*(parse 실패) None을 돌려준다 —
    빈 set으로 오인하면 *참조 중 서비스까지 고아로 보고 삭제할 위험*이 있어, 호출부가 '슬롯 정리 자체를
    건너뛰게' 한다. 파일이 아예 없으면(프로젝트 0) set()(정당한 빈 keep)."""
    p = Path(projects_path) if projects_path else (Path(__file__).resolve().parent.parent / "logs" / "projects.json")
    if not p.exists():
        return set()                                  # 프로젝트 없음 = 정당한 빈 keep
    try:
        return _onrender_subs(p.read_text())
    except Exception:
        return None                                   # 있는데 못 읽음 = '판단 불가' → 호출부가 정리 중단


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


def _billing_suspended(render_key) -> bool:
    """Render 계정의 무료 서비스가 'billing'으로 정지됐는지 — 무료 월 인스턴스시간(750h, 전 무료 서비스
    공유)이 소진되면 Render가 전 서비스를 정지하고 신규 무료 서비스 생성도 막는다(라이브 P-021 관측:
    suspended=12 전원 suspenders=['billing'], 신규 POST /services 실패). 이건 재시도나 슬롯 정리(서비스
    '수')로 풀리지 않는 '비-일시' 차단(월 리셋·유료 전환·서비스 축소가 필요)이라, 무한 재시도 대신
    사용자에게 보고해야 한다."""
    st, data = _http("GET", f"{RENDER_API}/services?limit=100", render_key)
    if st != 200 or not isinstance(data, list) or not data:
        return False
    susp = 0
    for item in data:
        s = item.get("service", item)
        sl = s.get("suspenders") or []
        if s.get("suspended") == "suspended" and any("billing" in str(x).lower() for x in sl):
            susp += 1
    return susp >= max(2, len(data) // 2)   # 절반 이상 billing 정지 = 계정 차원 차단


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
               if s["name"] not in keep and not (_onrender_subs(s["url"]) & keep)
               and s["name"] not in _PROTECT
               and not any(p and p in (s.get("url") or "") for p in _PROTECT)]   # 보호 서비스(데모 앱)는 고아여도 절대 삭제 안 함
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


def _final_deploy_result(url, workspace, repo_url, status,
                         check_live=None, verify=None, measure=None):
    """[배포 보고 정확성 — 라이브 P-020] 폴링 창(480s)이 끝났는데 Render 무료 티어 빌드는 그보다
    길어지기도 한다 — 빌드가 폴 창 뒤 비동기로 완료돼 라이브가 됐는데도 리더가 '미완·수동배포 필요'로
    오보(false negative)하던 문제(라이브: P-020이 멀쩡히 라이브인데 요약은 '배포 미완'). 창이 끝나면
    *한 번 더 길게* 진짜 라이브인지 확인하고(빌드가 방금 끝났을 수 있음), 그래도 아직이면 '실패'가
    아니라 '빌드 진행 중 → 곧 자동 라이브'임을 못박아 리더가 '실패/수동배포'로 오보하지 않게 한다."""
    check_live = check_live or _check_live
    verify = verify or _verify_live_assets
    measure = measure or _measure_usability
    served = check_live(url, tries=10)            # 콜드/지연 빌드 마지막 확인(더 길게 — 방금 끝났을 수 있음)
    if served:
        stale = verify(url, workspace)
        if not stale:
            return (f"배포 성공 ✅ 라이브(HTTP {served} + 산출물 일치): {url}  (repo: {repo_url})" + measure(url))
        return (f"배포 실패(스테일 서빙): 라이브 파일이 산출물과 다릅니다 — {', '.join(stale[:4])}. 옛 빌드가 "
                f"서빙 중일 수 있습니다 — 캐시·빌드 로그 확인 후 다시 배포(이 상태로 '완료' 보고 금지).")
    return (f"배포 진행 중(**실패 아님** — 빌드가 폴링 창보다 길어졌을 뿐): {url} — Render가 빌드를 마치면 "
            f"**자동으로 라이브**됩니다(보통 1~3분 더). **수동 배포하지 마세요**(이미 트리거됨). 1~3분 뒤 이 "
            f"URL을 다시 확인하면 200을 받습니다. 이걸 '실패/미완/수동배포 필요'로 보고하지 말고 **'배포 트리거 "
            f"완료 — 곧 라이브'**로 보고하세요(status={status}).")


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
    # [ground-truth 계정 — GH_USER 설정 드리프트 자가교정(2026-06-30 라이브 P-003)] 리포는 POST /user/repos =
    # *PAT 인증계정* 아래 생긴다. GH_USER 설정이 그 계정과 어긋나면(P-003: GH_USER=byundojin인데 토큰은
    # thisiscount01 → 리포는 thisiscount01에 생기고 push는 byundojin/…로 가 404) push가 통째로 죽는다.
    # 설정값이 아니라 *실제 owner*(201 응답 owner / 422 재사용 시 whoami)를 진실원으로 써 push한다.
    real_user = gh_user
    if st == 201 and isinstance(resp.get("owner"), dict) and resp["owner"].get("login"):
        real_user = resp["owner"]["login"]
    else:
        _who_st, _who = _http("GET", f"{GITHUB_API}/user", gh_pat)
        if _who_st == 200 and _who.get("login"):
            real_user = _who["login"]
    repo_url = f"https://github.com/{real_user}/{name}"

    # 4) push(force — 재배포 시 최신 상태로 덮어씀)
    push_url = f"https://x-access-token:{gh_pat}@github.com/{real_user}/{name}.git"
    rc, out = _git(["push", "-q", "-f", push_url, "main:main"], stage)
    shutil.rmtree(stage, ignore_errors=True)
    if rc != 0:
        _o = _mask_secret(out, gh_pat)
        # [비-일시 분류 — 재시도로 안 풀리는 설정/인증 오류는 즉시 보고(2026-06-30, 사용자: '믿음만 믿고 5번
        # 재시도가 이상'). Repository not found·인증 실패·권한 거부는 *결정적* 오류라 같은 자격증명으로 재시도하면
        # 같은 실패 → cap 5회를 헛되이 태운다. _billing_suspended와 같은 비-일시 처리를 push 오류에도.]
        _low = _o.lower()
        if any(m in _low for m in ("repository not found", "authentication failed", "could not read username",
                                   "invalid username or password", "permission denied", "denied to", "403 forbidden")):
            return ("배포 실패(비-일시 — 재시도 무의미): git push 인증/접근 오류입니다. **재배포하지 마세요** "
                    "— 같은 자격증명이면 결과도 같습니다(코드 문제 아님). 대개 GH_PAT 무효·권한 부족·리포 접근 "
                    "문제이니, complete_task에 '배포 자격증명 점검 필요: <원인>'으로 사용자에게 정직히 보고하세요. "
                    f"상세: {_o[-180:]}")
        return f"배포 실패(git push): {_o[-300:]}"

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
        keep_unknown = keep is None     # projects.json 못 읽음 → keep-set 불명
        if keep_unknown:
            # [보수 폴백] 참조 목록을 못 읽으면 고아 수거를 *건너뛴다*(빈 keep으로 참조 서비스 오삭제 방지).
            # 슬롯이 정말 부족하면 신규 생성이 cap에서 실패하고 그건 아래 비-일시 분기/보고로 잡힌다.
            keep = {name}
        else:
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
                if not keep_unknown and _free_slots(render_key, keep, want_free=3):
                    st, resp = _http("POST", f"{RENDER_API}/services", render_key, payload)
            if st != 201:
                # [비-일시 차단 식별 — 무한 재시도 차단] 계정의 무료 서비스가 'billing'으로 모두 정지된
                # 상태(무료 월 시간 소진)면 재시도·슬롯정리로 안 풀린다. '잠시 후 재시도' 대신 '재시도
                # 무의미·사용자 보고'로 정확히 안내해 13회 헛도는 루프(라이브 P-021)를 끊는다.
                if _billing_suspended(render_key):
                    return ("배포 불가(Render 무료 플랜 billing 정지 — 재시도 무의미): 계정의 무료 서비스가 모두 "
                            "'billing'으로 정지됐습니다. Render 무료 월 인스턴스시간(전 무료 서비스 공유, 750h)이 "
                            "소진된 상태로, **deploy를 다시 불러도·슬롯을 정리해도 풀리지 않습니다**(월 리셋 또는 "
                            "유료 전환·무료 서비스 축소가 필요). **deploy를 반복 호출하지 말고 이 사실을 사용자에게 "
                            "보고**하세요 — 빌드·검증 결과는 유효하니 산출물 결함이 아니라 '배포 보류(플랫폼 billing "
                            "정지)'로 보고하면 됩니다. 기존 배포 링크들도 같은 이유로 현재 모두 정지(503) 상태입니다.")
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
    # 폴링 창이 끝남 — 빌드가 더 길 수 있다(P-020). '실패/미완'으로 오보하지 않게 최종 라이브 확인 후 정확히 보고.
    return _final_deploy_result(url, workspace, repo_url, status)
