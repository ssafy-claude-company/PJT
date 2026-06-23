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


def test_미완Task_프로젝트는_부팅복구가_이어서_재개():
    """[복구 갭 — 사용자 지적 2026-06-13] 프로젝트 채널 평문 개입이 부분 처리된 채(봇 응답이 달려
    find_pending_request가 '완료'로 보고 못 잡음) 동면하면, open_task가 남은 등록 프로젝트는 그 채널
    개입으로 이어 재개한다 — 졸업 라우팅이 main 출신에만 해주던 'open_task 이어가기'를 동일 적용
    (라이브: '게임성 고도화' 개입이 복구에서 누락→사용자 수동 재전송). 이미 복구 큐에 든 채널(졸업
    등 중복)·메인 채널·open_task 없는(완료된) 프로젝트는 제외(이중 발사·유령 재개 방지)."""
    from src.main import projects_to_resume
    projects = {
        500: {"id": "P-010", "channel": 500, "leader": 12, "purpose": "게임", "open_task": {"task_id": "1"}},
        501: {"id": "P-012", "channel": 501, "leader": 13, "purpose": "웹", "open_task": {"task_id": "2"}},
        502: {"id": "P-002", "channel": 502, "leader": 14, "open_task": None},   # 완료 → 제외
        700: {"id": "P-MAIN", "channel": 700, "leader": 11, "open_task": {"task_id": "3"}},  # 메인 → 제외
    }
    out = projects_to_resume(projects, already_channels={501}, main_channel=700)   # 501=이미 졸업 라우팅으로 큐
    assert {p["id"] for p in out} == {"P-010"}     # P-012(이미 큐)·P-002(완료)·P-MAIN(메인) 모두 제외
    assert projects_to_resume({}, set(), 700) == []
    assert projects_to_resume(None, set(), 700) == []   # 레지스트리 부재에도 안전


def test_복구_이어가기_본문은_조기완료_새Task_금지_명시():
    """[복구 충돌 교정 — 사용자 지적 2026-06-14] 미완 Task 복원 복구에서 원요청을 그대로 재발사하면 리더가
    복원 Task를 조기 완료하고 새 Task를 연다(라이브 054013-1 조기완료→074010-1 신설, "기존 안 끝났는데 새로
    열림"). resume_continue_body는 원요청을 보존하면서 앞에 '새 Task 금지·복원 Task 이어서 완성·조기
    complete 금지'를 명시해 그 사고를 막는다."""
    from src.main import resume_continue_body
    out = resume_continue_body("게임성을 보완해줘")
    assert "게임성을 보완해줘" in out                 # 원요청 보존(시스템이 말 지어내지 않음)
    assert "이어가기" in out and "새 Task" in out      # 새 Task 금지·이어가기 명시
    assert "complete" in out.lower()                   # 조기 complete 금지
    assert resume_continue_body("") and resume_continue_body(None)   # 빈/None에도 안전(크래시 없음)


def test_좀비부활차단_자동1회재개후_사용자무활동이면_재부활_안함():
    """[좀비 부활 차단 — 라이브 P-019] 사용자가 돌아오지 않은(후속 0) 미완 프로젝트가 매 동면해동마다
    되살아나 공유 전문가(유일 AI 엔지니어)를 점유해 *활성 요청을 굶기던* 것 차단('P-19 하나만 돌렸는데
    P-13에 막힘'의 정체 = P-013 좀비 부활). 같은 미완 Task를 이미 한 번 자동 재개했으면
    (recovery_attempted==task_id) 자동 재개에서 제외 — 단 새 Task(task_id 다름)는 정상 재개(좀비 아님)."""
    from src.main import projects_to_resume
    projects = {
        500: {"id": "P-013", "channel": 500, "leader": 12, "open_task": {"task_id": "022539-1"},
              "recovery_attempted": "022539-1"},                      # 1회 자동재개+무활동+유휴 → 좀비, 제외
        501: {"id": "P-019", "channel": 501, "leader": 13, "open_task": {"task_id": "165413-1"}},  # 미시도 → 재개
        502: {"id": "P-020", "channel": 502, "leader": 14, "open_task": {"task_id": "NEW-2"},
              "recovery_attempted": "OLD-1"},                          # 새 Task(다른 id) → 재개(좀비 아님)
        # [컨테이너 죽음 보존(2026-06-23, 사용자: P-031 정지)] 가드 박혔어도 *진행 중 체인(depth>0)*이면 재개
        # — 한 부팅에 한 프로젝트만 깨우고 다른 활성 프로젝트가 영영 정지하던 라이브 P-030/P-031 교정.
        503: {"id": "P-031", "channel": 503, "leader": 15,
              "open_task": {"task_id": "175548-1", "active_chain": [{"from": 15, "to": 16, "body": "x"}]},
              "recovery_attempted": "175548-1"},                       # 가드+진행중(depth>0) → 재개(좀비 아님)
    }
    out = {p["id"] for p in projects_to_resume(projects, already_channels=set(), main_channel=700)}
    assert out == {"P-019", "P-020", "P-031"}                         # P-013(유휴 좀비)만 제외, P-031(진행중)은 재개


def test_수렴경보_파킹_Task는_자동재개_제외_S1a():
    """[수렴 경보 = 파킹] loop_escalated(사람 판정 대기) Task는 진행 중 체인(active_chain)이 있어도 자동
    부팅 재개에서 제외한다 — 라이브 P-031: 워커가 15GB로 부풀어 머신을 전역 OOM시키는 메모리폭탄이라
    자동 재개=스택 전체 위협(OOM킬러가 리스너까지). 사람이 채널에 글을 쓰면(개입) 경보가 풀려 정상 재개."""
    from src.main import projects_to_resume
    projects = {
        500: {"id": "P-031", "channel": 500, "leader": 12,
              "open_task": {"task_id": "180515-1", "loop_escalated": True,
                            "active_chain": [{"from": 12, "to": 13, "body": "x"}]}},   # 경보+진행중 → 파킹(제외)
        501: {"id": "P-040", "channel": 501, "leader": 14,
              "open_task": {"task_id": "9", "loop_escalated": False}},                  # 정상 미완 → 재개
    }
    out = {p["id"] for p in projects_to_resume(projects, already_channels=set(), main_channel=700)}
    assert out == {"P-040"}                                            # 경보 파킹(P-031)은 제외, 정상(P-040)만 재개
