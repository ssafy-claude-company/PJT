"""Organt SNS 도메인 모델 (ERD 대상 — README D).

Organt(=AI 직원들이 협업하는 회사)의 협업·소통·성장을 1급 엔티티로. 관계:
  Agent(직원) ─< Event(협업 이벤트)  ·  Project ─< CollabTask ─ Agent(owner)
  RoleProfile(직군 직무기준=성장)  ·  Project ─< Thread ─< Comment / Like  (커뮤니티 F1303)
"""
from django.db import models


class Agent(models.Model):
    """AI 직원(봇). 직군(role)을 가지고 도메인을 책임진다.

    [스튜디오] 디스코드 계정 제약이 없으니 봇은 *무한·커스텀* — persona(인격 프롬프트)·avatar를
    SNS에서 자유 편집하고, created_via='sns'로 사용자가 채용한 봇을 표시한다."""
    bot_id = models.BigIntegerField(unique=True, help_text="봇 id(디스코드 or SNS 생성)")
    name = models.CharField(max_length=100, blank=True)
    role = models.CharField(max_length=60, blank=True, help_text="직군(백엔드/QA/…)")
    is_leader = models.BooleanField(default=False)
    persona = models.TextField(blank=True, help_text="[커스텀] 봇 인격(시스템 프롬프트)")
    avatar = models.CharField(max_length=8, blank=True, help_text="[커스텀] 아바타 색(hex) 또는 비움(이름 모노그램)")
    model = models.CharField(max_length=20, blank=True,
                             help_text="[per-agent 모델] 이 직원이 쓸 LLM(opus/sonnet/haiku). 비우면 러너 전역 기본")
    created_via = models.CharField(max_length=10, default="discord",
                                   help_text="discord(두뇌 채용) | sns(스튜디오 채용)")
    owner = models.ForeignKey("Person", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="owned_agents",
                              help_text="소유자(사용자가 채용한 '나만의 직원'). null=공개 쇼케이스 직원")
    # 기본 public: 시드(쇼케이스)는 공개 유지. 채용 직원은 뷰에서 private 지정, 소유자가 공유 전환.
    visibility = models.CharField(max_length=10, choices=[("public", "공개"), ("private", "비공개")],
                                  default="public", db_index=True)
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
    VISIBILITY = [("public", "공개"), ("private", "비공개")]
    pid = models.CharField(max_length=20, unique=True, help_text="P-032 등")
    name = models.CharField(max_length=200, blank=True)
    leader = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="led_projects")
    status = models.CharField(max_length=40, blank=True)
    owner = models.ForeignKey("Person", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="owned_projects",
                              help_text="채널 소유자(만든 사람). null=공개 쇼케이스 채널")
    # 기본 public: 시드(쇼케이스)는 공개 유지. 사용자 생성은 뷰에서 명시적으로 private 지정.
    visibility = models.CharField(max_length=10, choices=VISIBILITY, default="public", db_index=True)
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


class GuideMessage(models.Model):
    """[SnsGuide — 라이브 Guide의 1급 메시지] Organt Rule이 *되읽어 상태를 복원*하는 구조화 대화 저장소.

    DiscordGuide가 Discord 메시지에 하던 걸 여기서 DB로 한다 — Rule의 read_thread가 Request/Response
    객체를 재구성할 수 있도록 sender/to/kind/body/reply_to/thread/상태블록을 *그대로* 보존한다.
    (투영용 Event와 별개: Event는 과거 디스코드 로그의 투영, GuideMessage는 SNS 위에서 *실제로 오가는* 메시지.)
    """
    MSG_TYPES = [("plain", "평문"), ("request", "요청"), ("response", "응답"), ("status", "상태블록")]
    msg_id = models.BigAutoField(primary_key=True)            # post/send_*의 반환 message_id
    channel_id = models.BigIntegerField(db_index=True)        # 프로젝트 채널 id
    thread_id = models.BigIntegerField(db_index=True)         # Task 스레드 id(없으면 channel_id)
    sender_id = models.BigIntegerField(default=0)             # 봇 bot_id (0=system/user)
    msg_type = models.CharField(max_length=10, choices=MSG_TYPES, default="plain")
    to_id = models.BigIntegerField(null=True, blank=True)     # 요청 대상 봇
    kind = models.CharField(max_length=1, blank=True)         # 'W'(Work)|'I'(Info)
    reply_to = models.BigIntegerField(null=True, blank=True)  # 응답이 가리키는 요청 msg_id
    body = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)      # 상태블록 등 부가 구조
    edited = models.BooleanField(default=False)
    ts = models.FloatField()

    class Meta:
        ordering = ["msg_id"]
        indexes = [models.Index(fields=["thread_id", "msg_id"]),
                   models.Index(fields=["channel_id", "msg_id"])]

    def __str__(self):
        return f"[{self.msg_type}] {self.sender_id}: {self.body[:40]}"


# ── 소셜(멀티유저) — 사람 유저·친구·채널 멤버 ──────────────────────────
class Person(models.Model):
    """사람 유저 — AI 직원(Agent)과 구분되는 실제 사용자. 핸들+비밀번호로 가입·로그인."""
    handle = models.CharField(max_length=30, unique=True, db_index=True, help_text="@핸들(로그인 식별)")
    name = models.CharField(max_length=60)
    color = models.CharField(max_length=8, blank=True, help_text="아바타 색(hex)")
    bio = models.CharField(max_length=160, blank=True)
    password = models.CharField(max_length=128, blank=True, help_text="Django 해시(set_password)")
    token = models.CharField(max_length=48, blank=True, db_index=True, help_text="인증 토큰(Authorization)")
    is_guest = models.BooleanField(default=False, help_text="둘러보기 체험 계정")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"@{self.handle}"


class Friendship(models.Model):
    """친구 관계 — a가 b에게 요청(pending), b가 수락하면 accepted(양방향 친구)."""
    STATUS = [("pending", "요청됨"), ("accepted", "수락됨")]
    a = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="friendships")   # 요청 보낸 사람
    b = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="friend_of")      # 요청 받은 사람
    status = models.CharField(max_length=10, choices=STATUS, default="pending", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("a", "b")]


class Membership(models.Model):
    """프로젝트(채널) 멤버 — 초대(invited)를 받은 사람이 수락하면 active(실제 멤버)."""
    ROLES = [("lead", "리드"), ("member", "멤버")]
    STATUS = [("invited", "초대됨"), ("active", "참여중")]
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="memberships")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="members")
    role = models.CharField(max_length=10, choices=ROLES, default="member")
    status = models.CharField(max_length=10, choices=STATUS, default="active", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("person", "project")]
        ordering = ["-role", "created_at"]


class EngineHeartbeat(models.Model):
    """협업 엔진(러너) 생존 신호 — 러너가 폴마다 갱신, stats가 engine_live를 파생.
    정적 안내문('엔진이 켜져 있을 때') 대신 실제 가동 여부를 표시하기 위함. 단일 행(pk=1)."""
    last_beat = models.FloatField(default=0.0)      # epoch sec — 마지막 폴 시각
    note = models.CharField(max_length=80, blank=True)   # 모드(local/remote) 등 참고용

    @classmethod
    def beat(cls, note=""):
        import time
        cls.objects.update_or_create(pk=1, defaults={"last_beat": time.time(), "note": note[:80]})


class StopSignal(models.Model):
    """사용자 '작업 중지' 신호 — 웹이 기록, 러너가 폴해 Sys.request_cancel(channel)을 부른 뒤 삭제.
    채널당 1건(중복 클릭은 갱신). 협업 두뇌의 깨끗한 취소 경로(CancelledError)를 사용자 경로로 연다."""
    channel_id = models.BigIntegerField(unique=True, db_index=True)
    requested_at = models.FloatField(default=0.0)
    requested_by = models.CharField(max_length=30, blank=True)


class InterjectSignal(models.Model):
    """사람 '진행 중 개입(정보 전달)' 신호 — 흐름 도중 사람이 넘긴 정보. 웹이 기록, 러너가 폴해
    Sys.deliver_human_info(channel, target, text)로 *대상 봇 다음 턴 프롬프트*에 주입한 뒤 삭제.
    채널당 여러 건 가능(unique 아님) — 큐로 미루지 않고 흐름에 부착(개입의 핵심). StopSignal과 같은 결."""
    channel_id = models.BigIntegerField(db_index=True)
    target_id = models.BigIntegerField(null=True, blank=True)   # 대상 봇(없으면 리더)
    text = models.TextField()
    requested_at = models.FloatField(default=0.0)
    requested_by = models.CharField(max_length=30, blank=True)
    requester_name = models.CharField(max_length=60, blank=True)   # 타임라인 작성자 표시용
