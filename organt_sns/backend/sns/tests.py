"""SNS 단위 테스트 — 추천 알고리즘(F1301)의 결정 로직을 DB 없이 검증."""
from django.test import TestCase

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
