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


# ── 상류 선행작업 되감기(report_up_to) — 임의 깊이·임의 대상 일반형 ──────────────────
# A→B→C에서 C가 A에게 Work를 요청 = 선행작업 미완 신호 → 막다른 거부 대신 보고체계로 되감는다.
# 하드코딩(A-B/A-B-C-D 고정) 없음을 깊이·대상을 바꿔가며 증명한다.

def test_상류보고_루트까지_3단():
    """A→B→C에서 C가 A(루트)로 되감기 → alive=A·종료, 서브체인 A→B→C 보존(owner→…→reporter)."""
    m = CommunicationManager(A)
    m.request(A, B, "r1")
    m.request(B, C, "r2")
    assert m.is_alive(C)
    sub = m.report_up_to(C, A, "A 선행작업 필요")
    assert m.is_alive(A) and m.done                       # 루트까지 되감김 → 시작점 복귀
    assert [(s["from"], s["to"]) for s in sub] == [(A, B), (B, C)]


def test_상류보고_부분되감기_중간주인_5단():
    """A→B→C→D→E에서 E가 B(중간)에게 보고: C·D relay, alive=B, A→B 유지(부분 되감기), 서브체인 보존."""
    D, E = 4, 5
    m = CommunicationManager(A)
    for frm, to, r in [(A, B, "r1"), (B, C, "r2"), (C, D, "r3"), (D, E, "r4")]:
        m.request(frm, to, r)
    assert m.is_alive(E) and len(m.open_requests) == 4
    sub = m.report_up_to(E, B, "B 선행작업 필요")
    assert m.is_alive(B) and not m.done                   # 중간 주인 → 부분 되감기(흐름 안 끝남)
    assert [(f.from_id, f.to_id) for f in m.open_requests] == [(A, B)]   # owner 위 프레임만 남음
    assert [(s["from"], s["to"]) for s in sub] == [(B, C), (C, D), (D, E)]


def test_상류보고_루트까지_5단():
    """A→B→C→D→E에서 E가 A(루트)에게 보고 → 끝까지 되감김(alive=A·종료), 전체 경로 보존."""
    D, E = 4, 5
    m = CommunicationManager(A)
    for frm, to, r in [(A, B, "r1"), (B, C, "r2"), (C, D, "r3"), (D, E, "r4")]:
        m.request(frm, to, r)
    sub = m.report_up_to(E, A)
    assert m.is_alive(A) and m.done and m.open_requests == []
    assert [(s["from"], s["to"]) for s in sub] == [(A, B), (B, C), (C, D), (D, E)]


def test_상류보고_비상류_대상은_거부():
    """보고 대상이 상류(ancestor)가 아니면 거부 — 아무 동료에게나 '되감기' 불가(되감기는 위로만)."""
    D = 4
    m = CommunicationManager(A)
    m.request(A, B, "r1")
    m.request(B, C, "r2")            # alive=C, ancestors={A,B}
    with pytest.raises(CommError):
        m.report_up_to(C, D)        # D는 상류가 아님


def test_상류보고_활성아닌_보고자_거부():
    """활성(베턴 보유)인 워커만 상류 보고 가능 — 자고 있는 동료는 보고 못 함."""
    m = CommunicationManager(A)
    m.request(A, B, "r1")
    m.request(B, C, "r2")            # alive=C
    with pytest.raises(CommError):
        m.report_up_to(B, A)        # 활성은 C인데 B가 보고 시도


# ── 정밀 복구: 체인 내부 복원(restore_chain) — 평탄화 없이 가장 깊은 워커부터 재개 ──────────
# 끊긴 A→B→C를 채팅 재발행 없이 스택으로 복원 → 가장 깊은 C부터 재개 → 끝나면 C→B→A 자연 unwind.

def test_정밀복구_체인내부복원_가장깊은워커재개_자연unwind():
    """[정밀 복구 — 내부 상태 복원] active_chain(A→B→C→D)을 채팅 재발행 없이 스택으로 복원하고 가장
    깊은 D부터 재개. 끝나면 respond가 D→C→B→A로 자연 unwind — 각자 범위 보존(평탄화로 중간 안 빼먹음)."""
    D = 4
    m = CommunicationManager(A)
    frames = [                                      # 위→아래 순(active_chain 형태)
        {"from": A, "to": B, "kind": "work", "body": "A→B 원문"},
        {"from": B, "to": C, "kind": "work", "body": "B→C 원문"},
        {"from": C, "to": D, "kind": "work", "body": "C→D 원문"},
    ]
    deepest = m.restore_chain(frames)
    assert deepest == D and m.is_alive(D)           # 가장 깊은 워커부터 재개(리더→C 평탄화 아님)
    assert len(m.open_requests) == 3 and not m.done  # 체인 그대로 복원
    assert m.open_requests[-1].body == "C→D 원문"    # 끊긴 그 깊이의 원문 보존
    # 끝났을 때 자연 unwind — 각자 범위 보존
    m.respond(D); assert m.is_alive(C) and not m.done   # D 완료 → C가 통합(C 범위)
    m.respond(C); assert m.is_alive(B) and not m.done   # C 완료 → B가 통합(B 범위)
    m.respond(B); assert m.is_alive(A) and m.done       # B 완료 → A 복귀·종료


def test_정밀복구_체인내부복원_종료흐름은거부():
    """종료된 흐름엔 체인 복원 불가(유령 복원 차단)."""
    m = CommunicationManager(A)
    m.request(A, B, "r1"); m.respond(B)
    assert m.done
    with pytest.raises(CommError):
        m.restore_chain([{"from": A, "to": B, "kind": "work", "body": "x"}])
