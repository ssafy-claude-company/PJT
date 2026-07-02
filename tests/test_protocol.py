"""재구현 ① 검증: 구조화 메시지 프로토콜 (docs Discord.md 포맷)."""
from organt_core.protocol import (
    Kind,
    Request,
    Response,
    TaskStatus,
    format_request,
    format_response,
    format_task_status,
    parse,
)

A, B = 1001, 2002


# --- 포맷 ---

def test_request_포맷_docs형식():
    s = format_request(B, Kind.WORK, "보고서 완성")
    assert s.splitlines() == ["[Request]", f"To: <@{B}>", "Kind: Work", "Body: 보고서 완성"]


def test_response_포맷_docs형식():
    assert format_response("완료") == "[Response]\nBody: 완료"


def test_task_status_블록_docs형식():
    ts = TaskStatus(task_id="001", purpose="ToDo앱", status="진행", goal="CRUD 동작",
                    group=[("@A", "leader"), ("@B", "dev")], result=None)
    out = format_task_status(ts)
    assert out.startswith("[Task-001]")
    assert "Purpose: ToDo앱" in out and "Status: 진행" in out and "Goal: CRUD 동작" in out
    assert "- @A: leader" in out and "- @B: dev" in out
    assert "result" not in out  # 종료 전엔 result 없음


def test_task_status_result_종료시():
    ts = TaskStatus(task_id="1", result="완수")
    assert "- result: 완수" in format_task_status(ts)


# --- 파싱 (왕복) ---

def test_request_왕복():
    content = format_request(B, Kind.WORK, "보고서 완성")
    msg = parse(message_id=555, author_id=A, mention_ids=[B], reply_to_id=None, content=content)
    assert isinstance(msg, Request)
    assert msg.to_id == B and msg.from_id == A and msg.kind == Kind.WORK
    assert msg.body == "보고서 완성" and msg.message_id == "555"


def test_info_kind_파싱():
    content = format_request(B, Kind.INFO, "진행상황?")
    msg = parse(message_id=1, author_id=A, mention_ids=[B], reply_to_id=None, content=content)
    assert isinstance(msg, Request) and msg.kind == Kind.INFO


def test_멀티라인_본문_전체전달():
    """여러 줄 + '['로 시작하는 줄을 포함한 본문이 첫 줄에서 잘리지 않고 통째로 전달돼야 한다."""
    body = ("버그 2개 고쳐줘.\n\n[버그1 — 무적 회전] 제자리 회전 시 무적이 됨.\n"
            "[버그2 — 먹이 섭취] 커지면 입이 안 커짐.\n끝.")
    content = format_request(B, Kind.WORK, body)
    msg = parse(message_id=9, author_id=A, mention_ids=[B], reply_to_id=None, content=content)
    assert isinstance(msg, Request)
    assert "[버그1 — 무적 회전]" in msg.body and "[버그2 — 먹이 섭취]" in msg.body   # '[' 줄도 보존
    assert "제자리 회전 시 무적" in msg.body and "입이 안 커짐" in msg.body and "끝." in msg.body
    assert msg.to_id == B and msg.kind == Kind.WORK


def test_멀티라인_응답_본문_전체전달():
    body = "분석 결과:\n- 항목1\n- 항목2\n[주의] 후속 필요"
    content = format_response(body)
    msg = parse(message_id=2, author_id=B, mention_ids=[], reply_to_id=555, content=content)
    assert isinstance(msg, Response) and "[주의] 후속 필요" in msg.body and "- 항목2" in msg.body


def test_response_왕복():
    content = format_response("결과 보고합니다")
    msg = parse(message_id=777, author_id=B, mention_ids=[], reply_to_id=555, content=content)
    assert isinstance(msg, Response)
    assert msg.body == "결과 보고합니다" and msg.replies_to == "555" and msg.from_id == B


def test_Response블록이라도_reply아니면_Response아님():
    msg = parse(message_id=1, author_id=A, mention_ids=[], reply_to_id=None,
                content="[Response]\nBody: x")
    assert not isinstance(msg, Response)


def test_일반_메시지는_None():
    assert parse(message_id=1, author_id=A, mention_ids=[B], reply_to_id=None,
                 content="그냥 잡담") is None
