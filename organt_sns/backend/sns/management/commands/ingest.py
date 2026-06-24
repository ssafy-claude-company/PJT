"""Organt 두뇌의 로그·상태 → DB ingest (idempotent — 재실행 가능).

소스(읽기만, 두뇌 무영향):
  logs/jobs.json          봇→직군 맵 → Agent
  logs/projects.json      프로젝트·리더·open_task → Project·CollabTask
  logs/role_profiles.json 증류된 직무기준 → RoleProfile
  logs/flow.jsonl+audit.jsonl  협업 이벤트(정규화) → Event (투영이라 재구축)

이후 Phase: 두뇌 in-process 싱크로 교체하면 무폴링 실시간. 지금은 이 커맨드(또는 watch 루프)로 갱신.
"""
import json
import os

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from sns.normalize import normalize, KIND_LEARN
from sns.models import Agent, RoleProfile, Project, CollabTask, Event


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


class Command(BaseCommand):
    help = "Organt 두뇌 로그·상태를 DB로 ingest (재실행 가능)"

    def add_arguments(self, parser):
        parser.add_argument("--max-bytes", type=int, default=3_000_000,
                            help="이벤트 로그 tail에서 읽을 최대 바이트")
        parser.add_argument("--max-lines", type=int, default=30000)

    def handle(self, *args, **opts):
        logs = settings.ORGANT_LOGS
        self.agents = {}  # bot_id -> Agent (캐시)
        self.sync_agents(logs)
        self.sync_projects(logs)
        self.sync_profiles(logs)
        n = self.ingest_events(logs, opts["max_bytes"], opts["max_lines"])
        self.compute_growth(logs)
        self.stdout.write(self.style.SUCCESS(
            f"✅ ingest 완료: events {n} · agents {Agent.objects.count()} · "
            f"projects {Project.objects.count()} · tasks {CollabTask.objects.count()} · "
            f"profiles {RoleProfile.objects.count()}"))

    # ── Agent ────────────────────────────────────────────────────────
    def agent(self, bot_id, role=None):
        if bot_id is None:
            return None
        try:
            bot_id = int(bot_id)
        except (TypeError, ValueError):
            return None
        a = self.agents.get(bot_id)
        if a is None:
            a, _ = Agent.objects.get_or_create(bot_id=bot_id)
            self.agents[bot_id] = a
        if role and a.role != role:
            a.role = role
            a.save(update_fields=["role"])
        return a

    def sync_agents(self, logs):
        jobs = _load_json(os.path.join(logs, "jobs.json"), {}).get("jobs", {})
        for bid, role in jobs.items():
            self.agent(bid, role)

    # ── Project · Task ──────────────────────────────────────────────
    def sync_projects(self, logs):
        d = _load_json(os.path.join(logs, "projects.json"), {})
        projs = d.get("projects", {}) if isinstance(d, dict) else {}
        leaders = set()
        for _ch, p in projs.items():
            if not isinstance(p, dict) or not p.get("id"):
                continue
            leader = self.agent(p.get("leader"))
            if leader:
                leaders.add(leader.bot_id)
            obj, _ = Project.objects.update_or_create(
                pid=p["id"],
                defaults={"name": (p.get("name") or "")[:200],
                          "leader": leader, "status": str(p.get("status") or "")[:40]})
            ot = p.get("open_task")
            if isinstance(ot, dict) and ot.get("task_id"):
                goal = ot.get("goal") or (ot.get("status") or {}).get("goal") or ""
                CollabTask.objects.update_or_create(
                    project=obj, task_id=str(ot["task_id"])[:40],
                    defaults={"purpose": (ot.get("purpose") or "")[:5000],
                              "goal": str(goal)[:5000],
                              "owner": self.agent(ot.get("owner")),
                              "cross_checks": int(ot.get("cross_checks") or 0),
                              "deploy_count": int(ot.get("deploy_count") or 0),
                              "status": "open"})
        if leaders:
            Agent.objects.filter(bot_id__in=leaders).update(is_leader=True)

    # ── RoleProfile (성장) ──────────────────────────────────────────
    def sync_profiles(self, logs):
        d = _load_json(os.path.join(logs, "role_profiles.json"), {})
        profs = d.get("profiles", {}) or {}
        exps = d.get("experience", {}) or {}
        for role, body in profs.items():
            if not isinstance(role, str):
                continue
            RoleProfile.objects.update_or_create(
                role=role[:60],
                defaults={"criteria": body or "",
                          "experience_count": len(exps.get(role, []) or [])})

    # ── Event (협업 피드, 투영) ─────────────────────────────────────
    def _events_window(self, logs, max_bytes, max_lines):
        out = []
        for name, source in (("flow.jsonl", "flow"), ("audit.jsonl", "audit")):
            path = os.path.join(logs, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            start = max(0, size - max_bytes)
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read()
            lines = data.decode("utf-8", "replace").split("\n")
            if start > 0 and lines:
                lines = lines[1:]
            for ln in [l for l in lines if l.strip()][-max_lines:]:
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                ev = normalize(rec, source)
                if ev is not None:
                    out.append(ev)
        out.sort(key=lambda e: e.ts)
        return out

    @transaction.atomic
    def ingest_events(self, logs, max_bytes, max_lines):
        evs = self._events_window(logs, max_bytes, max_lines)
        Event.objects.all().delete()   # 투영 → 윈도우로 재구축
        pid2proj = {p.pid: p for p in Project.objects.all()}
        rows = []
        for i, e in enumerate(evs, 1):
            rows.append(Event(
                seq=i, ts=e.ts, source=e.source, kind=e.kind,
                project=pid2proj.get(e.project) if e.project else None,
                actor=self.agent(e.actor, e.role),
                target=self.agent(e.target),
                summary=(e.summary or "")[:500], payload=e.payload or {}))
        Event.objects.bulk_create(rows, batch_size=1000)
        return len(rows)

    def compute_growth(self, logs):
        """직군별 누적 증류 횟수 = role_distilled 이벤트 수(전체 flow 스캔 — 832KB라 가볍다)."""
        counts = {}
        try:
            with open(os.path.join(logs, "flow.jsonl"), encoding="utf-8") as f:
                for ln in f:
                    try:
                        rec = json.loads(ln)
                    except Exception:
                        continue
                    if rec.get("event") == "role_distilled":
                        job = rec.get("job")
                        if job:
                            counts[job] = counts.get(job, 0) + 1
        except OSError:
            pass
        for role, n in counts.items():
            RoleProfile.objects.filter(role=role).update(distill_count=n)
