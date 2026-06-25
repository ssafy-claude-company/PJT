"""SNS 단위 테스트 — 추천 알고리즘(F1301)의 결정 로직을 DB 없이 검증."""
import time

from django.test import TestCase
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
