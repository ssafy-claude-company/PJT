"""DRF 뷰 — RESTful(F1304). Organt 파생 데이터는 읽기전용(GET), 커뮤니티(쓰레드/댓글/좋아요)는
사용자가 생성(POST) → 적합한 HTTP Method·status code로 응답."""
from django.db.models import Count, OuterRef, Subquery, IntegerField
from django.db.models.functions import Coalesce
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Agent, RoleProfile, Project, CollabTask, Event, Thread, Comment, Like, GuideMessage
from .recommend import score_candidates
from .insights import project_briefing
from .serializers import (
    AgentSerializer, RoleProfileSerializer, EventSerializer,
    ProjectSerializer, ProjectDetailSerializer,
    ThreadSerializer, ThreadDetailSerializer, CommentSerializer,
)

# Work(작업·위임/구현) vs Info(자문·질문) 자동 분류 — 질문이면 Info, 아니면 Work.
# Rule상 Info=자문(구현금지)·Work=위임(구현)으로 게이트 의미가 다르나, 오분류는 게이트가 흡수(저위험).
_Q_WORDS = ("뭐", "무엇", "무슨", "어떻게", "어떡", "어째", "왜", "언제", "어디", "누가", "누구",
            "얼마", "어느", "어떤", "몇", "가능한가", "가능해", "되나", "될까", "맞나", "맞아")
_Q_ENDINGS = ("나요", "까요", "을까", "ㄹ까", "는가", "은가", "인가", "ㄴ가", "냐", "니", "래", "지요", "죠")


def classify_kind(body):
    """본문으로 W/I 자동 분류. 토글이 명시(W/I)면 그쪽을 쓰고, 'auto'/미지정일 때만 호출."""
    b = (body or "").strip()
    if not b:
        return "W"
    if "?" in b or "？" in b:
        return "I"
    head = b[:24]
    if any(head.startswith(w) or (" " + w) in head for w in _Q_WORDS):
        return "I"
    last = b.rstrip(" .!~…").splitlines()[-1].strip() if b else b
    if any(last.endswith(e) for e in _Q_ENDINGS):
        return "I"
    return "W"


class AgentViewSet(viewsets.ReadOnlyModelViewSet):
    """AI 직원 목록·상세. /api/agents/ , /api/agents/{bot_id}/ , /api/agents/{bot_id}/events/
    공개 식별자 bot_id로 조회(피드·추천이 모두 bot_id로 참조)."""
    # bot_id=0은 system/user 센티넬과 충돌하는 유령 행 — 직원 목록·상세·선택에서 제외.
    serializer_class = AgentSerializer
    lookup_field = "bot_id"
    lookup_value_regex = "[0-9]+"
    ordering_fields = ["event_count", "role", "bot_id"]
    ordering = ["-event_count"]

    def get_queryset(self):
        """공개 직원(쇼케이스+공유) + 내 직원. ?scope=mine|public 로 좁힘."""
        from .social import current_person
        from django.db.models import Q
        cur = current_person(self.request)
        qs = Agent.objects.exclude(bot_id=0).annotate(event_count=Count("events"))
        scope = self.request.query_params.get("scope")
        if scope == "mine":
            return qs.filter(owner=cur) if cur else qs.none()
        if scope == "public":
            return qs.filter(visibility="public")
        # 기본: 공개(쇼케이스+남이 공유한 것) + 내 것(비공개 포함)
        return qs.filter(Q(visibility="public") | Q(owner=cur)) if cur else qs.filter(visibility="public")

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx["profiles"] = {p.role: p.distill_count for p in RoleProfile.objects.all()}  # N+1 회피
        return ctx

    @action(detail=True)
    def events(self, request, bot_id=None):
        # 'denied'(게이트 거부)는 SYS 안전장치가 '리더 독식/권한 밖 도구' 등을 막은 내부 기록 —
        # 직원이 '한 일'이 아니라 '못 하게 막힌 시도'다. 활동 피드엔 제외(거부가 도배돼 보이던 문제).
        return Response(EventSerializer(self.get_object().events.exclude(kind="denied")[:60], many=True).data)

    @action(detail=True, methods=["patch"])
    def edit(self, request, bot_id=None):
        """직원 편집 — 이름·인격·아바타색·직군. 내 직원만(공개 쇼케이스 직원은 읽기전용)."""
        import re as _re
        from .social import current_person
        a = self.get_object()
        cur = current_person(request)
        if a.owner_id is None or not cur or a.owner_id != cur.id:
            return Response({"detail": "내 직원만 편집할 수 있어요."}, status=403)
        for f in ("name", "persona", "avatar", "role"):
            if f not in request.data:
                continue
            v = str(request.data[f])
            if f == "avatar":                       # 아바타는 hex 색 또는 빈값(모노그램) — 그 외는 무시
                a.avatar = v if _re.fullmatch(r"#[0-9a-fA-F]{3,8}", v) else ""
            else:
                setattr(a, f, v[:200] if f == "persona" else v[:60])
        if "model" in request.data:                  # per-agent 모델 — 허용값만(그 외/빈값=러너 전역 기본)
            mv = str(request.data["model"]).strip().lower()
            a.model = mv if mv in ("opus", "sonnet", "haiku") else ""
        a.save()
        return Response(AgentSerializer(a, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def share(self, request, bot_id=None):
        """공개/비공개 전환 — 내 직원만. 공개하면 모두가 보고 쓸 수 있다(공유). POST .../share/"""
        from .social import current_person
        a = self.get_object()
        cur = current_person(request)
        if a.owner_id is None or not cur or a.owner_id != cur.id:
            return Response({"detail": "내 직원만 공개 설정을 바꿀 수 있어요."}, status=403)
        a.visibility = "private" if a.visibility == "public" else "public"
        a.save(update_fields=["visibility"])
        return Response(AgentSerializer(a, context=self.get_serializer_context()).data)


class RoleProfileViewSet(viewsets.ReadOnlyModelViewSet):
    """직군별 증류된 직무기준(에이전트 성장). /api/profiles/"""
    queryset = RoleProfile.objects.all()
    serializer_class = RoleProfileSerializer


class ProjectViewSet(viewsets.ReadOnlyModelViewSet):
    """프로젝트 목록·상세. /api/projects/ , /api/projects/P-032/ , /api/projects/P-032/events/"""
    # 카운트는 '독립 서브쿼리'로 — Count(distinct) 다중 어노테이션은 events×tasks 크로스조인이라
    # 쇼케이스(이벤트 수천)에서 폭증(8s). 서브쿼리는 관계별 독립 집계라 빠르다.
    _ev_sq = (Event.objects.filter(project=OuterRef("pk"))
              .values("project").annotate(c=Count("pk")).values("c"))
    _task_sq = (CollabTask.objects.filter(project=OuterRef("pk"))
                .values("project").annotate(c=Count("pk")).values("c"))
    # GuideMessage는 Project FK가 아니라 channel_id(=proj.id)로 묶인다.
    _msg_sq = (GuideMessage.objects.filter(channel_id=OuterRef("id")).exclude(msg_type="status")
               .values("channel_id").annotate(c=Count("msg_id")).values("c"))
    lookup_field = "pid"
    lookup_value_regex = "[A-Za-z]+-[0-9]+"   # P-(디스코드)·S-(SnsGuide)·U-(스튜디오) 모두

    def get_queryset(self):
        """공개 채널 + 내가 멤버인 채널만. 비공개는 멤버 아니면 안 보임(목록·상세 공통)."""
        from .social import current_person
        from django.db.models import Q
        cur = current_person(self.request)
        qs = Project.objects.annotate(
            event_count=Coalesce(Subquery(self._ev_sq, output_field=IntegerField()), 0),
            task_count=Coalesce(Subquery(self._task_sq, output_field=IntegerField()), 0),
            message_count=Coalesce(Subquery(self._msg_sq, output_field=IntegerField()), 0),
        ).order_by("-event_count", "-id")
        if cur:
            return qs.filter(Q(visibility="public") | Q(members__person=cur, members__status="active")).distinct()
        return qs.filter(visibility="public")

    def get_serializer_class(self):
        return ProjectDetailSerializer if self.action == "retrieve" else ProjectSerializer

    @action(detail=True)
    def events(self, request, pid=None):
        return Response(EventSerializer(self.get_object().events.exclude(kind="denied")[:80], many=True).data)

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
        # 'denied'(게이트 거부)는 내부 안전장치 기록 — 채널 타임라인엔 노이즈라 제외(아래 활동만 표시).
        evs = list(proj.events.exclude(kind="denied").select_related("actor", "target").order_by("-seq")[:limit])
        evs.reverse()
        # 본문은 payload(body/result/goal)에 전체가 있다 — summary는 100자 컷 요약이라 표시엔 전체 본문을 쓴다.
        from .guide_format import to_native, collab_kind, collab_round   # 디스코드 마크업·협업 라벨(회의/표결) → SNS-네이티브

        def _full(e):
            p = e.payload or {}
            return to_native(p.get("body") or p.get("result") or p.get("goal") or e.summary)

        def _marker(e):   # 본문 없는 위임 마커("ID → ID 위임") — 내용 이벤트가 본문을 들고 있어 중복
            s = (e.summary or "").rstrip()
            return e.kind == "delegation" and s.endswith("위임") and ":" not in s
        msgs = [{
            "type": "agent", "key": f"e{e.seq}", "ts": e.ts, "kind": e.kind,
            "actor_role": e.actor.role if e.actor else None,
            "actor_name": e.actor.name if e.actor else None,
            "actor_id": str(e.actor.bot_id) if e.actor else None,
            "target_role": e.target.role if e.target else None,
            "target_name": e.target.name if e.target else None,
            "summary": _full(e),
        } for e in evs if not _marker(e)]
        # SNS-네이티브 라이브 메시지(GuideMessage) — 스튜디오 요청 + (러너 단계) SnsGuide 출력
        gms = list(GuideMessage.objects.filter(channel_id=proj.id).exclude(msg_type="status").order_by("msg_id"))
        live_status = None
        if gms:
            ag = {a.bot_id: a for a in Agent.objects.exclude(bot_id=0)}   # 유령 bot_id=0 제외
            _km = {"request": "delegation", "response": "work", "plain": "work"}
            last_agent_id = None
            for gm in gms:
                # 디스코드식 상태 요약(sender=0·plain "● 작업 중 / ✅ 완료")은 표시·파싱 안 함 —
                # 상태는 아래 '구조화된 처리 상태(payload)'에서 직접 뽑는다(이모지 패턴 의존 X).
                if gm.sender_id == 0 and gm.msg_type == "plain":
                    continue
                if gm.sender_id == 0 and gm.msg_type == "request":
                    msgs.append({"type": "human", "key": f"g{gm.msg_id}", "ts": gm.ts,
                                 "author": (gm.payload or {}).get("requester_name") or "사람",
                                 "body": to_native(gm.body)})
                    p = gm.payload or {}             # 구조화 상태 — picked/done_ts(이모지 아님)
                    if p.get("done_ts"):
                        live_status = {"state": "done", "ts": p["done_ts"], "goal": to_native(gm.body)[:80]}
                    elif p.get("picked"):
                        live_status = {"state": "working", "ts": gm.ts, "goal": to_native(gm.body)[:80]}
                    else:
                        live_status = None           # 대기 — 상태 없음(요청만 표시)
                else:
                    last_agent_id = gm.sender_id
                    a = ag.get(gm.sender_id); ta = ag.get(gm.to_id) if gm.to_id else None
                    # 회의/표결 발언(_say)이면 네이티브 kind로 승격 + 라벨 접두 제거 — 협업이 채널에 보이게
                    native = to_native(gm.body)
                    ck, body = collab_kind(native)
                    kind = ck or ("consultation" if (gm.msg_type == "request" and gm.kind == "I") else _km.get(gm.msg_type, "work"))
                    msgs.append({"type": "agent", "key": f"g{gm.msg_id}", "ts": gm.ts, "kind": kind,
                                 "actor_role": a.role if a else None,
                                 "actor_name": a.name if a else None,
                                 "actor_id": str(a.bot_id) if a else None,
                                 "target_role": ta.role if ta else None,
                                 "target_name": ta.name if ta else None, "summary": body,
                                 "round": collab_round(native) if ck == "meeting" else None})
            if live_status and live_status.get("state") == "working" and last_agent_id:
                a = ag.get(last_agent_id)            # 진행 중이면 최근 활동 직원
                if a:
                    live_status["actor"] = a.name or a.role
        for thread in proj.threads.all():          # 모든 스레드의 코멘트 병합(첫 스레드만 보던 버그)
            for c in thread.comments.all():
                msgs.append({"type": "human", "key": f"c{c.id}", "ts": c.created_at.timestamp(),
                             "author": c.author_name, "body": c.body})
        msgs.sort(key=lambda m: m["ts"])
        # 미처리 요청 수(러너 꺼짐/대기 가시화) — sender_id=0 요청 중 픽업 안 됐고 응답도 없는 것
        responded = {g.reply_to for g in gms if g.msg_type == "response" and g.reply_to}
        pending = sum(1 for g in gms if g.sender_id == 0 and g.msg_type == "request"
                      and not (g.payload or {}).get("picked") and g.msg_id not in responded)
        # 멎은 요청 — 픽됐지만 응답·완료 없이 120초+ 경과(러너가 죽으면 영영 '작업 중'으로 박제).
        import time as _t
        _now = _t.time()
        stuck = sum(1 for g in gms if g.sender_id == 0 and g.msg_type == "request"
                    and (g.payload or {}).get("picked") and not (g.payload or {}).get("done_ts")
                    and g.msg_id not in responded
                    and (_now - ((g.payload or {}).get("picked_ts") or g.ts or 0)) > 120)
        # 프로젝트 한눈에 — 목표·상태·산출물(라이브 링크). 채팅 안 읽어도 맥락 파악.
        import re as _re
        goal = ""
        ge = proj.events.filter(kind="goal_set").order_by("-seq").first()
        if ge:
            goal = (ge.payload or {}).get("goal") or (ge.payload or {}).get("body") or ge.summary
        if not goal:
            t0 = proj.tasks.first()
            goal = (t0.goal or t0.purpose) if t0 else ""
        links = []
        for e in proj.events.filter(kind="task_complete"):
            for u in _re.findall(r"https?://[^\s)>\]]+", (e.payload or {}).get("result") or ""):
                if u not in links:
                    links.append(u)
        done = proj.events.filter(kind="task_complete").exists()
        deploys = proj.events.filter(kind="deploy").count()
        status = "완료" if done else ("진행 중" if msgs else "시작 전")
        context = {"goal": to_native((goal or "").strip())[:400], "status": status,
                   "deploys": deploys, "links": links[:4]}
        from .social import current_person, is_owner, is_member
        cur = current_person(request)
        return Response({"pid": proj.pid, "name": proj.name, "messages": msgs, "pending_count": pending,
                         "stuck_count": stuck,
                         "leader_id": str(proj.leader.bot_id) if proj.leader else None,
                         "leader_role": proj.leader.role if proj.leader else None, "context": context,
                         "visibility": proj.visibility, "owner_handle": proj.owner.handle if proj.owner else None,
                         "is_owner": is_owner(proj, cur), "is_member": is_member(proj, cur),
                         "live_status": live_status})

    @action(detail=True, methods=["post"], url_path="request")
    def make_request(self, request, pid=None):
        """채널에 봇 요청(작업/질문)을 맡긴다 — 협업 엔진이 픽업해 처리. 지금은 큐 적재+표시. 인증 필요."""
        import time
        from .social import current_person
        cur = current_person(request)
        if not cur:
            return Response({"detail": "로그인이 필요해요."}, status=401)
        proj = self.get_object()
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response({"detail": "내용은 필수입니다."}, status=400)
        raw = str(request.data.get("kind", "auto")).upper()
        if raw.startswith("W"):
            kind = "W"
        elif raw.startswith("I"):
            kind = "I"
        else:                                   # 'auto'/미지정 → 본문으로 자동 분류(토글은 override)
            kind = classify_kind(body)
        to_id = request.data.get("to_id")
        to_int = None
        if to_id:
            try:
                to_int = int(to_id)
            except (TypeError, ValueError):
                return Response({"detail": "담당 직원 지정이 올바르지 않습니다."}, status=400)
            if not Agent.objects.filter(bot_id=to_int).exists():
                return Response({"detail": "대상 직원을 찾을 수 없습니다."}, status=400)
        m = GuideMessage.objects.create(
            channel_id=proj.id, thread_id=proj.id, sender_id=0, msg_type="request",
            to_id=to_int, kind=kind, body=body[:4000], ts=time.time(),
            payload={"requester_name": cur.name, "requester_handle": cur.handle})  # 작성자 실명 표시용
        return Response({"msg_id": m.msg_id, "kind": kind, "queued": True}, status=201)

    @action(detail=True, methods=["post"])
    def requeue(self, request, pid=None):
        """멎은 요청 다시 맡기기 — 픽됐지만 응답·완료 없이 멈춘 요청의 picked를 해제해 큐로 되돌린다.
        러너가 죽어 영영 '작업 중'으로 박제된 경우 소유자/멤버가 복구. POST /api/projects/{pid}/requeue/"""
        import time
        from .social import current_person, is_owner, is_member
        cur = current_person(request)
        if not cur:
            return Response({"detail": "로그인이 필요해요."}, status=401)
        proj = self.get_object()
        if not (is_owner(proj, cur) or is_member(proj, cur)):
            return Response({"detail": "이 채널의 멤버만 할 수 있어요."}, status=403)
        gms = list(GuideMessage.objects.filter(channel_id=proj.id))
        responded = {g.reply_to for g in gms if g.msg_type == "response" and g.reply_to}
        now = time.time()
        n = 0
        for g in gms:                                  # 픽됐지만 무응답·미완·120초+ 경과 = 멎음
            p = g.payload or {}
            if (g.sender_id == 0 and g.msg_type == "request" and p.get("picked")
                    and not p.get("done_ts") and g.msg_id not in responded
                    and (now - (p.get("picked_ts") or g.ts or 0)) > 120):
                np = dict(p)
                np.pop("picked", None); np.pop("done_ts", None); np.pop("picked_ts", None)
                GuideMessage.objects.filter(msg_id=g.msg_id).update(payload=np)
                n += 1
        return Response({"requeued": n})

    @action(detail=True, methods=["post"])
    def stop(self, request, pid=None):
        """작업 중지 — 이 채널에서 진행 중인 협업 흐름을 멈추도록 러너에 신호(소유자/멤버).
        러너가 폴해 Sys.request_cancel(channel)을 부르면 진행 턴이 협조적으로 취소된다.
        POST /api/projects/{pid}/stop/"""
        import time
        from .social import current_person, is_owner, is_member
        from .models import StopSignal
        cur = current_person(request)
        if not cur:
            return Response({"detail": "로그인이 필요해요."}, status=401)
        proj = self.get_object()
        if not (is_owner(proj, cur) or is_member(proj, cur)):
            return Response({"detail": "이 채널의 멤버만 할 수 있어요."}, status=403)
        StopSignal.objects.update_or_create(
            channel_id=proj.id, defaults={"requested_at": time.time(), "requested_by": cur.handle[:30]})
        return Response({"ok": True})

    @action(detail=True, methods=["post"])
    def say(self, request, pid=None):
        """사람이 채널에 메시지를 남긴다 — F1303 유저 소통(Discord 자체가 커뮤니티). 인증 필요."""
        from .social import current_person
        cur = current_person(request)
        if not cur:
            return Response({"detail": "로그인이 필요해요."}, status=401)
        proj = self.get_object()
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response({"detail": "내용이 비었습니다."}, status=400)
        thread = proj.threads.first() or Thread.objects.create(project=proj, title=f"{proj.pid} 채널")
        c = Comment.objects.create(thread=thread, body=body[:2000], author_name=cur.name[:60])
        return Response({"type": "human", "key": f"c{c.id}", "ts": c.created_at.timestamp(),
                         "author": c.author_name, "body": c.body}, status=201)

    def _owner_or_403(self, proj, request):
        """채널 소유자만 관리(이름·보관·삭제·공개전환). 공개 쇼케이스(owner=null)는 누구도 못 건드림."""
        from .social import current_person, is_owner
        if not is_owner(proj, current_person(request)):
            return Response({"detail": "채널 소유자만 할 수 있어요."}, status=403)
        return None

    @action(detail=True, methods=["patch"])
    def rename(self, request, pid=None):
        """채널 이름 변경 — 소유자만. PATCH /api/projects/{pid}/rename/"""
        proj = self.get_object()
        err = self._owner_or_403(proj, request)
        if err:
            return err
        name = (request.data.get("name") or "").strip()
        if not name:
            return Response({"detail": "이름은 필수입니다."}, status=400)
        proj.name = name[:200]
        proj.save(update_fields=["name"])
        return Response({"pid": proj.pid, "name": proj.name})

    @action(detail=True, methods=["post"])
    def archive(self, request, pid=None):
        """채널 보관/복원 토글 — 소유자만. POST /api/projects/{pid}/archive/"""
        proj = self.get_object()
        err = self._owner_or_403(proj, request)
        if err:
            return err
        proj.status = "" if proj.status == "archived" else "archived"
        proj.save(update_fields=["status"])
        return Response({"pid": proj.pid, "status": proj.status, "archived": proj.status == "archived"})

    @action(detail=True, methods=["post"])
    def visibility(self, request, pid=None):
        """공개/비공개 전환 — 소유자만. POST /api/projects/{pid}/visibility/"""
        proj = self.get_object()
        err = self._owner_or_403(proj, request)
        if err:
            return err
        proj.visibility = "private" if proj.visibility == "public" else "public"
        proj.save(update_fields=["visibility"])
        return Response({"pid": proj.pid, "visibility": proj.visibility})

    @action(detail=True, methods=["delete"])
    def remove(self, request, pid=None):
        """채널 삭제 — 소유자만. DELETE /api/projects/{pid}/remove/"""
        proj = self.get_object()
        err = self._owner_or_403(proj, request)
        if err:
            return err
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
        from .social import current_person
        from django.db.models import Q
        cur = current_person(request)
        profiles = {p.role: p for p in RoleProfile.objects.all()}
        agents = (Agent.objects.exclude(bot_id=0).annotate(event_count=Count("events"))
                  .exclude(role="").exclude(role__isnull=True))
        # 공개 직원(쇼케이스+공유) + 내 직원만 추천(남의 비공개 직원 제외)
        agents = agents.filter(Q(visibility="public") | Q(owner=cur)) if cur else agents.filter(visibility="public")
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
        from .social import current_person
        person = current_person(request)       # 채용한 직원은 '나만의 직원'(owner=me)
        if not person:
            return Response({"detail": "로그인이 필요해요."}, status=401)
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
                    avatar=avatar, created_via="sns", owner=person, visibility="private")
                break
            except IntegrityError:
                continue
        else:
            return Response({"detail": "직원 생성에 실패했습니다. 다시 시도하세요."}, status=500)
        a.event_count = 0
        return Response(AgentSerializer(a).data, status=201)


class ChannelCreateView(APIView):
    """스튜디오 — 프로젝트(채널) 생성. POST {name, leader_bot_id?}. 인증 필요(만든 사람=리드)."""
    def post(self, request):
        from .social import current_person
        person = current_person(request)
        if not person:
            return Response({"detail": "로그인이 필요해요."}, status=401)
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
                return Response({"detail": "리더 직원 지정이 올바르지 않습니다."}, status=400)
        # 공개/비공개 — 기본 비공개('나만의 채널'). 체험 계정은 공개 불가(둘러보기 오염 방지).
        vis = "public" if request.data.get("visibility") == "public" else "private"
        if person.is_guest:
            vis = "private"
        p = Project.objects.create(pid=pid, name=name[:200], status="live", leader=leader,
                                   owner=person, visibility=vis)
        from .models import Membership                # 만든 사람을 채널 리드 멤버로
        Membership.objects.get_or_create(person=person, project=p, defaults={"role": "lead"})
        return Response({"pid": p.pid, "name": p.name, "status": p.status,
                         "leader_role": leader.role if leader else None, "visibility": p.visibility,
                         "owner_handle": person.handle, "event_count": 0, "task_count": 0}, status=201)


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
        # 직원·채널 수는 '내가 볼 수 있는 것'으로(공개 + 내 것) — 남의 비공개 수 비노출.
        from .social import current_person
        from django.db.models import Q
        from .models import Friendship, Membership
        cur = current_person(request)
        pending = {
            "friend_requests": Friendship.objects.filter(b=cur, status="pending").count() if cur else 0,
            "invites": Membership.objects.filter(person=cur, status="invited").count() if cur else 0,
        }
        agent_q = Agent.objects.exclude(bot_id=0)
        proj_q = Project.objects.all()
        if cur:
            agent_q = agent_q.filter(Q(owner__isnull=True) | Q(owner=cur))
            proj_q = proj_q.filter(Q(visibility="public") | Q(members__person=cur, members__status="active")).distinct()
        else:
            agent_q = agent_q.filter(owner__isnull=True)
            proj_q = proj_q.filter(visibility="public")
        # 협업 엔진(러너) 생존 — heartbeat 최근 30초 내면 가동 중(정적 안내문 대신 실제 표시).
        from .models import EngineHeartbeat
        import time as _t
        hb = EngineHeartbeat.objects.first()
        engine = {"live": bool(hb and (_t.time() - hb.last_beat) < 30), "last": hb.last_beat if hb else 0}
        return Response({
            "events": Event.objects.count(),
            "agents": agent_q.count(),
            "projects": proj_q.count(),
            "profiles": RoleProfile.objects.count(),
            "threads": Thread.objects.count(),
            "by_kind": by_kind,
            "baton": baton,
            "pending": pending,
            "engine": engine,
        })
