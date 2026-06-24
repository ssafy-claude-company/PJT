"""강점 기반 적임자 추천 (F1301).

Organt의 본질 기능인 *적임자 선발*(recruit / Task owner 지정)을 사용자향 추천으로 표현한다.
주어진 도메인·요구 키워드에 가장 적합한 AI 직원(에이전트)을 **설명 가능한 점수**로 랭킹한다.

  score = w_role·역할적합 + w_kw·직무기준_키워드중복 + w_exp·증류역량 + w_track·활동실적

모든 항은 후보군 내에서 0~1로 정규화하고, 응답에 *항별 기여도(reasons)*를 함께 돌려준다.
→ "왜 이 직원을 추천했는가"가 투명 = 추천 알고리즘이 채점 가능한 형태(F1301).

순수 함수(score_candidates)는 DB 없이 단위 테스트 가능하게 분리했다(sns/tests.py).
"""
import re

# 가중치(합 1.0). 역할 적합이 1차 신호, 직무기준 의미중복이 2차, 증류 역량·실적이 보정.
W_ROLE, W_KW, W_EXP, W_TRACK = 0.40, 0.30, 0.20, 0.10

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")

# 도메인 키워드 → 직군 시소러스. 질의가 직군명을 직접 쓰지 않아도(예: "실시간 멀티플레이 서버")
# 해당 직군(백엔드)으로 매칭되도록 하는 힌트 사전.
ROLE_HINTS = {
    "백엔드":      ["서버", "api", "db", "데이터베이스", "backend", "로직", "인증", "배포", "실시간", "소켓", "동기화"],
    "프론트엔드":  ["프론트", "ui", "ux", "화면", "frontend", "리액트", "react", "뷰", "vue", "css", "웹", "렌더", "반응형"],
    "기획":        ["기획", "planner", "요구사항", "스펙", "와이어프레임", "유저스토리", "정책"],
    "게임기획":    ["게임", "레벨", "밸런스", "game", "플레이", "재미", "스테이지", "메커닉", "협동"],
    "QA":          ["qa", "테스트", "품질", "버그", "검증", "엣지", "test", "회귀", "안정성"],
    "디자인":      ["디자인", "design", "컬러", "아트", "비주얼", "스타일"],
    "PM":          ["pm", "일정", "관리", "리더", "총괄", "조율", "로드맵"],
}


def tokens(text):
    return [t.lower() for t in _TOKEN.findall(text or "")]


def _norm(values):
    """후보 점수 리스트를 0~1로(최댓값 기준). 모두 0이면 그대로 0."""
    m = max(values) if values else 0
    if m <= 0:
        return [0.0] * len(values)
    return [v / m for v in values]


def score_candidates(query, candidates):
    """후보 에이전트에 점수·근거를 부여해 내림차순 리스트로.

    candidates: [{bot_id, name, role, is_leader, event_count,
                  distill_count, experience_count, criteria}]
    반환: 같은 dict + {score, reasons{role_match, keyword_overlap, expertise, track_record}}
          (criteria 원문은 응답에서 제외 — 점수 계산에만 사용)
    """
    q = set(tokens(query))
    raw = []
    for c in candidates:
        role = c.get("role") or ""
        role_tok = set(tokens(role))
        hints = set(ROLE_HINTS.get(role, []))
        # 역할 적합: 질의 토큰이 직군명과 겹치면 ×2(직접 지명), 힌트와 겹치면 ×1(의미 추론).
        role_hit = len(q & role_tok) * 2 + len(q & hints)
        # 직무기준(증류된 루브릭) 키워드 중복 — 실제 역량 텍스트와의 의미 적합.
        crit_tok = set(tokens(c.get("criteria") or ""))
        kw_hit = len(q & crit_tok)
        # 증류 역량: 수면 증류 누적 + 미증류 원석 경험(절반 가중).
        exp = (c.get("distill_count") or 0) + 0.5 * (c.get("experience_count") or 0)
        track = c.get("event_count") or 0
        raw.append((role_hit, kw_hit, exp, track))

    rn = _norm([r[0] for r in raw])
    kn = _norm([r[1] for r in raw])
    en = _norm([r[2] for r in raw])
    tn = _norm([r[3] for r in raw])

    out = []
    for c, (ro, kw, ex, tr) in zip(candidates, zip(rn, kn, en, tn)):
        reasons = {
            "role_match": round(W_ROLE * ro, 4),
            "keyword_overlap": round(W_KW * kw, 4),
            "expertise": round(W_EXP * ex, 4),
            "track_record": round(W_TRACK * tr, 4),
        }
        out.append({
            "bot_id": c.get("bot_id"),
            "name": c.get("name") or "",
            "role": c.get("role") or "",
            "is_leader": bool(c.get("is_leader")),
            "event_count": c.get("event_count") or 0,
            "distill_count": c.get("distill_count") or 0,
            "experience_count": c.get("experience_count") or 0,
            "score": round(sum(reasons.values()), 4),
            "reasons": reasons,
        })
    out.sort(key=lambda x: (x["score"], x["event_count"]), reverse=True)
    return out
