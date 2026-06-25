"""SNS API URL — RESTful 리소스 라우팅(F1304). URL이 리소스 구조·관계를 표현."""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from . import views
from . import guide_bridge
from . import social

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
    # 인증(회원가입/로그인) — 핸들+비밀번호, 토큰 발급
    path("auth/register/", social.register, name="register"),
    path("auth/login/", social.login, name="login"),
    path("auth/guest/", social.guest, name="guest"),
    path("auth/logout/", social.logout, name="logout"),
    # 소셜(멀티유저) — 정체성·친구·채널 멤버·워크스페이스
    path("me/", social.me, name="me"),
    path("people/", social.people, name="people"),
    path("friends/", social.friends, name="friends"),
    path("friends/requests/", social.friend_requests, name="friend-requests"),
    path("friends/requests/<str:handle>/accept/", social.accept_friend, name="accept-friend"),
    path("friends/<str:handle>/", social.unfriend, name="unfriend"),
    path("projects/<str:pid>/members/", social.members, name="members"),
    path("invites/", social.invites, name="invites"),
    path("invites/<str:pid>/", social.invite_respond, name="invite-respond"),
    path("workspace/", social.workspace, name="workspace"),
    path("", include(router.urls)),
]
