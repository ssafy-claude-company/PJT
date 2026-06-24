"""Organt SNS — 이벤트 스키마 + 정규화.

두뇌(Organt)는 이미 logs/flow.jsonl + logs/audit.jsonl 에 append-only 이벤트를 뱉는다.
여기서 그 *원시* 레코드를 '사회적 의미'를 가진 정규화 Event로 변환한다 — 즉 raw tool_use를
'위임/협의/검증/완성/학습' 같은 협업 서사로 번역하는 레이어. SNS가 보여주는 모든 건 이 Event의
투영(projection)이다. 확장: 새 종류는 EVENT_KINDS에 추가만 하면 됨(스키마 불변).
"""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator


# 직군 라벨(봇 id → 역할). 두뇌의 jobs.json/Discord 역할이 진실원이지만, 표시용 캐시.
# 런타임에 audit의 role 필드로 대부분 채워지므로 여기선 폴백만.
def role_of(actor: Optional[int], role: Optional[str]) -> Optional[str]:
    return role or (str(actor) if actor is not None else None)


# 사회적 이벤트 종류 — 협업 서사의 1급 단위(피드·투영이 이걸로 분기)
KIND_DELEGATION = "delegation"        # request(Work): 베턴을 동료에게 넘김
KIND_CONSULT = "consultation"         # request(Info): 동료에게 자문
KIND_WORK = "work"                    # 실작업(run/Write/Edit/Read/Grep…)
KIND_GOAL = "goal_set"                # set_goal: 목표 합의 확정
KIND_MEETING = "meeting"              # meet/vote: 회의·표결
KIND_VERIFY = "verification"          # 교차검증(QA 등)
KIND_DEPLOY = "deploy"                # 배포
KIND_COMPLETE = "task_complete"       # complete_task: 마감
KIND_RECRUIT = "recruit"              # 충원/직군 부여
KIND_LEARN = "agent_learned"          # 수면 증류로 직무기준 갱신
KIND_EXPERIENCE = "experience_saved"  # 원석 경험 적재
KIND_ALERT = "convergence_alert"      # 수렴 경보(회로차단기)
KIND_USER_REQUEST = "user_request"    # 사용자 요청 도착
KIND_INTERVENTION = "intervention"    # 사용자 개입(이어가기/수정)
KIND_QUEUED = "queued"                # 대기열 적재
KIND_FLOW_DONE = "flow_complete"      # 흐름 종료
KIND_RECOVERY = "recovery"            # 부팅 복구
KIND_DENIED = "denied"                # 도구 거부(게이트 발동)
KIND_RAW = "raw"                      # 미분류(원본 보존)


class Event(BaseModel):
    """정규화된 협업 이벤트(투영의 원재료). append-only·불변."""
    seq: int = 0                       # 단조 증가 시퀀스(ingest가 부여)
    ts: float                          # epoch seconds
    source: str                        # "flow" | "audit"
    kind: str                          # KIND_* (사회적 의미)
    project: Optional[str] = None      # P-번호
    actor: Optional[int] = None        # 행위자 봇 id
    role: Optional[str] = None         # 행위자 직군
    target: Optional[int] = None       # 대상(위임/자문 받는 동료)
    summary: str = ""                  # 사람이 읽는 한 줄
    payload: dict[str, Any] = Field(default_factory=dict)  # 원본·부가 데이터

    # [강건성] 두뇌 로그엔 구버전·잡 데이터가 섞인다(예: project=true(bool), 비정수 id). 이벤트
    # 소싱 무결성을 위해 드롭하지 않고 *coerce* 한다 — 스트림이 한 줄 때문에 멈추면 안 된다.
    @field_validator("project", "role", mode="before")
    @classmethod
    def _v_str_or_none(cls, v):
        return v if isinstance(v, str) else None

    @field_validator("actor", "target", mode="before")
    @classmethod
    def _v_int_or_none(cls, v):
        if v is None or isinstance(v, bool):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    @field_validator("summary", mode="before")
    @classmethod
    def _v_summary(cls, v):
        return "" if v is None else str(v)


_GUIDE = "mcp__guide__"
_WORK_TOOLS = {"run", "Write", "Edit", "Read", "Grep", "Glob", "Bash",
               "WebSearch", "WebFetch", "ToolSearch", "MultiEdit"}


def _clip(s: Any, n: int = 90) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def normalize(rec: dict[str, Any], source: str) -> Optional[Event]:
    """원시 flow/audit 레코드 → Event. 의미 없는 노이즈는 None(드롭)."""
    ts = float(rec.get("ts") or 0.0)
    if source == "audit":
        return _normalize_audit(rec, ts)
    return _normalize_flow(rec, ts)


def _normalize_audit(rec: dict, ts: float) -> Optional[Event]:
    ev = rec.get("event")
    actor = rec.get("actor")
    role = rec.get("role")
    if ev == "user_request":
        return Event(ts=ts, source="audit", kind=KIND_USER_REQUEST,
                     target=rec.get("to"), summary=_clip(rec.get("body"), 120),
                     payload={"to": rec.get("to")})
    if ev == "tool_denied":
        ti = rec.get("tool_input") or {}
        return Event(ts=ts, source="audit", kind=KIND_DENIED, actor=actor, role=role,
                     summary=f"{role or actor}: {rec.get('tool')} 거부({_clip(rec.get('reason'), 40)})",
                     payload={"tool": rec.get("tool"), "reason": rec.get("reason")})
    if ev != "tool_use":
        return None
    tool = rec.get("tool") or ""
    ti = rec.get("tool_input") or {}
    short = tool.replace(_GUIDE, "")
    # 위임/자문
    if tool == _GUIDE + "request":
        kind_w = str(ti.get("kind", "")).strip().lower().startswith("w")
        to = ti.get("to_id") or ti.get("to")
        try:
            to = int(to) if to is not None else None
        except (TypeError, ValueError):
            to = None
        body = _clip(ti.get("body"), 100)
        if kind_w:
            return Event(ts=ts, source="audit", kind=KIND_DELEGATION, actor=actor, role=role,
                         target=to, summary=f"{role or actor} → {to}: {body}", payload={"body": ti.get("body")})
        return Event(ts=ts, source="audit", kind=KIND_CONSULT, actor=actor, role=role,
                     target=to, summary=f"{role or actor} ?→ {to}: {body}", payload={"body": ti.get("body")})
    if tool in (_GUIDE + "set_goal",):
        return Event(ts=ts, source="audit", kind=KIND_GOAL, actor=actor, role=role,
                     summary=f"{role or actor}: 목표 확정 — {_clip(ti.get('goal'), 80)}", payload=dict(ti))
    if tool in (_GUIDE + "meet", _GUIDE + "vote"):
        return Event(ts=ts, source="audit", kind=KIND_MEETING, actor=actor, role=role,
                     summary=f"{role or actor}: {short}", payload=dict(ti))
    if tool == _GUIDE + "complete_task":
        return Event(ts=ts, source="audit", kind=KIND_COMPLETE, actor=actor, role=role,
                     summary=f"{role or actor}: Task 마감", payload=dict(ti))
    if tool == _GUIDE + "deploy":
        return Event(ts=ts, source="audit", kind=KIND_DEPLOY, actor=actor, role=role,
                     summary=f"{role or actor}: 배포", payload=dict(ti))
    if tool == _GUIDE + "recruit":
        return Event(ts=ts, source="audit", kind=KIND_RECRUIT, actor=actor, role=role,
                     summary=f"{role or actor}: 충원 — {_clip(ti.get('role'), 40)}", payload=dict(ti))
    if short in _WORK_TOOLS:
        # 실작업 — 무엇을 하는지 한 줄(파일/명령)
        detail = ti.get("file_path") or ti.get("command") or ti.get("query") or ti.get("pattern") or ""
        return Event(ts=ts, source="audit", kind=KIND_WORK, actor=actor, role=role,
                     summary=f"{role or actor}: {short} {_clip(detail, 60)}",
                     payload={"tool": short, "detail": _clip(detail, 200)})
    # 기타 guide 도구(run 등)는 작업으로
    return Event(ts=ts, source="audit", kind=KIND_WORK, actor=actor, role=role,
                 summary=f"{role or actor}: {short}", payload={"tool": short})


def _normalize_flow(rec: dict, ts: float) -> Optional[Event]:
    ev = rec.get("event") or ""
    proj = rec.get("project")
    extra = {k: v for k, v in rec.items() if k not in ("event", "ts", "project")}
    m = {
        "role_distilled": (KIND_LEARN, f"{rec.get('job')} 직무기준 증류(경험 {rec.get('used')}건 압축)"),
        "role_profile_saved": (KIND_LEARN, f"{rec.get('job')} 직무기준 갱신({rec.get('size')}자)"),
        "role_experience_saved": (KIND_EXPERIENCE, f"{rec.get('job')} 경험 적재"),
        "loop_circuit_breaker": (KIND_ALERT, f"수렴 경보 — 교차검증 {rec.get('cross')}회 미수렴"),
        "intervention": (KIND_INTERVENTION, f"사용자 개입 — {_clip(rec.get('text'), 80)}"),
        "queued": (KIND_QUEUED, f"대기열 적재 — {_clip(rec.get('text'), 60)}"),
        "flow_done": (KIND_FLOW_DONE, "흐름 종료"),
        "open_task_restored": (KIND_RECOVERY, f"미완 Task 복원({rec.get('task')})"),
        "set_goal_standard_set": (KIND_GOAL, f"목표 기준 확정({rec.get('chars')}자)"),
        "set_goal_consensus_coverage": (KIND_MEETING, "목표 합의 — 의견 수렴"),
        "req_sent": (KIND_DELEGATION, f"{rec.get('frm')} → {rec.get('to')} 위임"),
        "continue_incomplete": (KIND_WORK, f"이어가기(seg {rec.get('seg')}, 진행={rec.get('progressed')})"),
    }
    if ev in m:
        kind, summary = m[ev]
        return Event(ts=ts, source="flow", kind=kind, project=proj,
                     actor=rec.get("frm") or rec.get("owner"),
                     target=rec.get("to"), summary=summary, payload=extra)
    # 미분류 flow 이벤트는 보존(원본) — 드롭하지 않음(이벤트 소싱 무결성)
    return Event(ts=ts, source="flow", kind=KIND_RAW, project=proj,
                 summary=f"{ev} {_clip(extra, 60)}", payload={"event": ev, **extra})
