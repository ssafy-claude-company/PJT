"""부팅 복구 판정(find_pending_request) — 메인·프로젝트 채널 공용 순수 함수.

리스너가 흐름 도중 죽어도 재시작 시 '아직 [Response]가 안 달린 마지막 사용자 [Request]'를
다시 처리한다. 메인 채널만 스캔하던 구멍(프로젝트 채널 개입 요청이 재시작에 유실)을 메우며
모든 채널이 같은 판정을 쓰도록 분리했다.
"""
from src.main import find_pending_request, graduated_project
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


def test_졸업한_원요청은_프로젝트로_판정된다():
    """[졸업 라우팅] 원요청이 이미 등록 프로젝트로 졸업했으면(origin_msg 일치) 복구는 그 프로젝트를
    돌려받아 '재발사 대신 프로젝트 채널 개입'으로 잇는다 — 라이브 P-009: 동면 복구가 원요청을
    재발사해 등록·채용·산출물이 있는 진행을 버리고 새 스코프로 처음부터 다시 시작(사용자 지적)."""
    projects = {500: {"id": "P-009", "channel": 500, "leader": 11,
                      "origin_msg": "1", "open_task": {"task_id": "065442-1"}}}
    assert graduated_project(projects, "1")["id"] == "P-009"
    assert graduated_project(projects, 1)["id"] == "P-009"      # int/str 메시지 id 모두 매칭
    assert graduated_project(projects, "2") is None             # 무관한 요청은 기존 경로(재발사)
    assert graduated_project({}, "1") is None
    assert graduated_project({500: {"id": "P-001"}}, "1") is None   # origin 미기록(구세대 등록) → 기존 경로
