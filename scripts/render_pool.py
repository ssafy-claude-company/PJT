#!/usr/bin/env python3
"""Render 무료 티어 서비스 풀 관리 — 등록 프로젝트가 '아직 참조하는 링크'만 남기고 나머지(고아
서비스: 옛 테스트 배포·삭제된 채널·이름 불일치)를 정리해 25개 한도 슬롯을 되찾는다.

근거: 무료(Hobby) 티어는 서비스 개수 상한이 있어 풀이 차면 신규 배포가 '구조적으로' 막힌다
(라이브 관측 P-019: '한도 초과 → 사용자 보고로 마감'). keep-set = logs/projects.json 안에서
아직 참조되는 *.onrender.com 서비스명. 그 외는 삭제 후보.

사용:
  python scripts/render_pool.py --list     # 드라이런(분류만 출력, 삭제 없음)
  python scripts/render_pool.py --prune     # keep-set 외 고아 서비스 실제 삭제

자격증명(RENDER_KEY)은 .env에서 읽고 절대 출력하지 않는다.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
PROJECTS = ROOT / "logs" / "projects.json"
RENDER_API = "https://api.render.com/v1"


def _load_key() -> str:
    """RENDER_KEY를 환경 또는 .env에서 읽는다(값은 절대 출력 안 함)."""
    k = os.environ.get("RENDER_KEY")
    if k:
        return k
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("RENDER_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _http(method, url, token, retries=5):
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                return r.status, (json.loads(body) if body else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or "{}")
            except Exception:
                return e.code, {}
        except Exception as e:                       # 네트워크/DNS 실패(egress 프록시 간헐) — 재시도
            last = str(e)
            time.sleep(2 * (attempt + 1))
    return 0, {"error": last}


def _keep_names() -> set:
    """등록 레지스트리에서 아직 참조되는 *.onrender.com 서비스명 집합."""
    if not PROJECTS.exists():
        return set()
    txt = PROJECTS.read_text()
    return set(re.findall(r"https?://([a-z0-9][a-z0-9-]*)\.onrender\.com", txt))


def _list_services(token):
    """모든 서비스 나열(커서 페이지네이션). → [{id,name,url,created,suspended}]."""
    out, cursor = [], None
    for _ in range(20):
        url = f"{RENDER_API}/services?limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        st, data = _http("GET", url, token)
        if st != 200 or not isinstance(data, list):
            print(f"[오류] 서비스 목록 조회 실패: HTTP {st} {str(data)[:160]}")
            sys.exit(2)
        if not data:
            break
        for item in data:
            s = item.get("service", item)
            out.append({
                "id": s.get("id"),
                "name": s.get("name"),
                "url": (s.get("serviceDetails") or {}).get("url", ""),
                "created": s.get("createdAt", ""),
                "suspended": s.get("suspended", ""),
            })
            cursor = item.get("cursor")
        if len(data) < 100:
            break
    return out


def _classify(svcs, keep):
    """서비스명 또는 URL 서브도메인이 keep-set에 있으면 보존, 아니면 삭제 후보."""
    keep_list, drop_list = [], []
    for s in svcs:
        sub = ""
        m = re.search(r"https?://([a-z0-9][a-z0-9-]*)\.onrender\.com", s["url"] or "")
        if m:
            sub = m.group(1)
        referenced = (s["name"] in keep) or (sub in keep)
        (keep_list if referenced else drop_list).append(s)
    return keep_list, drop_list


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--list"
    token = _load_key()
    if not token:
        print("[중단] RENDER_KEY를 환경/.env에서 찾지 못했습니다.")
        sys.exit(1)
    keep = _keep_names()
    svcs = _list_services(token)
    keep_list, drop_list = _classify(svcs, keep)

    print(f"=== Render 서비스 풀: 총 {len(svcs)}개 (한도 25) ===")
    print(f"등록 레지스트리 참조 링크 {len(keep)}종: {', '.join(sorted(keep))}\n")
    print(f"[보존 {len(keep_list)}개 — 남아있는 채널이 참조]")
    for s in sorted(keep_list, key=lambda x: x["name"] or ""):
        print(f"  KEEP  {s['name']:<34} {s['url']}")
    print(f"\n[삭제 후보 {len(drop_list)}개 — 참조 없는 고아]")
    for s in sorted(drop_list, key=lambda x: x["name"] or ""):
        print(f"  DROP  {s['name']:<34} {s['url'] or '(URL 없음)'}  created={s['created'][:10]}")

    if mode != "--prune":
        print(f"\n드라이런(--list). 실제 삭제하려면: python {sys.argv[0]} --prune")
        return

    print(f"\n=== --prune: 고아 {len(drop_list)}개 삭제 시작 ===")
    ok = 0
    for s in drop_list:
        if not s["id"]:
            continue
        st, resp = _http("DELETE", f"{RENDER_API}/services/{s['id']}", token)
        if st in (200, 204):
            ok += 1
            print(f"  삭제됨  {s['name']}")
        else:
            print(f"  실패    {s['name']}: HTTP {st} {str(resp)[:120]}")
        time.sleep(0.5)
    print(f"\n완료: {ok}/{len(drop_list)} 삭제. 남은 서비스 {len(svcs) - ok}개 → 신규 슬롯 {25 - (len(svcs) - ok)}개 확보.")


if __name__ == "__main__":
    main()
