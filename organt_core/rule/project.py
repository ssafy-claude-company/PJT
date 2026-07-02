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


async def create_project(flow, args):
    """[Project Rule 로직] create_project 도구의 규칙 — 전용 채널 생성 + 팀 배정 + 레지스트리 등록.
    @tool 래퍼(guide_tools)가 _ok로 감싸므로 여기선 평문 문자열 반환(rule→guide_tools 순환 회피).
    flow는 duck-typed(guide·guild_id·pool·leader·register_project·project_* 보유)."""
    from .communication import _resolve_members, _uniq
    g = flow.guide
    if flow.project_channel is not None:
        return (f"이미 project_channel={flow.project_channel} (project_id={flow.project_id}) — "
                f"개입 중이면 create_project 말고 바로 작업하세요.")
    # [방어] 봇이 이름 앞에 'P-번호'를 끼워넣으면 떼어낸다 — 식별번호는 시스템(register_project)이
    # 자동 부여하는데, 포트폴리오 목록의 'P-NNN' 표기를 흉내 내 이름에 번호를 박으면 채널 이름이
    # 'P-021 …'이 되고 작업공간 폴더가 'p-021-p-021-…'로 번호가 중복된다. 번호는 시스템 몫이다.
    _raw = str(args.get("name") or "").strip()
    _clean = re.sub(r"^\s*[Pp]-\d+[\s:·\-–—.]*", "", _raw).strip()
    args["name"] = _clean or _raw
    flow.project_channel = await g.create_project_channel(flow.guild_id, args["name"])
    # [작업공간 격리·신원] 폴더는 여기서 깎지 않는다 — 흐름은 시작부터 고유 임시 폴더(new-…)에서
    # 일했고, 아래 register_project가 그 폴더를 **식별번호 이름(p-00n-슬러그)으로 개명**해
    # 신원을 번호로 확정한다(리더 작명 충돌이 폴더·배포 수준에서 무해 — 사용자 제안).
    assigned = _resolve_members(args.get("team", ""), flow, flow.pool)
    if assigned:
        flow.project_team = _uniq([flow.leader] + assigned)
    # 프로젝트는 내부 레지스트리에만 등록(채널 자체가 프로젝트 식별자 — 채널에 앵커 안 박음).
    flow.project_name = args["name"]   # 배포 슬롯 유도용(프로젝트별 결정적 서비스명)
    if flow.register_project:
        flow.project_id = flow.register_project(flow.project_channel, args["name"])
    return (f"project_channel={flow.project_channel} project_id={flow.project_id} "
            f"프로젝트팀={flow._names(flow.project_team)}")
