"""DRF 뷰 — RESTful(F1304). Organt 파생 데이터는 읽기전용(GET), 커뮤니티(쓰레드/댓글/좋아요)는
사용자가 생성(POST) → 적합한 HTTP Method·status code로 응답."""
from django.db.models import Count
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Agent, RoleProfile, Project, Event, Thread, Comment, Like
from .serializers import (
    AgentSerializer, RoleProfileSerializer, EventSerializer,
    ProjectSerializer, ProjectDetailSerializer,
    ThreadSerializer, ThreadDetailSerializer, CommentSerializer,
)


class AgentViewSet(viewsets.ReadOnlyModelViewSet):
    """AI 직원 목록·상세. /api/agents/ , /api/agents/{id}/ , /api/agents/{id}/events/"""
    queryset = Agent.objects.annotate(event_count=Count("events"))
    serializer_class = AgentSerializer
    ordering_fields = ["event_count", "role", "bot_id"]
    ordering = ["-event_count"]

    @action(detail=True)
    def events(self, request, pk=None):
        return Response(EventSerializer(self.get_object().events.all()[:60], many=True).data)


class RoleProfileViewSet(viewsets.ReadOnlyModelViewSet):
    """직군별 증류된 직무기준(에이전트 성장). /api/profiles/"""
    queryset = RoleProfile.objects.all()
    serializer_class = RoleProfileSerializer


class ProjectViewSet(viewsets.ReadOnlyModelViewSet):
    """프로젝트 목록·상세. /api/projects/ , /api/projects/P-032/ , /api/projects/P-032/events/"""
    queryset = Project.objects.annotate(
        event_count=Count("events", distinct=True), task_count=Count("tasks", distinct=True))
    lookup_field = "pid"
    lookup_value_regex = "P-[0-9]+"

    def get_serializer_class(self):
        return ProjectDetailSerializer if self.action == "retrieve" else ProjectSerializer

    @action(detail=True)
    def events(self, request, pid=None):
        return Response(EventSerializer(self.get_object().events.all()[:80], many=True).data)


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


class StatsView(APIView):
    """대시보드 헤더 통계 + 현재 베턴(단일 흐름)."""
    def get(self, request):
        by_kind = dict(Event.objects.values_list("kind")
                       .annotate(n=Count("id")).values_list("kind", "n"))
        last = (Event.objects
                .filter(kind__in=["work", "delegation", "verification", "goal_set", "deploy", "consultation"])
                .select_related("actor", "project").first())
        baton = None
        if last:
            baton = {"actor_id": last.actor.bot_id if last.actor else None,
                     "role": last.actor.role if last.actor else None,
                     "project": last.project.pid if last.project else None,
                     "summary": last.summary, "ts": last.ts}
        return Response({
            "events": Event.objects.count(),
            "agents": Agent.objects.count(),
            "projects": Project.objects.count(),
            "profiles": RoleProfile.objects.count(),
            "threads": Thread.objects.count(),
            "by_kind": by_kind,
            "baton": baton,
        })
