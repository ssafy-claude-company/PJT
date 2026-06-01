"""기능12 검증: 베턴 + 요청 스택 상태기계 (A→B→C 역순 close → 시작점 복귀)."""
import pytest

from src.communication import CommError, CommunicationManager

A, B, C = 1, 2, 3


def test_단일_요청_응답_후_시작점_종료():
    m = CommunicationManager(A)
    m.request(A, B, "r1")
    assert m.is_alive(B) and not m.is_alive(A)   # receiver wake, sender sleep
    m.respond(B)
    assert m.done and m.is_alive(A) and m.open_requests == []


def test_3단_요청후_역순_close_시작점복귀():
    m = CommunicationManager(A)
    m.request(A, B, "r1")   # 활성 B
    m.request(B, C, "r2")   # 활성 C
    assert m.is_alive(C) and len(m.open_requests) == 2

    m.respond(C)            # close r2(B→C) → 활성 B
    assert m.is_alive(B) and len(m.open_requests) == 1 and not m.done
    m.respond(B)            # close r1(A→B) → 시작점 A 복귀, 종료
    assert m.done and m.is_alive(A) and m.open_requests == []

    # 역순(C→B→A)으로 닫혔는지: respond 이벤트 순서 r2 먼저, r1 나중
    responds = [h for h in m.history if h[0] == "respond"]
    assert responds[0][3] == "r2" and responds[1][3] == "r1"


def test_활성아닌_Organt는_요청불가():
    m = CommunicationManager(A)
    m.request(A, B, "r1")   # 활성 B
    with pytest.raises(CommError):
        m.request(A, C, "x")  # A는 자고 있음 → 불가


def test_활성아닌_Organt는_응답불가():
    m = CommunicationManager(A)
    m.request(A, B, "r1")   # 활성 B
    with pytest.raises(CommError):
        m.respond(A)          # 활성은 B인데 A가 응답 시도


def test_열린요청_없으면_응답불가():
    m = CommunicationManager(A)
    with pytest.raises(CommError):
        m.respond(A)


def test_종료후_추가요청_불가():
    m = CommunicationManager(A)
    m.request(A, B, "r1")
    m.respond(B)
    assert m.done
    with pytest.raises(CommError):
        m.request(A, B, "r2")
