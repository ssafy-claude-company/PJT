"""DRF 직렬화 — Organt SNS 리소스를 JSON으로(F1304 RESTful)."""
from rest_framework import serializers

from .models import Agent, RoleProfile, Project, CollabTask, Event, Thread, Comment, Like


class AgentSerializer(serializers.ModelSerializer):
    # 봇 id는 19자리(>2^53) — JS Number 정밀도 초과라 반드시 문자열로(반올림→상세조회 404 방지).
    bot_id = serializers.SerializerMethodField()
    event_count = serializers.IntegerField(read_only=True, required=False)
    distill_count = serializers.SerializerMethodField()

    class Meta:
        model = Agent
        fields = ["id", "bot_id", "name", "role", "is_leader", "persona", "avatar",
                  "created_via", "event_count", "distill_count"]

    def get_bot_id(self, obj):
        return str(obj.bot_id)

    def get_distill_count(self, obj):
        # N+1 회피: 뷰가 context["profiles"]({role: distill_count})를 넘기면 그걸 사용.
        prof = self.context.get("profiles")
        if prof is not None:
            return prof.get(obj.role, 0)
        rp = RoleProfile.objects.filter(role=obj.role).first()
        return rp.distill_count if rp else 0


class RoleProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = RoleProfile
        fields = ["id", "role", "criteria", "experience_count", "distill_count", "updated_at"]


class EventSerializer(serializers.ModelSerializer):
    project_pid = serializers.CharField(source="project.pid", default=None, read_only=True)
    project_name = serializers.CharField(source="project.name", default=None, read_only=True)
    actor_role = serializers.CharField(source="actor.role", default=None, read_only=True)
    actor_id = serializers.SerializerMethodField()
    target_id = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = ["seq", "ts", "kind", "project_pid", "project_name",
                  "actor_id", "actor_role", "target_id", "summary"]

    def get_actor_id(self, obj):
        return str(obj.actor.bot_id) if obj.actor else None

    def get_target_id(self, obj):
        return str(obj.target.bot_id) if obj.target else None


class CollabTaskSerializer(serializers.ModelSerializer):
    owner_role = serializers.CharField(source="owner.role", default=None, read_only=True)

    class Meta:
        model = CollabTask
        fields = ["id", "task_id", "purpose", "goal", "owner", "owner_role",
                  "cross_checks", "deploy_count", "status"]


class ProjectSerializer(serializers.ModelSerializer):
    leader_role = serializers.CharField(source="leader.role", default=None, read_only=True)
    event_count = serializers.IntegerField(read_only=True, required=False)
    task_count = serializers.IntegerField(read_only=True, required=False)
    message_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = Project
        fields = ["id", "pid", "name", "leader", "leader_role", "status",
                  "event_count", "task_count", "message_count"]


class ProjectDetailSerializer(ProjectSerializer):
    tasks = CollabTaskSerializer(many=True, read_only=True)

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + ["tasks"]


class CommentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Comment
        fields = ["id", "thread", "author_name", "body", "created_at"]
        read_only_fields = ["created_at"]


class ThreadSerializer(serializers.ModelSerializer):
    project_pid = serializers.CharField(source="project.pid", default=None, read_only=True)
    comment_count = serializers.IntegerField(source="comments.count", read_only=True)
    like_count = serializers.IntegerField(source="likes.count", read_only=True)

    class Meta:
        model = Thread
        fields = ["id", "project", "project_pid", "title", "body",
                  "created_at", "comment_count", "like_count"]
        read_only_fields = ["created_at"]


class ThreadDetailSerializer(ThreadSerializer):
    comments = CommentSerializer(many=True, read_only=True)

    class Meta(ThreadSerializer.Meta):
        fields = ThreadSerializer.Meta.fields + ["comments"]
