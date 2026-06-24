"""Organt SNS — 이벤트 스토어 + 투영(projection) 레지스트리.

진실원 = append-only 이벤트 로그(Phase 1: 인메모리, 이후 SQLite). 모든 화면 데이터는 투영에서
파생된다. 투영은 플러그인(Projection 서브클래스) — 새 화면 = 새 투영 등록(코어 불변). 이게 확장성의
구조적 보장: 새 기능이 과거 이벤트를 안 깨고 얹힌다(이벤트 replay로 재생성 가능).
"""
from __future__ import annotations

import collections
import json
import os
from typing import Any, Callable, Optional

from events import Event, KIND_DELEGATION, KIND_CONSULT, KIND_WORK, KIND_GOAL, \
    KIND_VERIFY, KIND_DEPLOY, KIND_COMPLETE, KIND_LEARN, KIND_ALERT, KIND_FLOW_DONE


class Projection:
    """투영 베이스. apply()로 이벤트 흡수, snapshot()으로 현재 read-model 반환."""
    name = "base"

    def apply(self, ev: Event) -> None:  # noqa: D401
        ...

    def snapshot(self) -> Any:
        ...


class FeedProjection(Projection):
    """협업 서사 피드 — 최근 N개 사회적 이벤트(사람이 읽는 흐름)."""
    name = "feed"

    def __init__(self, size: int = 200):
        self.buf: collections.deque[dict] = collections.deque(maxlen=size)

    def apply(self, ev: Event) -> None:
        self.buf.append({"seq": ev.seq, "ts": ev.ts, "kind": ev.kind,
                         "project": ev.project, "actor": ev.actor, "role": ev.role,
                         "target": ev.target, "summary": ev.summary})

    def snapshot(self) -> Any:
        return list(self.buf)


class BatonProjection(Projection):
    """단일 흐름 베턴 — 지금 '활동 중'인 행위자(가장 최근 실작업/위임자)와 최근 위임 엣지."""
    name = "baton"

    def __init__(self):
        self.active_actor: Optional[int] = None
        self.active_role: Optional[str] = None
        self.active_project: Optional[str] = None
        self.last_ts: float = 0.0
        self.recent_edges: collections.deque[dict] = collections.deque(maxlen=12)

    def apply(self, ev: Event) -> None:
        if ev.kind in (KIND_WORK, KIND_DELEGATION, KIND_CONSULT, KIND_GOAL,
                       KIND_VERIFY, KIND_DEPLOY, KIND_COMPLETE) and ev.actor:
            self.active_actor = ev.actor
            self.active_role = ev.role
            self.active_project = ev.project or self.active_project
            self.last_ts = ev.ts
        if ev.kind in (KIND_DELEGATION, KIND_CONSULT) and ev.actor and ev.target:
            self.recent_edges.append({"frm": ev.actor, "frm_role": ev.role,
                                      "to": ev.target, "kind": ev.kind, "ts": ev.ts})
        if ev.kind == KIND_FLOW_DONE:
            self.active_actor = None
            self.active_role = None

    def snapshot(self) -> Any:
        return {"actor": self.active_actor, "role": self.active_role,
                "project": self.active_project, "last_ts": self.last_ts,
                "recent_edges": list(self.recent_edges)}


class ProjectsProjection(Projection):
    """프로젝트 보드 — 이벤트로 본 프로젝트별 활동(이름·상태는 projects.json이 보강)."""
    name = "projects"

    def __init__(self):
        self.by_id: dict[str, dict] = {}

    def apply(self, ev: Event) -> None:
        pid = ev.project
        if not pid:
            return
        p = self.by_id.setdefault(pid, {"id": pid, "events": 0, "last_ts": 0.0,
                                        "last_summary": "", "deploys": 0, "completes": 0})
        p["events"] += 1
        p["last_ts"] = ev.ts
        p["last_summary"] = ev.summary
        if ev.kind == KIND_DEPLOY:
            p["deploys"] += 1
        if ev.kind == KIND_COMPLETE:
            p["completes"] += 1

    def snapshot(self) -> Any:
        return sorted(self.by_id.values(), key=lambda p: p["last_ts"], reverse=True)


class AgentsProjection(Projection):
    """에이전트(봇) — 활동량·직군·성장(증류 횟수). 직무기준은 app이 role_profiles.json에서 보강."""
    name = "agents"

    def __init__(self):
        self.by_actor: dict[int, dict] = {}
        self.by_role_distills: dict[str, int] = collections.defaultdict(int)

    def apply(self, ev: Event) -> None:
        if ev.actor:
            a = self.by_actor.setdefault(ev.actor, {"actor": ev.actor, "role": ev.role,
                                                    "actions": 0, "last_ts": 0.0, "last": ""})
            a["actions"] += 1
            a["last_ts"] = ev.ts
            a["last"] = ev.summary
            if ev.role:
                a["role"] = ev.role
        if ev.kind == KIND_LEARN:
            job = ev.payload.get("job")
            if job:
                self.by_role_distills[job] += 1

    def snapshot(self) -> Any:
        return {"agents": sorted(self.by_actor.values(), key=lambda a: a["actions"], reverse=True),
                "distills_by_role": dict(self.by_role_distills)}


class StatsProjection(Projection):
    """헤더 통계 — 총 이벤트·종류별 카운트·활성 경보."""
    name = "stats"

    def __init__(self):
        self.total = 0
        self.by_kind: dict[str, int] = collections.defaultdict(int)
        self.alerts: list[dict] = []

    def apply(self, ev: Event) -> None:
        self.total += 1
        self.by_kind[ev.kind] += 1
        if ev.kind == KIND_ALERT:
            self.alerts.append({"ts": ev.ts, "project": ev.project, "summary": ev.summary})

    def snapshot(self) -> Any:
        return {"total": self.total, "by_kind": dict(self.by_kind),
                "alerts": self.alerts[-10:]}


class Store:
    """append-only 이벤트 로그 + 투영들. append()가 seq 부여 후 전 투영에 흡수시키고 구독자에 통지."""
    def __init__(self, keep: int = 8000):
        # Phase 1 인메모리 — 메모리 바운드 위해 최근 keep개만 보관(투영은 append 시 흡수되므로
        # 전체 로그가 필요 없다; 이후 SQLite 백킹으로 무제한 보관·replay).
        self.events: collections.deque[Event] = collections.deque(maxlen=keep)
        self._seq = 0
        self.projections: list[Projection] = [
            StatsProjection(), BatonProjection(), ProjectsProjection(),
            AgentsProjection(), FeedProjection(),
        ]
        self._on_event: list[Callable[[Event], None]] = []  # bus 통지 훅

    def subscribe_sink(self, fn: Callable[[Event], None]) -> None:
        self._on_event.append(fn)

    def append(self, ev: Event) -> Event:
        self._seq += 1
        ev.seq = self._seq
        self.events.append(ev)
        for p in self.projections:
            try:
                p.apply(ev)
            except Exception:
                pass  # 한 투영의 버그가 스트림을 막지 않음(격리)
        for fn in self._on_event:
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    def snapshot(self) -> dict[str, Any]:
        return {p.name: p.snapshot() for p in self.projections}
