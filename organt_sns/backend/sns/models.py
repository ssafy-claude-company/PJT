"""Organt SNS 도메인 모델 (ERD 대상 — README D).

Organt(=AI 직원들이 협업하는 회사)의 협업·소통·성장을 1급 엔티티로. 관계:
  Agent(직원) ─< Event(협업 이벤트)  ·  Project ─< CollabTask ─ Agent(owner)
  RoleProfile(직군 직무기준=성장)  ·  Project ─< Thread ─< Comment / Like  (커뮤니티 F1303)
"""
from django.db import models


class Agent(models.Model):
    """AI 직원(봇). 직군(role)을 가지고 도메인을 책임진다."""
    bot_id = models.BigIntegerField(unique=True, help_text="Organt 두뇌의 봇 id")
    name = models.CharField(max_length=100, blank=True)
    role = models.CharField(max_length=60, blank=True, help_text="직군(백엔드/QA/…)")
    is_leader = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["role", "bot_id"]

    def __str__(self):
        return f"{self.role or '예비'}#{self.bot_id}"


class RoleProfile(models.Model):
    """직군별 *증류된 직무기준* — 경험→수면 증류로 쌓인 역량(에이전트 성장의 결정체)."""
    role = models.CharField(max_length=60, unique=True)
    criteria = models.TextField(blank=True, help_text="증류된 직무기준(품질 루브릭)")
    experience_count = models.IntegerField(default=0, help_text="아직 증류 안 된 원석 경험 수")
    distill_count = models.IntegerField(default=0, help_text="누적 증류 횟수(성장 지표)")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-distill_count", "role"]

    def __str__(self):
        return f"{self.role}(증류 {self.distill_count})"


class Project(models.Model):
    """프로젝트(P-번호) — 한 작품의 협업 공간."""
    pid = models.CharField(max_length=20, unique=True, help_text="P-032 등")
    name = models.CharField(max_length=200, blank=True)
    leader = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="led_projects")
    status = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pid"]

    def __str__(self):
        return f"{self.pid} {self.name}".strip()


class CollabTask(models.Model):
    """프로젝트 내 Task — 목표 단위(owner가 책임, 교차검증·배포 누계)."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    task_id = models.CharField(max_length=40)
    purpose = models.TextField(blank=True)
    goal = models.TextField(blank=True)
    owner = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="owned_tasks")
    cross_checks = models.IntegerField(default=0)
    deploy_count = models.IntegerField(default=0)
    status = models.CharField(max_length=40, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("project", "task_id")

    def __str__(self):
        return f"{self.project.pid}/{self.task_id}"


class Event(models.Model):
    """협업 이벤트 스트림(피드) — 위임/자문/작업/검증/완성/학습 등 협업 서사의 1급 단위."""
    seq = models.BigIntegerField(unique=True, help_text="ingest 단조 시퀀스")
    ts = models.FloatField(help_text="epoch seconds")
    source = models.CharField(max_length=10, help_text="flow|audit")
    kind = models.CharField(max_length=30, db_index=True,
                            help_text="delegation/consultation/work/verification/...")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="events")
    actor = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="events")
    target = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="received_events")
    summary = models.CharField(max_length=500, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-seq"]
        indexes = [models.Index(fields=["-seq"]), models.Index(fields=["kind"])]

    def __str__(self):
        return f"[{self.kind}] {self.summary[:50]}"


class Thread(models.Model):
    """커뮤니티 쓰레드(F1303) — 프로젝트/협업을 주제로 사람·에이전트가 소통하는 공간."""
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE,
                                related_name="threads")
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class Comment(models.Model):
    """쓰레드 댓글 — 사용자가 소통하는 1급 행위(F1303)."""
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="comments")
    author_name = models.CharField(max_length=60, default="익명")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.author_name}: {self.body[:30]}"


class Like(models.Model):
    """좋아요 — 쓰레드 반응(커뮤니티)."""
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="likes")
    user_key = models.CharField(max_length=80, help_text="익명/세션 식별 키")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("thread", "user_key")
