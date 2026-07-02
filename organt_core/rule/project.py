"""[Project Rule] 프로젝트(산출물)의 배포 신원·타겟 적합성 규칙 — 원래 설계(REWORK_DESIGN §7
rule/project.py) 복원. 잘못된 구현이 guide_tools에 병합했던 Project 배포 규칙을 되돌린다:
① 배포 서비스명 = 프로젝트 식별번호(P-번호)로 결정적(작명 사고·슬롯 하이재킹 차단),
② 배포 타겟(Render Node) 적합성(런타임에 Python spawn/exec하는 구조 사전 차단).
guide_tools가 re-export해 기존 소비처(sys_core·tests)는 그대로 동작."""
import json
import os
import re


# (그 전에 토큰·빌드 낭비) → *첫 배포 전에* 구조적으로 잡고 명확한 처방을 준다. (빌드타임 학습용 Python은 OK —
# 런타임 spawn/exec와 start 커맨드의 Python 실행만 차단.) 오발 최소: spawn/exec에 python/pip가 *명시*될 때만.
_RUNTIME_PY_RE = re.compile(
    r"(?:spawn|exec|execSync|execFile|fork)\s*\(\s*[`'\"]\s*(?:python|pip|gunicorn|uvicorn|flask|streamlit)",
    re.IGNORECASE)
_PY_START_RE = re.compile(r"\b(?:python3?|pip|gunicorn|uvicorn|flask|streamlit|fastapi|conda)\b", re.IGNORECASE)


def _deploy_infeasibility(workspace) -> str:
    """배포 타겟(Render Node)에서 못 뜨는 구조면 사유 문자열, 아니면 ''. ① package.json start/main이 Python류를
    직접 실행하나 ② .js 서버가 런타임에 python/pip를 spawn/exec 하나. (Python을 빌드타임 학습에만 쓰는 건 통과.)"""
    ws = str(workspace or "")
    if not ws or not os.path.isdir(ws):
        return ""
    pj = os.path.join(ws, "package.json")
    if os.path.isfile(pj):
        try:
            with open(pj, encoding="utf-8") as fh:
                data = json.load(fh)
            start = str((data.get("scripts") or {}).get("start") or "") + " " + str(data.get("main") or "")
            if _PY_START_RE.search(start):
                return f"package.json의 start/main이 Python류를 실행합니다('{start.strip()[:60]}')."
        except Exception:
            pass
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "dist", "build", ".next", "__pycache__")]
        for fn in files:
            if not fn.endswith((".js", ".mjs", ".cjs", ".ts")):
                continue
            try:
                with open(os.path.join(root, fn), encoding="utf-8", errors="ignore") as fh:
                    m = _RUNTIME_PY_RE.search(fh.read())
            except Exception:
                continue
            if m:
                return f"{os.path.relpath(os.path.join(root, fn), ws)}가 런타임에 Python을 spawn/exec 합니다('{m.group(0)[:40]}…')."
    return ""


def deploy_service_name(flow, arg_name: str = "") -> str:
    """배포 서비스명 결정 — [멀티 프로젝트] 등록 프로젝트는 식별번호(P-번호)로 **결정적으로**
    정한다: 같은 프로젝트는 늘 같은 서비스, 다른 프로젝트는 다른 서비스. 미등록 흐름은 슬롯이
    **없다**("") — 배포 신원은 프로젝트가 보증한다(사용자 설계 확인 2026-06-12: 배포는
    프로젝트마다. 과거의 DEPLOY_NAME env 폴백은 미등록 배포를 공유 슬롯(P-002 라이브 겸용
    todo-organt-demo)으로 보내 덮어쓰기 위험을 남겼었다). 에이전트 임의 명명(arg_name)은
    등록·미등록 어디서도 슬롯이 되지 못한다(작명 사고 차단)."""
    pname = getattr(flow, "project_name", None)
    pid = getattr(flow, "project_id", None)
    if pid:
        # [신원=번호] 등록 프로젝트의 슬롯은 무조건 식별번호 — 이름 슬러그를 쓰면 일반명사 이름이
        # 충돌할 때 다른 작품이 같은 슬롯을 덮어쓴다(라이브: 지진·모션이 대기질 슬롯을 연쇄 점유).
        return f"organt-{str(pid).lower()}"
    if pname:
        slug = re.sub(r"[^a-z0-9-]", "-", str(pname).lower()).strip("-")[:40]
        if slug:
            return f"organt-{slug}"
    return ""
