"""기능13 검증: busy 가드(증명②) + 상신(증명③) + Accept/Redo."""
import pytest

from src.communication import CommError, CommunicationManager, RedoLimitExceeded

A, B, C, D = 1, 2, 3, 4


# --- busy 가드 (증명②) ---

def test_busy_Organt에_Work요청_거부():
    m = CommunicationManager(A)
    m.request(A, B, "r1")        # 활성 B, 참여 {A,B}
    m.request(B, C, "r2")        # 활성 C, 참여 {A,B,C}
    assert m.is_busy(B) and m.is_busy(A)
    with pytest.raises(CommError):
        m.request(C, B, "x")     # B는 미완 Work 보유 → 거부


def test_새_Organt에는_요청가능():
    m = CommunicationManager(A)
    m.request(A, B, "r1")
    m.request(B, C, "r2")
    assert not m.is_busy(D)
    f = m.request(C, D, "r3")    # D는 신규 → OK
    assert f.to_id == D and m.is_alive(D)


# --- 상신 (증명③) ---

def test_B가_멈추면_상신되어_교착없이_종료():
    m = CommunicationManager(A)
    m.request(A, B, "r1")        # 활성 B
    m.escalate("B 타임아웃")       # B 멈춤 → 강제 close + 상신
    assert m.done and m.is_alive(A) and m.escalated_to_origin


def test_중간Organt_멈춤_깊은체인_상신():
    m = CommunicationManager(A)
    m.request(A, B, "r1")
    m.request(B, C, "r2")        # 활성 C
    m.respond(C)                 # 활성 B (B가 A에 응답해야)
    m.escalate("B 멈춤")          # B 멈춤 → A→B 강제 close, A로 상신
    assert m.done and m.is_alive(A) and m.escalated_to_origin
    assert "B 멈춤" in m.escalations[-1][1]


# --- Accept / Redo ---

def test_redo_한계내_재요청_후_초과시_상신신호():
    m = CommunicationManager(A, redo_limit=2)
    m.request(A, B, "r1")        # 활성 B
    m.request(B, C, "r2")        # 활성 C
    m.respond(C, "redo")         # 활성 B (B 불만족)
    m.redo(B, C, "r2a")          # redo 1 → 활성 C
    m.respond(C, "redo")
    m.redo(B, C, "r2b")          # redo 2 → 활성 C
    m.respond(C, "redo")
    with pytest.raises(RedoLimitExceeded):
        m.redo(B, C, "r2c")      # redo 3 > 2 → 상신 필요


def test_accept_응답은_정상_close():
    m = CommunicationManager(A)
    m.request(A, B, "r1")
    f = m.respond(B, "accept", "완료")
    assert m.done and f.request_id == "r1"
