"""부팅 복구 판정(find_pending_request) — 메인·프로젝트 채널 공용 순수 함수.

리스너가 흐름 도중 죽어도 재시작 시 '아직 [Response]가 안 달린 마지막 사용자 [Request]'를
다시 처리한다. 메인 채널만 스캔하던 구멍(프로젝트 채널 개입 요청이 재시작에 유실)을 메우며
모든 채널이 같은 판정을 쓰도록 분리했다.
"""
from src.main import find_pending_request
from src.protocol import Kind, Request, Response


def _req(mid, frm=999, to=11):
    return Request(to_id=to, kind=Kind.WORK, body="b", from_id=frm, message_id=str(mid))


def test_미응답_사용자요청은_복구대상():
    assert find_pending_request([_req(1)], {11, 22}).message_id == "1"


def test_Response가_달리면_완료로_해제():
    msgs = [_req(1), Response(body="done", from_id=11, replies_to="1")]
    assert find_pending_request(msgs, {11}) is None


def test_봇이_보낸_Request는_무시():
    assert find_pending_request([_req(1, frm=11)], {11}) is None


def test_응답후_새요청은_그것만_복구():
    msgs = [_req(1), Response(body="ok", from_id=11, replies_to="1"), _req(2)]
    assert find_pending_request(msgs, {11}).message_id == "2"
