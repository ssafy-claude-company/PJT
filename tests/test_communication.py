"""기능11 검증: Request/Response Discord 메시지 인코딩/디코딩."""
from src.communication import (
    Request,
    Response,
    format_request,
    format_response,
    parse_message,
)

A, B = 1001, 2002


def test_request_포맷에_멘션과_태그():
    s = format_request(B, "work", "보고서 만들어줘")
    assert s.startswith("[REQ:work]") and f"<@{B}>" in s and "보고서" in s


def test_request_왕복():
    content = format_request(B, "work", "보고서 만들어줘")
    msg = parse_message(message_id=555, author_id=A, mention_ids=[B],
                        reply_to_id=None, content=content)
    assert isinstance(msg, Request)
    assert msg.from_id == A and msg.to_id == B and msg.kind == "work"
    assert msg.text == "보고서 만들어줘" and msg.request_id == "555"


def test_response_왕복():
    content = format_response("accept", "완료했습니다")
    msg = parse_message(message_id=777, author_id=B, mention_ids=[],
                        reply_to_id=555, content=content)
    assert isinstance(msg, Response)
    assert msg.from_id == B and msg.replies_to == "555"
    assert msg.result == "accept" and msg.text == "완료했습니다"


def test_RESP태그라도_reply아니면_Request나_None():
    # [RESP:..] 인데 reply가 아니면 Response로 보지 않는다
    msg = parse_message(message_id=1, author_id=A, mention_ids=[],
                        reply_to_id=None, content="[RESP:accept] 어쩌고")
    assert not isinstance(msg, Response)


def test_일반메시지는_None():
    assert parse_message(message_id=1, author_id=A, mention_ids=[B],
                         reply_to_id=None, content="그냥 잡담") is None


def test_멘션없는_요청은_to_None():
    msg = parse_message(message_id=2, author_id=A, mention_ids=[],
                        reply_to_id=None, content="[REQ:work] 누구에게?")
    assert isinstance(msg, Request) and msg.to_id is None
