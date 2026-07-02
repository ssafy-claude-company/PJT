"""[Core util] 도구·Rule이 공유하는 표현·디버그·반응 유틸 — Rule 로직이 아니라 횡단 관심사.
guide_tools와 rule/ 양쪽이 *순환의존 없이* 여기서 import(종전 guide_tools에 흩어졌던 것 중립화)."""
import os


_DEBUG = bool(os.environ.get("ORGANT_DEBUG"))


def _dbg(msg):
    """진단 로그(기본 off). ORGANT_DEBUG 설정 시에만 stdout으로."""
    if _DEBUG:
        print(msg, flush=True)


def _ok(text):
    return {"content": [{"type": "text", "text": text}]}


async def _react(g, channel_id, message_id, emoji):
    """이모지 반응(상태 표시). Guide에 react가 없으면(테스트 등) 조용히 건너뜀."""
    fn = getattr(g, "react", None)
    if fn:
        await fn(channel_id, message_id, emoji)


def _speech_clip(s, n=1500) -> str:
    """발언 안전망: 폭주만 막고 **침묵 절단하지 않는다** — 잘리면 잘렸다고 표기한다.
    종전의 하드컷([:300]/[:400])은 '3~5줄' 지시를 지킨 발언(한국어 200~400자+)까지 단어
    중간에서 잘랐다(라이브: 회의 발언 전원이 307~308자로 박제, "…프론트엔"에서 끊김 — 사용자
    관측). 더 나쁜 건 회의록도 잘려 **다음 발언자들이 서로의 잘린 주장을 보고 토론**한 것 —
    분량 통제는 지시(프롬프트)와 모델 판단의 몫이고, 시스템은 안전망만 친다."""
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + f" …(발언 {len(s)}자 — {n}자 안전망에서 잘림)"


def _looks_transient(text: str) -> bool:
    """동료 응답이 일시적 API 오류로 보이는지 — 그렇다면 답으로 취급하지 말고 재시도."""
    t = (text or "").strip().lower()
    return t.startswith("api error") or t.startswith("(동료 처리 중 오류")
