"""DRF 뷰 — RESTful(F1304). Organt 파생 데이터는 읽기전용(GET), 커뮤니티(쓰레드/댓글/좋아요)는
사용자가 생성(POST) → 적합한 HTTP Method·status code로 응답."""
from django.db.models import Count, OuterRef, Subquery, IntegerField
from django.db.models.functions import Coalesce
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Agent, RoleProfile, Project, Event, Thread, Comment, Like, GuideMessage
from .recommend import score_candidates
from .insights import project_briefing
from .serializers import (
    AgentSerializer, RoleProfileSerializer, EventSerializer,
    ProjectSerializer, ProjectDetailSerializer,
    ThreadSerializer, ThreadDetailSerializer, CommentSerializer,
)


class AgentViewSet(viewsets.ReadOnlyModelViewSet):
    """AI 직원 목록·상세. /api/agents/ , /api/agents/{bot_id}/ , /api/agents/{bot_id}/events/
    공개 식별자 bot_id로 조회(피드·추천이 모두 bot_id로 참조)."""
    # bot_id=0은 system/user 센티넬과 충돌하는 유령 행 — 직원 목록·상세·선택에서 제외.
    queryset = Agent.objects.exclude(bot_id=0).annotate(event_count=Count("events"))
    serializer_class = AgentSerializer
    lookup_field = "bot_id"
    lookup_value_regex = "[0-9]+"
    ordering_fields = ["event_count", "role", "bot_id"]
    ordering = ["-event_count"]

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["profiles"] = {p.role: p.distill_count for p in RoleProfile.objects.all()}  # N+1 회피
        return ctx

    @action(detail=True)
    def events(self, request, bot_id=None):
        return Response(EventSerializer(self.get_object().events.all()[:60], many=True).data)

    @action(detail=True, methods=["patch"])
    def edit(self, request, bot_id=None):
        """봇 편집(관리 기능) — 이름·인격·아바타색·직군 수정. PATCH /api/agents/{bot_id}/edit/"""
        import re as _re
        a = self.get_object()
        for f in ("name", "persona", "avatar", "role"):
            if f not in request.data:
                continue
            v = str(request.data[f])
            if f == "avatar":                       # 아바타는 hex 색 또는 빈값(모노그램) — 그 외는 무시
                a.avatar = v if _re.fullmatch(r"#[0-9a-fA-F]{3,8}", v) else ""
            else:
                setattr(a, f, v[:200] if f == "persona" else v[:60])
        a.save()
        return Response(AgentSerializer(a, context=self.get_serializer_context()).data)


class RoleProfileViewSet(viewsets.ReadOnlyModelViewSet):
    """직군별 증류된 직무기준(에이전트 성장). /api/profiles/"""
    queryset = RoleProfile.objects.all()
    serializer_class = RoleProfileSerializer


class ProjectViewSet(viewsets.ReadOnlyModelViewSet):
    """프로젝트 목록·상세. /api/projects/ , /api/projects/P-032/ , /api/projects/P-032/events/"""
    # GuideMessage는 Project FK가 아니라 channel_id(=proj.id)로 묶인다 — 서브쿼리로 메시지 수 집계.
    _msg_sq = (GuideMessage.objects.filter(channel_id=OuterRef("id")).exclude(msg_type="status")
               .values("channel_id").annotate(c=Count("msg_id")).values("c"))
    queryset = Project.objects.annotate(
        event_count=Count("events", distinct=True), task_count=Count("tasks", distinct=True),
        message_count=Coalesce(Subquery(_msg_sq, output_field=IntegerField()), 0)
    ).order_by("-event_count", "-id")
    lookup_field = "pid"
    lookup_value_regex = "[A-Za-z]+-[0-9]+"   # P-(디스코드)·S-(SnsGuide)·U-(스튜디오) 모두

    def get_serializer_class(self):
        return ProjectDetailSerializer if self.action == "retrieve" else ProjectSerializer

    @action(detail=True)
    def events(self, request, pid=None):
        return Response(EventSerializer(self.get_object().events.all()[:80], many=True).data)

    @action(detail=True)
    def collab(self, request, pid=None):
        """협업 '구조'(Phase 3 시각화) — 베턴·위임 트리·검증 게이트·개입·배포를 이벤트에서 구조화.
        평탄한 메시지 목록(messages)과 달리, '누가 누구에게 위임했고 어디서 검증·개입·배포됐나'를 1급으로."""
        from collections import defaultdict
        proj = self.get_object()
        leader_role = proj.leader.role if proj.leader else None
        roles = {}
        edges = defaultdict(lambda: {"count": 0, "last": "", "ts": 0})
        gates, interventions, deploys, milestones = [], [], [], []

        def rec(r, leader=False):
            if not r:
                return None
            x = roles.setdefault(r, {"role": r, "is_leader": False, "out": 0, "recv": 0, "work": 0, "verify": 0})
            if leader:
                x["is_leader"] = True
            return x

        def verdict(s):
            s = s or ""
            if any(w in s for w in ("통과", "승인", "합격", "pass", "PASS", "✅")):
                return "pass"
            if any(w in s for w in ("반려", "실패", "거부", "미흡", "fail", "FAIL", "❌")):
                return "fail"
            return "open"

        outputs = []
        rec(leader_role, leader=True)
        for e in proj.events.select_related("actor", "target").order_by("seq"):
            ar = e.actor.role if e.actor else None
            tr = e.target.role if e.target else None
            rec(ar); rec(tr)
            if e.kind == "delegation" and ar and tr:
                k = (ar, tr); edges[k]["count"] += 1; edges[k]["last"] = e.summary; edges[k]["ts"] = e.ts
                rec(ar)["out"] += 1; rec(tr)["recv"] += 1
            elif e.kind == "work" and ar:
                rec(ar)["work"] += 1
            elif e.kind in ("verification", "consultation") and ar:
                rec(ar)["verify"] += 1
                gates.append({"role": ar, "target": tr, "summary": e.summary, "ts": e.ts,
                              "outcome": verdict(e.summary), "kind": e.kind})
            elif e.kind == "intervention":
                interventions.append({"role": ar, "summary": e.summary, "ts": e.ts})
            elif e.kind == "deploy":
                deploys.append({"role": ar, "summary": e.summary, "ts": e.ts})
            elif e.kind in ("goal_set", "task_complete"):
                milestones.append({"kind": e.kind, "role": ar, "summary": e.summary, "ts": e.ts})
                if e.kind == "task_complete":   # 산출물 — 마감 보고의 result가 진짜 결과물 요약
                    res = (e.payload or {}).get("result") or e.summary
                    if res:
                        import re
                        links = re.findall(r"https?://[^\s)>\]]+", res)
                        outputs.append({"role": ar, "result": res, "ts": e.ts,
                                        "links": sorted(set(links))[:4]})
        # 라이브/스튜디오 GuideMessage 요청도 위임 엣지로(러너 흐름·스튜디오 요청 가시화)
        ag = {a.bot_id: a for a in Agent.objects.all()}
        for gm in GuideMessage.objects.filter(channel_id=proj.id, msg_type="request"):
            a = ag.get(gm.sender_id); ta = ag.get(gm.to_id) if gm.to_id else None
            ar = (a.role if a else ("요청" if gm.sender_id == 0 else None))
            tr = ta.role if ta else None
            if ar and tr:
                k = (ar, tr); edges[k]["count"] += 1; edges[k]["last"] = gm.body; edges[k]["ts"] = gm.ts
        delegations = [{"from": k[0], "to": k[1], **v}
                       for k, v in sorted(edges.items(), key=lambda x: -x[1]["count"])]
        # 태스크 단위 구조(검증 게이트의 진짜 신호 — cross_checks/deploy는 태스크에 집계됨)
        tasks = [{"task_id": t.task_id, "purpose": t.purpose, "goal": t.goal,
                  "owner_role": t.owner.role if t.owner else None,
                  "cross_checks": t.cross_checks, "deploy_count": t.deploy_count, "status": t.status}
                 for t in proj.tasks.select_related("owner").order_by("task_id")]
        cross_total = sum(t["cross_checks"] for t in tasks)
        return Response({
            "pid": proj.pid, "name": proj.name, "leader_role": leader_role,
            "roles": sorted(roles.values(), key=lambda r: (not r["is_leader"], -(r["out"] + r["work"]))),
            "delegations": delegations, "tasks": tasks, "outputs": outputs[-8:],
            "gates": gates[-40:], "interventions": interventions[-25:],
            "deploys": deploys[-20:], "milestones": milestones[-20:],
            "counts": {"delegations": sum(v["count"] for v in edges.values()),
                       "consult_gates": len(gates), "cross_checks": cross_total,
                       "interventions": len(interventions), "deploys": len(deploys),
                       "tasks": len(tasks)},
        })

    @action(detail=True)
    def briefing(self, request, pid=None):
        """생성형 AI 협업 브리핑(F1302). /api/projects/P-032/briefing/
        AI 키 설정 시 LLM 요약, 미설정 시 규칙기반 폴백(generated=false)."""
        return Response(project_briefing(self.get_object()))

    @action(detail=True)
    def messages(self, request, pid=None):
        """채널(=프로젝트) 메시지 타임라인 — 봇 협업 이벤트 + 사람 코멘트를 시간순 병합."""
        proj = self.get_object()
        try:
            limit = min(int(request.query_params.get("limit") or 160), 400)
        except (TypeError, ValueError):
            limit = 160
        evs = list(proj.events.select_related("actor", "target").order_by("-seq")[:limit])
        evs.reverse()
        msgs = [{
            "type": "agent", "key": f"e{e.seq}", "ts": e.ts, "kind": e.kind,
            "actor_role": e.actor.role if e.actor else None,
            "actor_name": e.actor.name if e.actor else None,
            "actor_id": str(e.actor.bot_id) if e.actor else None,
            "target_role": e.target.role if e.target else None,
            "target_name": e.target.name if e.target else None,
            "summary": e.summary,
        } for e in evs]
        # SNS-네이티브 라이브 메시지(GuideMessage) — 스튜디오 요청 + (러너 단계) SnsGuide 출력
        gms = list(GuideMessage.objects.filter(channel_id=proj.id).exclude(msg_type="status").order_by("msg_id"))
        if gms:
            ag = {a.bot_id: a for a in Agent.objects.exclude(bot_id=0)}   # 유령 bot_id=0 제외
            _km = {"request": "delegation", "response": "work", "plain": "work"}
            for gm in gms:
                if gm.sender_id == 0 and gm.msg_type == "request":
                    msgs.append({"type": "human", "key": f"g{gm.msg_id}", "ts": gm.ts,
                                 "author": "나", "body": gm.body})
                else:
                    a = ag.get(gm.sender_id); ta = ag.get(gm.to_id) if gm.to_id else None
                    kind = "consultation" if (gm.msg_type == "request" and gm.kind == "I") else _km.get(gm.msg_type, "work")
                    msgs.append({"type": "agent", "key": f"g{gm.msg_id}", "ts": gm.ts, "kind": kind,
                                 "actor_role": a.role if a else None,
                                 "actor_name": a.name if a else None,
                                 "actor_id": str(a.bot_id) if a else None,
                                 "target_role": ta.role if ta else None,
                                 "target_name": ta.name if ta else None, "summary": gm.body})
        for thread in proj.threads.all():          # 모든 스레드의 코멘트 병합(첫 스레드만 보던 버그)
            for c in thread.comments.all():
                msgs.append({"type": "human", "key": f"c{c.id}", "ts": c.created_at.timestamp(),
                             "author": c.author_name, "body": c.body})
        msgs.sort(key=lambda m: m["ts"])
        # 미처리 요청 수(러너 꺼짐/대기 가시화) — sender_id=0 요청 중 픽업 안 됐고 응답도 없는 것
        responded = {g.reply_to for g in gms if g.msg_type == "response" and g.reply_to}
        pending = sum(1 for g in gms if g.sender_id == 0 and g.msg_type == "request"
                      and not (g.payload or {}).get("picked") and g.msg_id not in responded)
        return Response({"pid": proj.pid, "name": proj.name, "messages": msgs, "pending_count": pending})

    @action(detail=True, methods=["post"], url_path="request")
    def make_request(self, request, pid=None):
        """채널에 봇 요청(작업/질문)을 맡긴다 — 협업 엔진이 픽업해 처리. 지금은 큐 적재+표시."""
        import time
        proj = self.get_object()
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response({"detail": "내용은 필수입니다."}, status=400)
        kind = "W" if str(request.data.get("kind", "W")).upper().startswith("W") else "I"
        to_id = request.data.get("to_id")
        to_int = None
        if to_id:
            try:
                to_int = int(to_id)
            except (TypeError, ValueError):
                return Response({"detail": "담당 봇 지정이 올바르지 않습니다."}, status=400)
            if not Agent.objects.filter(bot_id=to_int).exists():
                return Response({"detail": "대상 봇을 찾을 수 없습니다."}, status=400)
        m = GuideMessage.objects.create(
            channel_id=proj.id, thread_id=proj.id, sender_id=0, msg_type="request",
            to_id=to_int, kind=kind, body=body[:4000], ts=time.time())
        return Response({"msg_id": m.msg_id, "kind": kind, "queued": True}, status=201)

    @action(detail=True, methods=["post"])
    def say(self, request, pid=None):
        """사람이 채널에 메시지를 남긴다 — F1303 유저 소통(Discord 자체가 커뮤니티)."""
        proj = self.get_object()
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response({"detail": "내용이 비었습니다."}, status=400)
        thread = proj.threads.first() or Thread.objects.create(project=proj, title=f"{proj.pid} 채널")
        c = Comment.objects.create(thread=thread, body=body[:2000],
                                   author_name=(request.data.get("author") or "사람")[:60])
        return Response({"type": "human", "key": f"c{c.id}", "ts": c.created_at.timestamp(),
                         "author": c.author_name, "body": c.body}, status=201)

    @action(detail=True, methods=["patch"])
    def rename(self, request, pid=None):
        """채널 이름 변경(관리 기능). PATCH /api/projects/{pid}/rename/ — 기본 제공(P-) 채널은 보호."""
        proj = self.get_object()
        if proj.pid.startswith("P-"):
            return Response({"detail": "기본 제공(데모) 채널은 변경할 수 없습니다."}, status=403)
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "이름은 필수입니다."}, status=400)
        proj.name = name[:200]
        proj.save(update_fields=["name"])
        return Response({"pid": proj.pid, "name": proj.name})

    @action(detail=True, methods=["post"])
    def archive(self, request, pid=None):
        """채널 보관/복원 토글(status). POST /api/projects/{pid}/archive/ — 기본 제공(P-) 채널은 보호."""
        proj = self.get_object()
        if proj.pid.startswith("P-"):
            return Response({"detail": "기본 제공(데모) 채널은 보관할 수 없습니다."}, status=403)
        proj.status = "" if proj.status == "archived" else "archived"
        proj.save(update_fields=["status"])
        return Response({"pid": proj.pid, "status": proj.status, "archived": proj.status == "archived"})

    @action(detail=True, methods=["delete"])
    def remove(self, request, pid=None):
        """채널 삭제 — 내가 만든 채널(U-/S-)만. 기본 제공(P-) 채널은 보호. DELETE /api/projects/{pid}/remove/"""
        proj = self.get_object()
        if proj.pid.startswith("P-"):
            return Response({"detail": "기본 제공(데모) 채널은 삭제할 수 없습니다."}, status=403)
        GuideMessage.objects.filter(channel_id=proj.id).delete()
        pid_ = proj.pid
        proj.delete()
        return Response({"deleted": pid_}, status=200)


class EventViewSet(viewsets.ReadOnlyModelViewSet):
    """협업 이벤트 피드. /api/events/?kind=delegation&project=P-032"""
    serializer_class = EventSerializer

    def get_queryset(self):
        qs = Event.objects.select_related("project", "actor", "target")
        kind = self.request.query_params.get("kind")
        project = self.request.query_params.get("project")
        if kind:
            qs = qs.filter(kind=kind)
        if project:
            qs = qs.filter(project__pid=project)
        return qs


class ThreadViewSet(viewsets.ModelViewSet):
    """커뮤니티 쓰레드(F1303) — 사용자가 생성/조회, 댓글·좋아요로 소통."""
    queryset = Thread.objects.all()

    def get_serializer_class(self):
        return ThreadDetailSerializer if self.action == "retrieve" else ThreadSerializer

    @action(detail=True, methods=["get", "post"])
    def comments(self, request, pk=None):
        thread = self.get_object()
        if request.method == "GET":
            return Response(CommentSerializer(thread.comments.all(), many=True).data)
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response({"detail": "body는 필수입니다."}, status=400)
        c = Comment.objects.create(
            thread=thread, author_name=(request.data.get("author_name") or "익명")[:60], body=body)
        return Response(CommentSerializer(c).data, status=201)

    @action(detail=True, methods=["post"])
    def like(self, request, pk=None):
        thread = self.get_object()
        user_key = (request.data.get("user_key") or request.META.get("REMOTE_ADDR") or "anon")[:80]
        _obj, created = Like.objects.get_or_create(thread=thread, user_key=user_key)
        return Response({"liked": True, "like_count": thread.likes.count()},
                        status=201 if created else 200)


class RecommendView(APIView):
    """강점 기반 적임자 추천(F1301) — Organt의 '적임자 선발'을 사용자향 추천으로.

    GET /api/recommend/?q=실시간 멀티플레이 서버 동기화&top=5
      q   : 도메인·요구 키워드(자유 텍스트). 비우면 전반 역량 상위 순.
      top : 상위 N명(기본 5, 최대 20).
    응답의 results[].reasons 에 항별 점수 기여도를 담아 추천 근거를 투명하게 노출.
    """
    def get(self, request):
        q = (request.query_params.get("q") or request.query_params.get("query") or "").strip()
        try:
            top = min(max(int(request.query_params.get("top") or 5), 1), 20)
        except (TypeError, ValueError):
            top = 5
        profiles = {p.role: p for p in RoleProfile.objects.all()}
        agents = (Agent.objects.annotate(event_count=Count("events"))
                  .exclude(role="").exclude(role__isnull=True))
        candidates = []
        for a in agents:
            p = profiles.get(a.role)
            candidates.append({
                "bot_id": str(a.bot_id), "name": a.name, "role": a.role,
                "is_leader": a.is_leader, "event_count": a.event_count,
                "distill_count": p.distill_count if p else 0,
                "experience_count": p.experience_count if p else 0,
                "criteria": p.criteria if p else "",
            })
        ranked = score_candidates(q, candidates)
        return Response({
            "query": q,
            "weights": {"role_match": 0.40, "keyword_overlap": 0.30,
                        "expertise": 0.20, "track_record": 0.10},
            "count": len(ranked),
            "results": ranked[:top],
        })


class RecruitView(APIView):
    """스튜디오 — 봇 채용(무한·커스텀). 디스코드 계정 제약이 없으니 클릭 한 번에 생성.
    POST {role, name?, persona?, avatar?}"""
    def post(self, request):
        import time
        import random
        import re as _re
        from django.db import IntegrityError
        role = (request.data.get("role") or "").strip()
        if not role:
            return Response({"detail": "직군(role)은 필수입니다."}, status=400)
        name = (request.data.get("name") or "").strip()
        av = str(request.data.get("avatar") or "")
        avatar = av if _re.fullmatch(r"#[0-9a-fA-F]{3,8}", av) else ""   # hex 색 또는 빈값(모노그램)
        for _ in range(5):                    # bot_id 충돌(같은 ms) 대비 재시도
            bot_id = int(time.time() * 1000) * 1000 + random.randint(0, 999999)
            if not name:                      # 이름은 정체성 — 비우면 고유 이름 자동 배정(직군≠이름)
                from .names import assign_name
                taken = set(n for n in Agent.objects.exclude(name="").values_list("name", flat=True) if n)
                name = assign_name(bot_id, taken)
            try:
                a = Agent.objects.create(
                    bot_id=bot_id, role=role[:60], name=name[:100],
                    persona=(request.data.get("persona") or "")[:5000],
                    avatar=avatar, created_via="sns")
                break
            except IntegrityError:
                continue
        else:
            return Response({"detail": "봇 생성에 실패했습니다. 다시 시도하세요."}, status=500)
        a.event_count = 0
        return Response(AgentSerializer(a).data, status=201)


class ChannelCreateView(APIView):
    """스튜디오 — 프로젝트(채널) 생성. POST {name, leader_bot_id?}"""
    def post(self, request):
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "채널 이름은 필수입니다."}, status=400)
        n = Project.objects.filter(pid__startswith="U-").count() + 1
        pid = f"U-{n:03d}"
        while Project.objects.filter(pid=pid).exists():
            n += 1
            pid = f"U-{n:03d}"
        leader = None
        lb = request.data.get("leader_bot_id")
        if lb:
            try:
                leader = Agent.objects.filter(bot_id=int(lb)).first()
            except (TypeError, ValueError):
                return Response({"detail": "리더 봇 지정이 올바르지 않습니다."}, status=400)
        p = Project.objects.create(pid=pid, name=name[:200], status="live", leader=leader)
        return Response({"pid": p.pid, "name": p.name, "status": p.status,
                         "leader_role": leader.role if leader else None,
                         "event_count": 0, "task_count": 0}, status=201)


class StatsView(APIView):
    """대시보드 헤더 통계 + 현재 베턴(단일 흐름)."""
    def get(self, request):
        by_kind = dict(Event.objects.values_list("kind")
                       .annotate(n=Count("id")).values_list("kind", "n"))
        last = (Event.objects
                .filter(kind__in=["work", "delegation", "verification", "goal_set", "deploy", "consultation"])
                .exclude(project__status="archived")        # 보관된 채널은 베턴에서 제외
                .select_related("actor", "project").first())
        baton = None
        if last:
            baton = {"actor_id": str(last.actor.bot_id) if last.actor else None,
                     "role": last.actor.role if last.actor else None,
                     "name": last.actor.name if last.actor else None,
                     "project": last.project.pid if last.project else None,
                     "project_name": last.project.name if last.project else None,
                     "summary": last.summary, "ts": last.ts}
        return Response({
            "events": Event.objects.count(),
            "agents": Agent.objects.exclude(bot_id=0).count(),
            "projects": Project.objects.count(),
            "profiles": RoleProfile.objects.count(),
            "threads": Thread.objects.count(),
            "by_kind": by_kind,
            "baton": baton,
        })
