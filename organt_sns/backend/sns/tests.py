"""SNS 단위 테스트 — 추천 알고리즘(F1301)의 결정 로직을 DB 없이 검증."""
import time

from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from sns.recommend import score_candidates, tokens


def _cands():
    return [
        {"bot_id": 1, "name": "", "role": "백엔드", "is_leader": False,
         "event_count": 600, "distill_count": 47, "experience_count": 3,
         "criteria": "REST API 서버 인증 실시간 소켓 동기화 트랜잭션 안정성"},
        {"bot_id": 2, "name": "", "role": "프론트엔드", "is_leader": False,
         "event_count": 300, "distill_count": 59, "experience_count": 1,
         "criteria": "Vue 컴포넌트 렌더 반응형 화면 상태관리"},
        {"bot_id": 3, "name": "", "role": "QA", "is_leader": False,
         "event_count": 120, "distill_count": 5, "experience_count": 0,
         "criteria": "테스트 엣지 케이스 회귀 버그 재현 품질"},
    ]


class RecommendTest(TestCase):
    def test_역할적합이_1차신호다(self):
        # "서버 실시간 동기화"는 직군명 미언급이나 백엔드 힌트·직무기준과 강하게 겹친다.
        ranked = score_candidates("실시간 멀티플레이 서버 동기화", _cands())
        self.assertEqual(ranked[0]["role"], "백엔드")
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])

    def test_직군명_직접지명이_가산된다(self):
        ranked = score_candidates("QA 테스트 품질 검증", _cands())
        self.assertEqual(ranked[0]["role"], "QA")

    def test_근거가_항별로_분해된다(self):
        ranked = score_candidates("화면 반응형 UI 렌더", _cands())
        top = ranked[0]
        self.assertEqual(top["role"], "프론트엔드")
        self.assertEqual(set(top["reasons"]),
                         {"role_match", "keyword_overlap", "expertise", "track_record"})
        # score = 항별 기여도 합
        self.assertAlmostEqual(top["score"], round(sum(top["reasons"].values()), 4), places=3)

    def test_빈질의는_전반역량_상위순(self):
        # 질의가 없으면 역할/키워드 항이 0 → 증류 역량·실적으로 정렬.
        ranked = score_candidates("", _cands())
        self.assertEqual(len(ranked), 3)
        self.assertGreaterEqual(ranked[0]["score"], ranked[-1]["score"])

    def test_토크나이저_한영숫자(self):
        self.assertEqual(tokens("Vue3 실시간-동기화!"), ["vue3", "실시간", "동기화"])


class MeetingVisibilityTest(TestCase):
    """회의·표결이 '유령 채널'로 새지 않고 채널에 네이티브로 표시되는지 — 회귀 가드."""

    def test_post가_스레드를_채널로_해석한다(self):
        # 라우팅 픽스: _say(회의·표결)가 합성 thread_id로 post()해도 실제 채널로 풀려야 한다.
        from sns.sns_guide import SnsGuide
        g = SnsGuide()
        tid = g._new_id()
        g._thread_channel[tid] = 4242                       # open_task가 등록하는 thread→channel 매핑
        self.assertEqual(g._thread_channel.get(int(tid), int(tid)), 4242)  # 회의 발언 → 채널
        self.assertEqual(g._thread_channel.get(99, 99), 99)               # 실제 채널 직접 post는 통과

    def test_회의발언이_messages에서_네이티브kind로_표시(self):
        # 표시 픽스: collab_kind가 [회의] 라벨을 떼고 meeting kind로 — messages 액션 end-to-end.
        from sns.models import Project, Agent, GuideMessage
        proj = Project.objects.create(pid="S-9001", name="검수 채널", visibility="public")
        member = Agent.objects.create(bot_id=900001, role="게임 기획자", name="김도윤")
        GuideMessage.objects.create(                        # 픽스된 post()가 남기는 형태 그대로
            channel_id=proj.id, thread_id=proj.id, sender_id=member.bot_id,
            msg_type="plain", body="[회의 1R] 서버 권위 모델을 제안합니다", ts=time.time())
        res = APIClient().get(f"/api/projects/{proj.pid}/messages/")
        self.assertEqual(res.status_code, 200)
        meet = [m for m in res.data["messages"] if m.get("kind") == "meeting"]
        self.assertEqual(len(meet), 1, "회의 메시지가 표시 안 됨(아까처럼 숨김)")
        self.assertNotIn("[회의", meet[0]["summary"])       # 프로토콜 라벨 제거됨
        self.assertIn("서버 권위", meet[0]["summary"])
        self.assertEqual(meet[0]["round"], 1)               # 라운드 N 보존(블록을 라운드로 분절)


class DeniedFeedTest(TestCase):
    """게이트 거부(denied)는 내부 안전장치 기록 — 활동 피드에 도배되면 안 된다(회귀 가드)."""

    def test_거부는_직원_활동피드에서_제외(self):
        from sns.models import Agent, Event
        a = Agent.objects.create(bot_id=900100, role="게임 기획자", name="박서연", visibility="public")
        Event.objects.create(seq=9001, ts=1.0, kind="work", actor=a, summary="서버 구현")
        Event.objects.create(seq=9002, ts=2.0, kind="denied", actor=a, summary="거부(리더 독식)")
        res = APIClient().get(f"/api/agents/{a.bot_id}/events/")
        self.assertEqual(res.status_code, 200)
        kinds = [e["kind"] for e in res.data]
        self.assertIn("work", kinds)
        self.assertNotIn("denied", kinds)   # 게이트 거부는 '한 일'이 아니라 '막힌 시도' — 피드 제외


class RequeueStuckTest(TestCase):
    """멎은 요청(픽됐지만 응답·완료 없이 멈춤) 다시 맡기기 — 소유자/멤버 세션인증 복구."""

    def _setup(self):
        from sns.models import Project, Person, Membership
        proj = Project.objects.create(pid="S-9100", name="멎은 채널", visibility="public")
        Person.objects.create(handle="tester", name="테스터", token="tok_requeue_1")
        Membership.objects.create(person=Person.objects.get(handle="tester"), project=proj, status="active")
        return proj

    def _stuck(self, proj, picked_ago):
        from sns.models import GuideMessage
        t = time.time() - picked_ago
        return GuideMessage.objects.create(
            channel_id=proj.id, thread_id=proj.id, sender_id=0, msg_type="request",
            kind="W", body="게임 만들어줘", ts=t, payload={"picked": True, "picked_ts": t})

    def _client(self, tok):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        return c

    def test_멎은요청은_멤버가_다시맡기면_재큐된다(self):
        from sns.models import GuideMessage
        proj = self._setup()
        gm = self._stuck(proj, 360)                     # 작업창(5분) 넘게 무응답·미완 = 멎음
        res = self._client("tok_requeue_1").post(f"/api/projects/{proj.pid}/requeue/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["requeued"], 1)
        self.assertFalse((GuideMessage.objects.get(msg_id=gm.msg_id).payload or {}).get("picked"))

    def test_방금_픽한_요청은_재큐_안됨(self):                  # staleness 가드: 작업 중을 끊지 않음
        from sns.models import GuideMessage
        proj = self._setup()
        gm = self._stuck(proj, 5)
        res = self._client("tok_requeue_1").post(f"/api/projects/{proj.pid}/requeue/")
        self.assertEqual(res.data["requeued"], 0)
        self.assertTrue((GuideMessage.objects.get(msg_id=gm.msg_id).payload or {}).get("picked"))

    def test_작업중인_최신픽은_멎음아님_옛픽만_재큐(self):
        # 순차 러너: 가장 최근 픽이 '활성 작업'이라 멎음에서 제외 — 그보다 앞서 픽돼 버려진 것만 재큐.
        from sns.models import GuideMessage, EngineHeartbeat
        proj = self._setup()
        EngineHeartbeat.beat("test")                    # 엔진 가동 중 — 최신 픽을 '작업 중'으로 인정
        old = self._stuck(proj, 400)                    # 옛 픽(버려짐) — 최신이 아니므로 멎음
        live = self._stuck(proj, 30)                    # 방금 픽(작업 중) — 멎음 아님
        res = self._client("tok_requeue_1").post(f"/api/projects/{proj.pid}/requeue/")
        self.assertEqual(res.data["requeued"], 1)       # 옛 픽 하나만
        self.assertFalse((GuideMessage.objects.get(msg_id=old.msg_id).payload or {}).get("picked"))
        self.assertTrue((GuideMessage.objects.get(msg_id=live.msg_id).payload or {}).get("picked"))

    def test_messages_작업중인_요청은_멎음에_안잡힘(self):
        # 표시(messages)의 stuck_count와 live_status가 같은 helper를 써 같은 요청을 동시에 '작업 중·멎음'으로 안 잡음.
        from sns.models import EngineHeartbeat
        proj = self._setup()
        EngineHeartbeat.beat("test")                    # 엔진 가동 중
        self._stuck(proj, 400)                          # 옛 픽 → 멎음 1
        self._stuck(proj, 30)                           # 최신 픽 → 작업 중(live_status)
        data = self._client("tok_requeue_1").get(f"/api/projects/{proj.pid}/messages/").data
        self.assertEqual(data["stuck_count"], 1)        # 옛 픽만 멎음(최신은 작업 중이라 제외)
        self.assertEqual(data["live_status"]["state"], "working")

    def test_엔진_꺼지면_픽은_빨리_멎음(self):
        # heartbeat가 죽으면(러너 사망) 활성 작업도 없고, 픽은 짧은 grace로 빨리 '멎음' 노출 → 빠른 복구.
        from sns.models import EngineHeartbeat
        proj = self._setup()
        EngineHeartbeat.objects.update_or_create(pk=1, defaults={"last_beat": time.time() - 120})  # 엔진 꺼짐
        self._stuck(proj, 45)                           # 45초 전 픽 — 엔진 켜졌으면 작업 중, 꺼졌으니 멎음
        data = self._client("tok_requeue_1").get(f"/api/projects/{proj.pid}/messages/").data
        self.assertEqual(data["stuck_count"], 1)        # 엔진 꺼짐 → 30초 grace 넘겨 멎음
        self.assertIsNone(data["live_status"])          # 작업 중 아님(러너 죽음)

    def test_진행갱신_touch가_picked_ts를_새로고쳐_멎음에서_뺀다(self):
        # 긴 흐름 보호: 러너가 흐름 도중 picked_ts를 touch로 갱신 → 5분+ 협업도 '멎음'으로 오판 안 됨.
        from sns.models import GuideMessage
        from sns.management.commands.run_organt_sns import _local_pick
        from sns.views import stuck_requests
        proj = self._setup()
        gm = self._stuck(proj, 400)                     # 400초 전 픽 → 지금은 멎음 1
        rows = lambda: list(GuideMessage.objects.filter(channel_id=proj.id))
        self.assertEqual(len(stuck_requests(rows(), time.time())), 1)
        _local_pick(gm.msg_id, touch=True)              # 진행 갱신 — picked_ts=now
        self.assertGreater((GuideMessage.objects.get(msg_id=gm.msg_id).payload or {}).get("picked_ts"),
                           time.time() - 5)             # 새로고쳐짐
        self.assertEqual(len(stuck_requests(rows(), time.time())), 0)   # 작업 중으로 되살아나 멎음 0

    def test_messages_조용은_도구활동정지_기준이다_메시지간격_아님(self):
        # [수정] '조용'은 채널 메시지 간격이 아니라 러너가 보고한 실제 도구활동 정지(idle_s) 기준이어야 한다.
        # 협업글은 위임 완료마다(몇 분 간격) 떠서, 잘 돌아도 메시지 기준이면 거짓 '조용'이 떴다.
        from sns.models import EngineHeartbeat, GuideMessage
        proj = self._setup()
        EngineHeartbeat.beat("test")
        g = self._stuck(proj, 5)
        # ① 활발히 작업 중(30초 전 도구활동) — 협업 메시지가 한동안 없어도 '조용' 안 뜸(거짓 경보 제거)
        GuideMessage.objects.filter(msg_id=g.msg_id).update(
            payload={"picked": True, "picked_ts": time.time(), "idle_s": 30})
        data = self._client("tok_requeue_1").get(f"/api/projects/{proj.pid}/messages/").data
        self.assertEqual(data["live_status"]["state"], "working")
        self.assertIsNone(data["live_status"]["quiet"])              # 활동 중 → 조용 아님
        # ② 4분+ 도구활동 정지 = 진짜 멈춤 → 정직하게 quiet에 실린다
        GuideMessage.objects.filter(msg_id=g.msg_id).update(
            payload={"picked": True, "picked_ts": time.time(), "idle_s": 250})
        data = self._client("tok_requeue_1").get(f"/api/projects/{proj.pid}/messages/").data
        self.assertEqual(data["live_status"]["quiet"], 250)         # 실제 무진행 250초 → '4분째 조용'

    def test_messages_봇출력_없으면_quiet는_None(self):
        # 픽 직후 아직 봇 출력이 없으면 정체 판단 불가 → quiet=None(화면은 그냥 '작업 중').
        from sns.models import EngineHeartbeat
        proj = self._setup()
        EngineHeartbeat.beat("test")
        self._stuck(proj, 5)
        data = self._client("tok_requeue_1").get(f"/api/projects/{proj.pid}/messages/").data
        self.assertEqual(data["live_status"]["state"], "working")
        self.assertIsNone(data["live_status"]["quiet"])

    def test_권한_비로그인401_비멤버403(self):
        from sns.models import Person
        proj = self._setup()
        self._stuck(proj, 300)
        self.assertEqual(APIClient().post(f"/api/projects/{proj.pid}/requeue/").status_code, 401)
        Person.objects.create(handle="outsider", name="남", token="tok_out")
        self.assertEqual(self._client("tok_out").post(f"/api/projects/{proj.pid}/requeue/").status_code, 403)


class EngineHeartbeatTest(TestCase):
    """협업 엔진(러너) 생존 — stats가 heartbeat에서 engine_live를 파생(정적 안내문 대체)."""

    def test_heartbeat_없으면_꺼짐_최근이면_가동(self):
        from sns.models import EngineHeartbeat
        self.assertFalse(APIClient().get("/api/stats/").data["engine"]["live"])   # 신호 없음 → 꺼짐
        EngineHeartbeat.beat("test")
        self.assertTrue(APIClient().get("/api/stats/").data["engine"]["live"])    # 방금 beat → 가동

    def test_오래된_heartbeat는_꺼짐(self):
        from sns.models import EngineHeartbeat
        EngineHeartbeat.objects.update_or_create(pk=1, defaults={"last_beat": time.time() - 60})
        self.assertFalse(APIClient().get("/api/stats/").data["engine"]["live"])   # 60초 전 > 30초 임계 → 꺼짐


class ClassifyKindTest(TestCase):
    """Work/Info 자동 분류 — 질문은 Info(자문), 지시는 Work(위임). 토글은 override."""

    def test_질문은_Info(self):
        from sns.views import classify_kind
        for q in ["이거 어떻게 만들어?", "배포 됐나요", "왜 안 되지", "무슨 색이 좋을까요", "가능한가?"]:
            self.assertEqual(classify_kind(q), "I", q)

    def test_지시는_Work(self):
        from sns.views import classify_kind
        for w in ["로그인 페이지 만들어줘", "서버에 캐시 붙여", "2인 협동 게임 제작", "버튼 색을 보라색으로 바꿔"]:
            self.assertEqual(classify_kind(w), "W", w)

    def test_캐주얼_발화는_Info(self):
        # [근본] 일상 발화·추천·짧은 진술은 Work가 아니라 Info(대화) — 프로젝트 기계가 안 돌게.
        from sns.views import classify_kind
        for c in ["배고파", "예산이 2억7천만원 있어", "오늘 점심 추천좀", "안녕", "저녁 뭐 먹지", "출출하다"]:
            self.assertEqual(classify_kind(c), "I", c)

    def test_빌드동사_들어간_질문은_Info(self):
        # '배포 됐나요?'는 질문 — 빌드동사(배포)보다 질문 신호가 우선.
        from sns.views import classify_kind
        for q in ["배포 됐나요", "만들어졌어?", "수정됐는지 확인해줄래"]:
            self.assertEqual(classify_kind(q), "I", q)

    def test_make_request_auto는_본문분류_명시는_override(self):
        from sns.models import Project, Person
        proj = Project.objects.create(pid="S-9200", name="분류 채널", visibility="public")
        Person.objects.create(handle="cu", name="씨유", token="tok_cls")
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token tok_cls")
        self.assertEqual(c.post(f"/api/projects/{proj.pid}/request/",
                                {"kind": "auto", "body": "이거 어떻게 해요?"}).data["kind"], "I")
        self.assertEqual(c.post(f"/api/projects/{proj.pid}/request/",
                                {"kind": "W", "body": "이거 어떻게 해요?"}).data["kind"], "W")  # override


class StopWorkTest(TestCase):
    """작업 중지 — 소유자/멤버가 신호 기록 → 러너가 폴해 SYS.request_cancel(진행 흐름 협조적 취소)."""

    def _setup(self):
        from sns.models import Project, Person, Membership
        proj = Project.objects.create(pid="S-9300", name="중지 채널", visibility="public")
        Person.objects.create(handle="st", name="중지", token="tok_stop")
        Membership.objects.create(person=Person.objects.get(handle="st"), project=proj, status="active")
        return proj

    def test_멤버는_중지신호_기록(self):
        from sns.models import StopSignal
        proj = self._setup()
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token tok_stop")
        self.assertEqual(c.post(f"/api/projects/{proj.pid}/stop/").status_code, 200)
        self.assertTrue(StopSignal.objects.filter(channel_id=proj.id).exists())

    def test_권한_비로그인401_비멤버403(self):
        from sns.models import Person
        proj = self._setup()
        self.assertEqual(APIClient().post(f"/api/projects/{proj.pid}/stop/").status_code, 401)
        Person.objects.create(handle="out2", name="남", token="tok_out2")
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token tok_out2")
        self.assertEqual(c.post(f"/api/projects/{proj.pid}/stop/").status_code, 403)


class PerAgentModelTest(TestCase):
    """per-agent 모델 — 편집으로 직원별 LLM 지정(소유자만, 허용값만). 러너가 model_map을 빌더에 전달해
    그 봇만 build_options에 model override를 싣는다(전체 체인은 tests/test_sys 의 _make_builder 검증)."""

    def test_edit_모델_허용값만_소유자만(self):
        from sns.models import Agent, Person
        owner = Person.objects.create(handle="own", name="주인", token="tok_own")
        a = Agent.objects.create(bot_id=920001, role="백엔드", owner=owner, visibility="public")
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token tok_own")
        self.assertEqual(c.patch(f"/api/agents/{a.bot_id}/edit/", {"model": "opus"}).data["model"], "opus")
        self.assertEqual(c.patch(f"/api/agents/{a.bot_id}/edit/", {"model": "gpt-9"}).data["model"], "")  # 허용 외 → 전역
        Person.objects.create(handle="other", name="남", token="tok_other")
        c2 = APIClient()
        c2.credentials(HTTP_AUTHORIZATION="Token tok_other")
        self.assertEqual(c2.patch(f"/api/agents/{a.bot_id}/edit/", {"model": "opus"}).status_code, 403)  # 남의 직원

    def test_local_models_지정봇만(self):
        from sns.models import Agent
        from sns.management.commands.run_organt_sns import _local_models
        Agent.objects.create(bot_id=920010, role="백엔드", model="opus")
        Agent.objects.create(bot_id=920011, role="QA", model="")          # 미지정 → 전역
        mm = _local_models()
        self.assertEqual(mm.get(920010), "opus")
        self.assertNotIn(920011, mm)


class InterjectTest(TestCase):
    """진행 중 개입(정보 전달) — 소유자/멤버가 신호 기록 + 타임라인 표시용 plain. 러너가 폴해 주입."""

    def _setup(self):
        from sns.models import Project, Person, Membership
        proj = Project.objects.create(pid="S-9400", name="개입 채널", visibility="public")
        Person.objects.create(handle="iv", name="개입자", token="tok_iv")
        Membership.objects.create(person=Person.objects.get(handle="iv"), project=proj, status="active")
        return proj

    def test_멤버는_개입신호_기록_및_타임라인표시(self):
        from sns.models import InterjectSignal
        proj = self._setup()
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token tok_iv")
        self.assertEqual(c.post(f"/api/projects/{proj.pid}/interject/", {"body": "백엔드 코드 이상, 다시 봐"}).status_code, 200)
        self.assertTrue(InterjectSignal.objects.filter(channel_id=proj.id, text__icontains="백엔드").exists())
        msgs = c.get(f"/api/projects/{proj.pid}/messages/").data["messages"]
        self.assertTrue(any(x["type"] == "human" and "백엔드 코드 이상" in x["body"] for x in msgs))

    def test_빈내용_400_권한_401_403(self):
        from sns.models import Person
        proj = self._setup()
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Token tok_iv")
        self.assertEqual(c.post(f"/api/projects/{proj.pid}/interject/", {"body": "  "}).status_code, 400)
        self.assertEqual(APIClient().post(f"/api/projects/{proj.pid}/interject/", {"body": "x"}).status_code, 401)
        Person.objects.create(handle="ov", name="남", token="tok_ov")
        c2 = APIClient()
        c2.credentials(HTTP_AUTHORIZATION="Token tok_ov")
        self.assertEqual(c2.post(f"/api/projects/{proj.pid}/interject/", {"body": "x"}).status_code, 403)

    def test_local_interject_pending_소거(self):
        from sns.models import InterjectSignal
        from sns.management.commands.run_organt_sns import _local_interject_pending
        proj = self._setup()
        InterjectSignal.objects.create(channel_id=proj.id, target_id=None, text="hi", requested_at=0)
        out = _local_interject_pending(proj.id)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "hi")
        self.assertFalse(InterjectSignal.objects.filter(channel_id=proj.id).exists())


class SqliteConcurrencyPragmaTest(TestCase):
    """다중 접속 안정화 — 모든 SQLite 연결에 WAL+busy_timeout이 걸려 'database is locked'를 막는다.
    (apps.SnsConfig.ready의 connection_created 시그널 회귀 방지.)"""

    def test_연결에_busy_timeout과_WAL이_걸린다(self):
        from django.db import connection
        if connection.vendor != "sqlite":
            self.skipTest("SQLite 전용 — Postgres(자체 서버)에선 no-op")
        with connection.cursor() as c:
            c.execute("PRAGMA busy_timeout;"); bt = c.fetchone()[0]
            c.execute("PRAGMA journal_mode;"); jm = str(c.fetchone()[0]).lower()
        self.assertGreaterEqual(int(bt), 20000)          # 잠금 시 대기(기본 0/5초 → 20초) — 시그널이 적용됨
        self.assertIn(jm, ("wal", "memory"))             # 디스크면 WAL, 테스트 인메모리면 memory


class ReadThreadResponseTest(TransactionTestCase):
    """read_thread 회귀 — 응답(response) 메시지가 든 스레드를 읽을 때 던지지 않는다.
    (async ORM(sync_to_async)이 스레드 연결을 쓰므로 TransactionTestCase로 — 원자 트랜잭션 잠금 회피.)
    이전엔 Response(reply_to=…)로 잘못 생성해 TypeError → 봇이 답한 채널은 read_thread가 통째로 실패했고,
    브레인 '상황 인지'(채널 최근 대화 주입)가 조용히 빈손이 됐다. 필드명은 replies_to."""

    def test_응답이_든_스레드도_던지지_않고_replies_to를_채운다(self):
        import asyncio
        from sns.models import GuideMessage
        from sns.sns_guide import SnsGuide
        tid = 778899
        req = GuideMessage.objects.create(channel_id=tid, thread_id=tid, sender_id=0, ts=time.time(),
                                          msg_type="request", kind="I", body="오늘 저녁 추천좀")
        GuideMessage.objects.create(channel_id=tid, thread_id=tid, sender_id=12, msg_type="response",
                                    ts=time.time(), reply_to=req.msg_id, body="순두부찌개 어떠세요")
        rows = asyncio.run(SnsGuide().read_thread(tid, include_plain=True))   # 던지면 실패
        self.assertEqual(len(rows), 2)
        resp = rows[-1]
        self.assertEqual(resp.body, "순두부찌개 어떠세요")
        self.assertEqual(resp.replies_to, str(req.msg_id))   # 올바른 필드에 매핑


class FlowIdleReaperTest(TestCase):
    """정체 기준 슬롯 회수 — 흐름의 '무진행 시간'(last_activity 정지)으로 먹통을 가린다(나이 아님).
    잘 도는 흐름(최근 활동)은 작은 값, 멈춘 흐름은 큰 값, 흐름 없으면 None — '10분 넘게 일하면 무조건
    끊김'(나이 컷) 대신 진짜 멈춤만 회수하게 한 회귀 가드."""

    def test_flow_idle_무진행시간으로_먹통만_가린다(self):
        import time as _t
        from sns.management.commands.run_organt_sns import _flow_idle

        class _Flow:
            def __init__(self, ch, la, done=False):
                self.user_channel, self.last_activity, self.done = ch, la, done

        class _Sys:
            def __init__(self, flows): self.active_flows = dict(enumerate(flows))

        now = _t.monotonic()
        sysm = _Sys([_Flow(500, now - 1200), _Flow(600, now - 5)])
        self.assertGreater(_flow_idle(sysm, 500), 1000)   # 20분 전 활동 → 멈춤(큰 값 → 회수 대상)
        self.assertLess(_flow_idle(sysm, 600), 60)        # 방금 활동 → 진행 중(작은 값 → 안 끊음)
        self.assertIsNone(_flow_idle(sysm, 999))          # 그 채널 활성 흐름 없음
        self.assertIsNone(_flow_idle(_Sys([_Flow(700, now - 1200, done=True)]), 700))  # 완료는 제외


class StopChannelTest(TestCase):
    """중지 신뢰성 — 흐름이 *안 도는 사이* 누른 중지도 픽 요청을 '중지됨'으로 종결한다(작업중·멎음 아님,
    재픽·재개 차단). 종전엔 inflight 채널만 중지를 봐서 유실됐다(라이브: cancel 0건). 전역 스캔으로 해소."""

    def _setup(self):
        from sns.models import Project, Person, Membership
        proj = Project.objects.create(pid="S-9500", name="중지채널", visibility="public")
        Person.objects.create(handle="sc", name="중지씨", token="tok_sc")
        Membership.objects.create(person=Person.objects.get(handle="sc"), project=proj, status="active")
        return proj

    def _picked(self, proj, ago=30):
        from sns.models import GuideMessage
        t = time.time() - ago
        return GuideMessage.objects.create(channel_id=proj.id, thread_id=proj.id, sender_id=0,
            msg_type="request", kind="W", body="게임 만들어줘", ts=t, payload={"picked": True, "picked_ts": t})

    def test_stop_channel_픽요청을_중지됨으로_종결(self):
        from sns.management.commands.run_organt_sns import _local_stop_channel
        from sns.models import GuideMessage
        proj = self._setup()
        gm = self._picked(proj)
        self.assertEqual(_local_stop_channel(proj.id), 1)
        p = GuideMessage.objects.get(msg_id=gm.msg_id).payload
        self.assertTrue(p.get("stopped"))
        self.assertTrue(p.get("done_ts"))

    def test_messages_중지된_요청은_작업중도_멎음도_아님(self):
        from sns.management.commands.run_organt_sns import _local_stop_channel
        from sns.models import EngineHeartbeat
        proj = self._setup()
        EngineHeartbeat.beat("test")                       # 엔진 가동
        self._picked(proj)                                 # 원래 '작업 중'
        c = APIClient(); c.credentials(HTTP_AUTHORIZATION="Token tok_sc")
        d = c.get(f"/api/projects/{proj.pid}/messages/").data
        self.assertEqual(d["live_status"]["state"], "working")   # 중지 전: 작업 중
        _local_stop_channel(proj.id)                       # 사용자 중지(흐름 안 도는 사이)
        d = c.get(f"/api/projects/{proj.pid}/messages/").data
        self.assertEqual(d["live_status"]["state"], "stopped")   # 중지 후: 중지됨
        self.assertEqual(d["stuck_count"], 0)                    # 멎음에도 안 잡힘

    def test_중지된_요청은_재픽_대기에_안_뜬다(self):
        from sns.management.commands.run_organt_sns import _local_stop_channel, _local_pending
        proj = self._setup()
        self._picked(proj)
        _local_stop_channel(proj.id)
        self.assertEqual(len(_local_pending(set())), 0)    # picked+done → pending 아님(재픽/재개 차단)

    def test_local_all_stops_전체_반환_소거(self):
        from sns.management.commands.run_organt_sns import _local_all_stops
        from sns.models import StopSignal
        proj = self._setup()
        StopSignal.objects.create(channel_id=proj.id)
        StopSignal.objects.create(channel_id=999123)
        chans = _local_all_stops()
        self.assertIn(proj.id, chans); self.assertIn(999123, chans)
        self.assertFalse(StopSignal.objects.exists())      # 전부 소거(전역 스캔 1회 소비)


class DisplayAndRoutingTest(TestCase):
    """B(actor 스왑)·C(빈 버블)·A(자동 리더) 회귀 가드."""

    def _proj(self, pid="S-9700", leader=None):
        from sns.models import Project, Person, Membership
        proj = Project.objects.create(pid=pid, name="ch", visibility="public", leader=leader)
        Person.objects.get_or_create(handle="dr", defaults={"name": "디알", "token": "tok_dr"})
        Membership.objects.get_or_create(person=Person.objects.get(handle="dr"), project=proj,
                                         defaults={"status": "active"})
        return proj

    def _gm(self, proj, sender, mtype="plain", to=None, kind="", body="x", payload=None):
        from sns.models import GuideMessage
        return GuideMessage.objects.create(channel_id=proj.id, thread_id=proj.id, sender_id=sender,
            msg_type=mtype, to_id=to, kind=kind, body=body, ts=time.time(), payload=payload or {})

    def _client(self):
        c = APIClient(); c.credentials(HTTP_AUTHORIZATION="Token tok_dr"); return c

    def test_B_actor는_담당_to_id_고정_마지막발화자_아님(self):
        from sns.models import Agent, EngineHeartbeat
        EngineHeartbeat.beat("t")
        be = Agent.objects.create(bot_id=11, role="백엔드", name="고은호", visibility="public")
        fe = Agent.objects.create(bot_id=12, role="프론트", name="이서준", visibility="public")
        proj = self._proj()
        # 고은호(11)에게 맡긴 작업 중 요청
        self._gm(proj, 0, "request", to=11, kind="W", body="게임 만들어줘",
                 payload={"picked": True, "picked_ts": time.time()})
        self._gm(proj, 12, "plain", body="제가 프론트 먼저 봤습니다")   # 이서준이 '마지막 발화'
        d = self._client().get(f"/api/projects/{proj.pid}/messages/").data
        self.assertEqual(d["live_status"]["state"], "working")
        self.assertEqual(d["live_status"]["actor"], "고은호")   # 담당(to_id) — 마지막 발화자 이서준 아님

    def test_B_담당없으면_프로젝트_리더(self):
        from sns.models import Agent, EngineHeartbeat
        EngineHeartbeat.beat("t")
        lead = Agent.objects.create(bot_id=21, role="기획", name="강주원", visibility="public")
        proj = self._proj(pid="S-9701", leader=lead)
        self._gm(proj, 0, "request", to=None, kind="W", body="추천좀",
                 payload={"picked": True, "picked_ts": time.time()})
        self._gm(proj, 99, "plain", body="누군가 발화")
        d = self._client().get(f"/api/projects/{proj.pid}/messages/").data
        self.assertEqual(d["live_status"]["actor"], "강주원")   # 담당 없음 → 프로젝트 리더

    def test_C_빈_본문_라벨만_메시지는_버블로_안남는다(self):
        from sns.models import Agent
        Agent.objects.create(bot_id=11, role="백엔드", name="고은호", visibility="public")
        proj = self._proj(pid="S-9702")
        self._gm(proj, 11, "plain", body="[회의 1R]")        # 라벨만 — collab_kind 후 본문 빈다
        self._gm(proj, 11, "plain", body="[회의 1R] 실제 내용 있음")
        d = self._client().get(f"/api/projects/{proj.pid}/messages/").data
        bodies = [m.get("summary") or m.get("body") or "" for m in d["messages"] if m.get("type") == "agent"]
        self.assertNotIn("", [b.strip() for b in bodies])     # 빈 버블 없음
        self.assertTrue(any("실제 내용" in b for b in bodies)) # 내용 있는 건 남음

    def test_A_route_to_채널리더_없으면_최근봇_없으면_None(self):
        from sns.management.commands.run_organt_sns import _route_to, _local_pending
        from sns.models import Agent
        lead = Agent.objects.create(bot_id=31, role="기획", name="리더봇", visibility="public")
        p1 = self._proj(pid="S-9703", leader=lead)
        self.assertEqual(_route_to(p1.id), 31)                # 지정 리더 우선
        p2 = self._proj(pid="S-9704")                          # 리더 없음
        self._gm(p2, 42, "plain", body="최근 활동 봇")
        self.assertEqual(_route_to(p2.id), 42)                # 최근 활동 봇
        p3 = self._proj(pid="S-9705")                          # 리더도 봇활동도 없음
        self.assertIsNone(_route_to(p3.id))
        # 미지정 요청의 pending에 route_to 실림
        self._gm(p1, 0, "request", to=None, kind="W", body="추천")
        row = [r for r in _local_pending(set()) if r["channel_id"] == p1.id][0]
        self.assertEqual(row["route_to"], 31)


class ResumeCutBuildTest(TestCase):
    """[잘린 빌드 자동 재개] 회귀 가드 — 러너가 죽어 picked_ts가 멎은 빌드는 다시 큐로,
    살아있는(touch 중)·완료·중지·이미응답한 요청은 재개 안 됨."""

    def _proj(self, pid="S-9800", leader=None):
        from sns.models import Project
        return Project.objects.create(pid=pid, name="ch", visibility="public", leader=leader)

    def _gm(self, proj, sender, mtype="request", to=None, kind="W", body="빌드", payload=None):
        from sns.models import GuideMessage
        return GuideMessage.objects.create(channel_id=proj.id, thread_id=proj.id, sender_id=sender,
            msg_type=mtype, to_id=to, kind=kind, body=body, ts=time.time(), payload=payload or {})

    def test_멎은_빌드는_재큐_살아있는것은_아님(self):
        from sns.management.commands.run_organt_sns import _local_pending
        proj = self._proj()
        stale = self._gm(proj, 0, body="fps 만들어줘",
                         payload={"picked": True, "picked_ts": time.time() - 600})   # 10분 전 픽, touch 끊김 = 사망
        live = self._gm(proj, 0, body="todo 만들어줘",
                        payload={"picked": True, "picked_ts": time.time() - 5})       # 방금 touch = 살아있음
        ids = {r["msg_id"] for r in _local_pending(set())}
        self.assertIn(stale.msg_id, ids)        # 멎은 빌드 → 재개
        self.assertNotIn(live.msg_id, ids)      # 도는 빌드 → 그대로

    def test_완료_중지_미픽은_재개_규칙(self):
        from sns.management.commands.run_organt_sns import _local_pending
        proj = self._proj(pid="S-9801")
        done = self._gm(proj, 0, payload={"picked": True, "picked_ts": time.time() - 600,
                                          "done_ts": time.time()})                    # 완료
        stopped = self._gm(proj, 0, payload={"picked": True, "picked_ts": time.time() - 600,
                                             "stopped": True, "done_ts": time.time()})  # 사용자 중지
        fresh = self._gm(proj, 0, payload={})                                          # 미픽 신규 = 항상 큐
        ids = {r["msg_id"] for r in _local_pending(set())}
        self.assertNotIn(done.msg_id, ids)
        self.assertNotIn(stopped.msg_id, ids)
        self.assertIn(fresh.msg_id, ids)

    def test_이미_응답한_멎은_빌드는_재개안됨(self):
        from sns.management.commands.run_organt_sns import _local_pending
        from sns.models import GuideMessage
        proj = self._proj(pid="S-9802")
        answered = self._gm(proj, 0, body="이미 끝낸 빌드",
                            payload={"picked": True, "picked_ts": time.time() - 600})
        GuideMessage.objects.create(channel_id=proj.id, thread_id=proj.id, sender_id=11,
            msg_type="response", reply_to=answered.msg_id, body="다 했습니다", ts=time.time())
        ids = {r["msg_id"] for r in _local_pending(set())}
        self.assertNotIn(answered.msg_id, ids)   # 응답 존재 → 재개 안 함(완료로 간주)

    def test_seen_집합은_재개에서_제외(self):
        from sns.management.commands.run_organt_sns import _local_pending
        proj = self._proj(pid="S-9803")
        stale = self._gm(proj, 0, payload={"picked": True, "picked_ts": time.time() - 600})
        self.assertNotIn(stale.msg_id, {r["msg_id"] for r in _local_pending({stale.msg_id})})


class BackstopResumeTest(TestCase):
    """[백스톱 컷 재개] _local_pick(unpick=True)가 픽을 해제해 다시 큐로 돌리는지 — 1시간컷/정체컷 후
    같은 러너가 이어받게 하는 핵심(seen만 비우면 pending이 다시 내보냄)."""

    def test_unpick_clears_picked_state(self):
        from sns.management.commands.run_organt_sns import _local_pick, _local_pending
        from sns.models import Project, GuideMessage
        proj = Project.objects.create(pid="S-9900", name="ch", visibility="public")
        gm = GuideMessage.objects.create(channel_id=proj.id, thread_id=proj.id, sender_id=0,
            msg_type="request", kind="W", body="fps 이어서", ts=time.time(),
            payload={"picked": True, "picked_ts": time.time(), "done_ts": time.time()})
        _local_pick(gm.msg_id, unpick=True)
        p = GuideMessage.objects.get(msg_id=gm.msg_id).payload or {}
        self.assertNotIn("picked", p)
        self.assertNotIn("done_ts", p)
        self.assertNotIn("picked_ts", p)
        # 픽 해제됐으니 pending에 다시 뜬다(재개 가능)
        self.assertIn(gm.msg_id, {r["msg_id"] for r in _local_pending(set())})


class ArticleBoardTest(TestCase):
    """프로젝트 산출물·작업 보드(/article) — 배포/저장소 링크 추출·분류 + Task 단위."""

    def _client(self):
        from sns.models import Person
        Person.objects.get_or_create(handle="ab", defaults={"name": "에이비", "token": "tok_ab"})
        c = APIClient(); c.credentials(HTTP_AUTHORIZATION="Token tok_ab"); return c

    def test_deliverables_라이브_봇메시지_배포_repo_링크_추출_분류(self):
        from sns.models import Project, Agent, GuideMessage
        be = Agent.objects.create(bot_id=11, role="백엔드", name="고은호", visibility="public")
        proj = Project.objects.create(pid="S-9800", name="보드", visibility="public", leader=be)
        GuideMessage.objects.create(channel_id=proj.id, thread_id=proj.id, sender_id=11, msg_type="plain",
            body="배포 완료: https://organt-p-001.onrender.com 코드: https://github.com/me/proj.",
            ts=time.time(), payload={})
        # sender 0(사람) URL은 산출물 아님(봇 산출만 집계)
        GuideMessage.objects.create(channel_id=proj.id, thread_id=proj.id, sender_id=0, msg_type="request",
            body="참고 https://example.com/ref", ts=time.time(), payload={})
        d = self._client().get(f"/api/projects/{proj.pid}/article/").data
        types = {x["type"]: x["url"] for x in d["deliverables"]}
        self.assertIn("deploy", types); self.assertIn("repo", types)
        self.assertTrue(types["deploy"].endswith("onrender.com"))     # 꼬리 구두점 제거됨
        self.assertIn("github.com/me/proj", types["repo"])
        self.assertFalse(any("example.com" in x["url"] for x in d["deliverables"]))  # 사람 링크 제외
        self.assertEqual(d["deliverables"][0]["type"], "deploy")      # deploy 먼저 정렬
        self.assertEqual(d["stats"]["live_links"], 1)
        self.assertEqual(d["stats"]["repo_links"], 1)

    def test_tasks_단위_담당_상태_노출(self):
        from sns.models import Project, Agent, CollabTask
        be = Agent.objects.create(bot_id=11, role="백엔드", name="고은호", visibility="public")
        proj = Project.objects.create(pid="S-9801", name="보드2", visibility="public", leader=be)
        CollabTask.objects.create(project=proj, task_id="T1", purpose="서버 구현",
            owner=be, cross_checks=2, deploy_count=1, status="완료")
        d = self._client().get(f"/api/projects/{proj.pid}/article/").data
        self.assertEqual(len(d["tasks"]), 1)
        t = d["tasks"][0]
        self.assertEqual(t["task_id"], "T1")
        self.assertEqual(t["owner_role"], "백엔드")
        self.assertEqual(t["owner_name"], "고은호")
        self.assertEqual(t["status"], "완료")
        self.assertEqual(t["cross_checks"], 2)

    def test_task_complete_이벤트_result_링크도_산출물_그리고_완료상태(self):
        from sns.models import Project, Agent, Event
        be = Agent.objects.create(bot_id=11, role="백엔드", name="고은호", visibility="public")
        proj = Project.objects.create(pid="S-9802", name="보드3", visibility="public", leader=be)
        Event.objects.create(seq=99501, ts=1.0, source="flow", kind="task_complete", project=proj,
            actor=be, summary="완성", payload={"result": "라이브 https://demo.onrender.com 입니다"})
        d = self._client().get(f"/api/projects/{proj.pid}/article/").data
        self.assertTrue(any(x["type"] == "deploy" and "demo.onrender.com" in x["url"]
                            for x in d["deliverables"]))
        self.assertEqual(d["status"], "완료")     # task_complete 있으면 완료


class SecretVaultTest(TestCase):
    """개인 자격증명 금고(/me/secrets) — 암호화 저장·값 미반환·소유 격리·BYO 복호화."""

    def _person(self, handle="vault1", guest=False):
        from sns.models import Person
        p, _ = Person.objects.get_or_create(
            handle=handle, defaults={"name": "금고", "token": "tok_" + handle, "is_guest": guest})
        return p

    def _client(self, handle="vault1"):
        c = APIClient(); c.credentials(HTTP_AUTHORIZATION=f"Token tok_{handle}"); return c

    def test_저장은_암호화_되고_값은_절대_미반환(self):
        self._person()
        r = self._client().post("/api/me/secrets/",
                                {"name": "RENDER_KEY", "value": "rnd_supersecret123"}, format="json")
        self.assertEqual(r.status_code, 200)
        s = r.data["secrets"]
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["name"], "RENDER_KEY")
        self.assertNotIn("value", s[0])
        self.assertEqual(s[0]["hint"], "••••t123")
        self.assertNotIn("supersecret", str(r.data))            # 값이 응답 어디에도 없음
        from sns.models import PersonSecret
        from sns.secrets_vault import decrypt
        ps = PersonSecret.objects.get(name="RENDER_KEY")
        self.assertNotIn("supersecret", ps.value_enc)           # DB는 암호문
        self.assertEqual(decrypt(ps.value_enc), "rnd_supersecret123")   # 서버만 복호화

    def test_임의_이름_범용저장_플랫폼_무관(self):
        # 금고는 Render 전용이 아니다 — 어떤 이름이든 저장(VERCEL_TOKEN·임의 키). 고정칸 없음.
        self._person()
        c = self._client()
        for n in ("VERCEL_TOKEN", "NETLIFY_AUTH_TOKEN", "MY_CUSTOM_VAR"):
            c.post("/api/me/secrets/", {"name": n, "value": n.lower() + "_v"}, format="json")
        names = {s["name"] for s in self._client().get("/api/me/secrets/").data["secrets"]}
        self.assertEqual(names, {"VERCEL_TOKEN", "NETLIFY_AUTH_TOKEN", "MY_CUSTOM_VAR"})
        # 응답엔 Render 고정 필드(deploy_ready/deploy_names)가 더는 없다
        self.assertNotIn("deploy_ready", self._client().get("/api/me/secrets/").data)

    def test_남의_시크릿은_안보이고_삭제된다(self):
        self._person("vault1"); self._person("vaultB")
        self._client("vault1").post("/api/me/secrets/", {"name": "GH_PAT", "value": "ghp_a"}, format="json")
        self.assertEqual(len(self._client("vaultB").get("/api/me/secrets/").data["secrets"]), 0)
        self.assertTrue(self._client("vault1").delete("/api/me/secrets/GH_PAT/").data["deleted"])
        self.assertEqual(len(self._client("vault1").get("/api/me/secrets/").data["secrets"]), 0)

    def test_게스트는_저장_불가_미인증은_401(self):
        self._person("vaultG", guest=True)
        self.assertEqual(self._client("vaultG").post("/api/me/secrets/",
                         {"name": "GH_PAT", "value": "x"}, format="json").status_code, 403)
        self.assertEqual(APIClient().get("/api/me/secrets/").status_code, 401)

    def test_deploy_creds_for_owner값_복호화(self):
        from sns.social import deploy_creds_for
        p = self._person("vaultD"); c = self._client("vaultD")
        c.post("/api/me/secrets/", {"name": "RENDER_KEY", "value": "rnd_dep"}, format="json")
        c.post("/api/me/secrets/", {"name": "GH_PAT", "value": "ghp_dep"}, format="json")
        creds = deploy_creds_for(p)                       # names 없으면 전부
        self.assertEqual(creds["RENDER_KEY"], "rnd_dep")
        self.assertEqual(creds["GH_PAT"], "ghp_dep")
        only = deploy_creds_for(p, ["RENDER_KEY"])        # 어댑터가 필요한 키만
        self.assertEqual(only, {"RENDER_KEY": "rnd_dep"})
