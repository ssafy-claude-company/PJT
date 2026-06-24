"""SNS API URL — RESTful 리소스 라우팅(F1304). URL이 리소스 구조·관계를 표현."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from . import views
from . import guide_bridge

router = DefaultRouter()
router.register("agents", views.AgentViewSet, basename="agent")
router.register("profiles", views.RoleProfileViewSet, basename="profile")
router.register("projects", views.ProjectViewSet, basename="project")
router.register("events", views.EventViewSet, basename="event")
router.register("threads", views.ThreadViewSet, basename="thread")

urlpatterns = [
    path("stats/", views.StatsView.as_view(), name="stats"),
    path("recommend/", views.RecommendView.as_view(), name="recommend"),
    path("recruit/", views.RecruitView.as_view(), name="recruit"),          # 스튜디오 봇 채용
    path("channels/", views.ChannelCreateView.as_view(), name="channel-create"),  # 프로젝트 생성
    # guide bridge — 두뇌(러너)가 HTTPS로 매체에 말하는 입·출구(Phase 2). Bearer 토큰 필요.
    path("guide/ingest/", guide_bridge.ingest, name="guide-ingest"),
    path("guide/pending/", guide_bridge.pending, name="guide-pending"),
    path("guide/pick/", guide_bridge.pick, name="guide-pick"),
    path("guide/thread/", guide_bridge.thread, name="guide-thread"),
    path("", include(router.urls)),
]
