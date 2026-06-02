"""산출물 공개 배포 — GitHub repo push + Render 웹서비스 생성/갱신.

Guide의 `deploy` 리더 툴이 호출한다. 자격증명은 환경변수로 주입한다(코드/로그에 박지 않음):
  GH_PAT, GH_USER, RENDER_KEY, RENDER_OWNER
Node 앱(서버가 process.env.PORT 사용)만 지원한다. 같은 name으로 다시 부르면 갱신 배포한다.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

GITHUB_API = "https://api.github.com"
RENDER_API = "https://api.render.com/v1"
_TERMINAL_FAIL = ("build_failed", "update_failed", "canceled", "deactivated", "pre_deploy_failed")


def _http(method, url, token, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def _git(args, cwd):
    cmd = ["git", "-c", "commit.gpgsign=false", "-c", "user.email=deploy@organt.local",
           "-c", "user.name=Organt Deploy", *args]
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


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
    if sid:
        _http("POST", f"{RENDER_API}/services/{sid}/deploys", render_key, {})
    else:
        payload = {"type": "web_service", "name": name, "ownerId": owner_id,
                   "repo": repo_url, "branch": "main", "autoDeploy": "yes",
                   "serviceDetails": {"runtime": "node", "plan": "free", "region": region,
                                      "envSpecificDetails": {"buildCommand": "npm install",
                                                             "startCommand": start_cmd}}}
        st, resp = _http("POST", f"{RENDER_API}/services", render_key, payload)
        if st != 201:
            return f"배포 실패(Render 서비스 생성): HTTP {st} {json.dumps(resp)[:200]}"
        svc = resp.get("service", {})
        sid = svc.get("id")
        url = svc.get("serviceDetails", {}).get("url", "")

    # 6) 배포가 라이브 될 때까지 폴링
    deadline = time.time() + 220
    status = "?"
    while time.time() < deadline:
        st, deps = _http("GET", f"{RENDER_API}/services/{sid}/deploys?limit=1", render_key)
        if isinstance(deps, list) and deps:
            status = deps[0]["deploy"]["status"]
            if status == "live":
                return f"배포 성공 ✅ 라이브: {url}  (repo: {repo_url})"
            if status in _TERMINAL_FAIL:
                return f"배포 실패(Render {status}) — 빌드 로그 확인 필요. 예정 URL: {url}"
        time.sleep(6)
    return f"배포 트리거됨(빌드 진행 중, status={status}): {url} — 1~2분 후 라이브 예상"
