"""재구현 검증(P2P 모델): Guide 도구 + 베턴 wake + 단일흐름."""
import asyncio

from src.guide_tools import Flow, make_guide_tools
from src.protocol import Kind
from src.sys_core import Sys


class FakeGuide:
    def __init__(self):
        self.calls = []

    async def post(self, ch, sender, content, reply_to=None):
        self.calls.append(("post", ch, sender, content))
        return "m1"

    async def create_project_channel(self, gid, name):
        self.calls.append(("create_channel", name))
        return 9001

    async def open_task(self, ch, status):
        self.calls.append(("open_task", ch, status.purpose))
        return "blk", "thr"

    async def update_status(self, ch, blk, status):
        self.calls.append(("update", status.status))
        return blk

    async def send_request(self, thr, sender, to, kind, body):
        self.calls.append(("req", sender, to, body))
        return "reqid"

    async def send_response(self, thr, sender, req, body):
        self.calls.append(("resp", sender, body))
        return "respid"


def _flow(g, leader=11):
    f = Flow(g, channel_id=500, guild_id=1, leader_id=leader, bot_info={11: "L", 12: "M"})
    f.start_root("root")
    return f


def _tools(f, me, role):
    return {t.name: t for t in make_guide_tools(f, me, role)}


def test_서브프로세스_사망_143은_일시오류로_재시도대상():
    """SDK 서브프로세스가 SIGTERM(143)/파이프끊김으로 죽으면 일시오류로 보고 resume 재시도해야 한다
    — 작업이 끝났는데 마무리 메시지만 깨져 에러가 최종 응답으로 올라오는 일 방지."""
    from src.organt import _is_transient_api_error
    assert _is_transient_api_error("API Error: Command failed with exit code 143 (exit code: 143)")
    assert _is_transient_api_error("API Error: Fatal error in message reader")
    assert _is_transient_api_error("API Error: 529 overloaded")
    assert not _is_transient_api_error("배포 완료. 라이브 URL: https://x")   # 정상 응답은 재시도 아님
    assert not _is_transient_api_error("API Error: invalid request 400")    # 비일시 오류는 재시도 아님


def test_member는_request_recruit_run():
    f = _flow(FakeGuide())
    assert {t.name for t in make_guide_tools(f, 12, "member")} == {"request", "recruit", "run"}


def test_leader는_project_task_도구():
    f = _flow(FakeGuide())
    names = {t.name for t in make_guide_tools(f, 11, "leader")}
    # 보고/답변 툴 없음(반환=Response). 흐름 도구(request·recruit·run)+리더 셋업·배포 도구.
    assert names == {"request", "recruit", "run",
                     "create_project", "create_task", "set_goal", "complete_task", "deploy"}


def test_리더_등록툴이_전부_허용목록에_있음():
    """make_guide_tools(leader)가 등록한 모든 guide 툴은 허용목록(FLOW_TOOLS+LEADER_TOOLS)에도 있어야 한다.
    등록만 되고 allowed_tools에서 빠지면 런타임에 권한거부된다(set_goal 누락 사고 재발 방지)."""
    from src.guide_tools import FLOW_TOOLS, LEADER_TOOLS
    f = _flow(FakeGuide())
    names = {t.name for t in make_guide_tools(f, 11, "leader")}
    allowed = set(FLOW_TOOLS) | set(LEADER_TOOLS)
    missing = {n for n in names if f"mcp__guide__{n}" not in allowed}
    assert not missing, f"허용목록(FLOW_TOOLS+LEADER_TOOLS)에서 빠진 리더 툴: {missing}"


def test_run_안전가드():
    f = _flow(FakeGuide())
    rt = {t.name: t for t in make_guide_tools(f, 11, "leader")}["run"]
    f.workspace = None
    assert "작업공간" in asyncio.run(rt.handler({"command": "echo hi"}))["content"][0]["text"]
    f.workspace = "/tmp"
    assert "거부" in asyncio.run(rt.handler({"command": "rm -rf /tmp/x"}))["content"][0]["text"]
    assert "거부" in asyncio.run(rt.handler({"command": "git commit -am x"}))["content"][0]["text"]


def test_run_파일작성_백도어_차단():
    """run으로 파일 작성(heredoc·cat>·tee)은 막힌다 — 산출물 작성은 Write/Edit로(권한·협의 게이트·기록 적용).
    이 백도어가 열려 있으면 리더가 위임 없이 전부 혼자 찍어내 독점하거나 협의 중 선구현이 가능했다."""
    f = _flow(FakeGuide())
    f.workspace = "/tmp"
    rt = {t.name: t for t in make_guide_tools(f, 12, "member")}["run"]
    for cmd in ("cat > server.js << 'EOF'\nx\nEOF", "echo hi | tee app.js", "cat>x.js"):
        out = asyncio.run(rt.handler({"command": cmd}))["content"][0]["text"]
        assert "거부" in out and "Write/Edit" in out, cmd
    ok = asyncio.run(rt.handler({"command": "echo built"}))["content"][0]["text"]   # 정상 실행은 통과
    assert "거부" not in ok and "built" in ok


def test_run_백그라운드_프로세스_그룹째_정리():
    """run이 백그라운드로 띄운 자식(서버 등)을 끝나면 그룹째 정리 → 포트/프로세스 누수 없음."""
    import os
    import time as _t
    f = _flow(FakeGuide())
    f.workspace = "/tmp"
    rt = {t.name: t for t in make_guide_tools(f, 11, "leader")}["run"]
    # 마커는 작업공간 내 상대경로로 기록(절대경로 '> /' 리다이렉트는 안전가드가 차단).
    name = f"organt_runtest_{os.getpid()}.pid"
    marker = f"/tmp/{name}"
    # 백그라운드로 오래 자는 자식을 띄우고 그 PID를 기록 → run 반환 뒤엔 죽어 있어야 함.
    out = asyncio.run(rt.handler({"command": f"sleep 30 & echo $! > {name}; echo started"}))
    text = out["content"][0]["text"]
    assert "[exit 0]" in text and "started" in text   # 거부 아닌 실제 실행 확인
    with open(marker) as fp:
        pid = int(fp.read().strip())
    os.remove(marker)

    def _running(p):  # 좀비(Z)는 죽은 것으로 간주 — 자원/포트를 더는 잡지 않음
        try:
            with open(f"/proc/{p}/stat") as fp:
                return fp.read().split(") ", 1)[1].split(" ", 1)[0] != "Z"
        except (FileNotFoundError, ProcessLookupError):
            return False

    for _ in range(40):       # init의 reaping을 잠깐 기다림(최대 ~2s)
        if not _running(pid):
            break
        _t.sleep(0.05)
    assert not _running(pid), f"백그라운드 자식(pid={pid})이 정리되지 않고 누수됨"


def test_팀_배정_recruit_팀밖요청거부():
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "A", 13: "B"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12"}))   # 13 제외 배정
    assert set(f.project_team) == {11, 12}
    asyncio.run(t["create_task"].handler({"purpose": "x", "goal": "g", "members": "12"}))
    assert set(f.current.team) == {11, 12}
    r = asyncio.run(t["request"].handler({"to_id": "13", "kind": "Info", "body": "x"}))
    assert "팀이 아닙니다" in r["content"][0]["text"]          # 팀 밖 → 거부(게시 안 함)
    assert not any(c[0] == "req" for c in g.calls)
    asyncio.run(t["recruit"].handler({"member": "B", "reason": "부족"}))   # 역할명으로 채용
    assert 13 in f.current.team


def test_owner는_work수신자_goal합의후():
    """새 모델(중앙집권 방지): create_task는 Purpose만 — Goal·owner 선배정 없음. Goal은 set_goal로 확정해야
    Work 위임 가능(선분배 금지), 그 Work를 받은 동료가 곧 그 Task의 owner가 된다(수신=소유)."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "A백엔드", 13: "B프론트"})
    f.start_root("root")
    waked = []

    async def wake(to, b, k):
        waked.append(to)
        return "완료"

    f.wake = wake
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12,13"}))
    asyncio.run(t["create_task"].handler({"purpose": "서버", "members": "12,13"}))
    # 선배정 없음: owner·goal 비어 있음 (판 걸 때 분배 안 함)
    assert f.current.owner == 0 and f.current.status.owner == "" and not f.current.status.goal
    # Goal 미확정 상태에서 Work 위임은 거부(선분배 금지)
    blocked = asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "서버 만들어"}))
    assert "Goal" in blocked["content"][0]["text"] and f.current.owner == 0
    assert not any(c[0] == "req" for c in g.calls)               # 거부 → 게시 안 함
    # 팀 합의 결과를 리더가 set_goal로 확정 — 이 Task의 멤버 전원(12,13)을 Info로 물어야 통과(Task별·멤버별)
    f.comm.history.append(("request", 11, 12, "r", Kind.INFO))
    f.comm.history.append(("request", 11, 13, "r", Kind.INFO))
    asyncio.run(t["set_goal"].handler({"goal": "GET/POST /todos 동작"}))
    assert f.current.status.goal == "GET/POST /todos 동작"
    # 이제 Work 위임 → 받은 동료(12)가 owner가 됨 (수신=소유)
    asyncio.run(t["request"].handler({"to_id": "12", "kind": "Work", "body": "서버 만들어"}))
    assert f.current.owner == 12 and "A백엔드" in f.current.status.owner
    assert 12 in waked


def test_set_goal은_Task멤버_전원_의견받은뒤에만_Task별():
    """Goal은 'Task마다 그 담당 팀이 함께' 정한다(docs: Task.Team이 Goal을 정함) — 이 Task 멤버 전원을 Info로
    물은 뒤에만 set_goal 통과. 전역 1회로 끝내는 리더 독단/선지정 차단, Task가 바뀌면 추적도 리셋(Task별)."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "백", 13: "프"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12,13"}))
    asyncio.run(t["create_task"].handler({"purpose": "서버", "members": "12,13"}))
    f.comm.history.append(("request", 11, 12, "r", Kind.INFO))    # 12만 물음
    r1 = asyncio.run(t["set_goal"].handler({"goal": "동작"}))
    assert "거부" in r1["content"][0]["text"] and not f.current.status.goal   # 13 미협의 → 거부
    f.comm.history.append(("request", 11, 13, "r", Kind.INFO))    # 13도 물음
    asyncio.run(t["set_goal"].handler({"goal": "동작"}))
    assert f.current.status.goal == "동작"                         # 전원 협의 → 통과
    # 다음 Task에선 추적 리셋(hist_start) → 이전 협의 재사용 불가
    f.current.verified = True
    asyncio.run(t["complete_task"].handler({"result": "ok"}))
    asyncio.run(t["create_task"].handler({"purpose": "프론트", "members": "13"}))
    r3 = asyncio.run(t["set_goal"].handler({"goal": "화면"}))     # 새 Task에서 13 다시 안 물음
    assert "거부" in r3["content"][0]["text"]                      # Task별로 다시 합의해야 함


def test_create_task_빈껍데기_purpose는_팀이_set_goal로():
    """create_task는 Purpose를 비운 '빈 껍데기'로 연다(리더가 할 일 선지정 금지) — Purpose·Goal은 그 Task
    멤버 협의 후 set_goal(purpose, goal)로 함께 확정된다(분산: 무엇을 풀지도 팀이 정함)."""
    g = FakeGuide()
    f = _flow(g)                                   # leader 11, member 12
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_task"].handler({"members": "12"}))
    assert f.current.status.purpose == "" and f.current.status.goal == ""   # 빈 껍데기(리더 선지정 없음)
    r0 = asyncio.run(t["set_goal"].handler({"purpose": "서버", "goal": "동작"}))
    assert "거부" in r0["content"][0]["text"]                                # 멤버 협의 전엔 거부
    f.comm.history.append(("request", 11, 12, "r", Kind.INFO))               # 팀 회의
    asyncio.run(t["set_goal"].handler({"purpose": "할 일 저장 문제 해결", "goal": "추가·삭제 시나리오 통과"}))
    assert f.current.status.purpose == "할 일 저장 문제 해결"                  # Purpose가 팀 회의로 채워짐
    assert f.current.status.goal == "추가·삭제 시나리오 통과"


def test_continue전_고아베턴_복구():
    """위임 도중 리더 턴이 끝나 베턴이 동료에 굳으면(고아), continue가 리더를 다시 띄우기 전에 베턴을
    리더로 강제 복구한다 — '활성=동료'로 모든 요청이 거부되는 '두 흐름' 버그 방지."""
    import types
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"}, workspace="/ws", max_continue=3)
    calls = []

    async def fake_run_turn(flow, oid, body, kind, role):
        calls.append(role)
        if len(calls) == 1:                        # seg1: 위임 도중 끝남 → 베턴이 동료(12)에 굳음
            flow.current = types.SimpleNamespace(
                task_id="t1", status=types.SimpleNamespace(status="진행", result=None))
            flow.comm.request(11, 12, "leak", Kind.WORK)        # alive→12(고아 프레임)
            return "작업 중 (⚠ 턴 한도 도달 — 미완)"
        assert flow.comm.alive == 11, f"continue 진입 시 베턴이 리더가 아님: {flow.comm.alive}"  # 복구됨
        flow.current = None
        return "완료"

    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(500, 11, "큰 작업", root_id="r"))
    assert len(calls) == 2
    assert any(e["event"] == "baton_recover_continue" and e.get("recovered") for e in s.flow_log)


def test_request_동료_깨우고_베턴복귀():
    g = FakeGuide()
    f = _flow(g)
    waked = []

    async def wake(to, b, k):
        waked.append((to, b, k))
        return f"{b} 처리완료"

    f.wake = wake
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "members": "12"}))
    f.comm.history.append(("request", 11, 12, "r", Kind.INFO))   # 목표 합의 전 팀 Info 협의
    asyncio.run(tools["set_goal"].handler({"goal": "g"}))     # Work 위임은 Goal 확정 후 가능
    res = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "백엔드"}))
    assert len(waked) == 1 and waked[0][0] == 12 and waked[0][2] == Kind.WORK   # 동료 깨움
    assert "백엔드" in waked[0][1] and "Goal: g" in waked[0][1]   # 원 요청 + Goal 계약을 안고 전달
    assert f.comm.alive == 11                        # 응답 후 베턴 복귀
    assert "처리완료" in res["content"][0]["text"]
    assert any(c[0] == "req" for c in g.calls) and any(c[0] == "resp" for c in g.calls)


def test_request_자기자신_거부_게시안함():
    g = FakeGuide()
    f = _flow(g)
    f.wake = lambda *a: None
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "goal": "g"}))
    r = asyncio.run(tools["request"].handler({"to_id": "11", "kind": "Work", "body": "x"}))
    assert "거부" in r["content"][0]["text"]
    assert not any(c[0] == "req" for c in g.calls)   # 검증 실패 → 게시 안 함


def test_단일Task_순차_생성과_완료마감():
    g = FakeGuide()
    f = _flow(g)
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "백엔드", "goal": "API 동작"}))
    # 현재 Task 미완이면 새 Task 거부(단일흐름 — 한 번에 하나, 고아 '진행' 방지)
    blocked = asyncio.run(tools["create_task"].handler({"purpose": "프론트", "goal": "화면 연동"}))
    assert "단일흐름" in blocked["content"][0]["text"] and len(f.tasks) == 1
    # 현재 Task 완료 마감 → 다음 Task 허용 (run 검증돼야 마감 가능 — 허위완료 가드)
    f.current.verified = True
    r = asyncio.run(tools["complete_task"].handler({"result": "백엔드 완료"}))
    assert "완료" in r["content"][0]["text"] and f.current is None
    asyncio.run(tools["create_task"].handler({"purpose": "프론트", "goal": "화면 연동"}))
    assert len(f.tasks) == 2 and f.tasks[0].task_id != f.tasks[1].task_id   # task_id 유니크
    f.current.verified = True
    r2 = asyncio.run(tools["complete_task"].handler({"result": "프론트 완료"}))
    assert f.tasks[1].status.status == "완료" and "프론트 완료" in f.tasks[1].status.result
    assert f.current is None
    # 현재 Task 없으면 request 거부(게시 안 함)
    f.wake = lambda *a: None
    rr = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "x"}))
    assert "진행 중인 Task가 없습니다" in rr["content"][0]["text"]


def test_허위완료_차단_run검증_후에만_마감():
    """run으로 한 번도 검증 안 한 Task는 complete_task 거부(허위완료 차단). run 후엔 허용."""
    f = _flow(FakeGuide())
    f.workspace = "/tmp"
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "백엔드", "goal": "API 동작"}))
    # 실행 전 마감 시도 → 거부(허위완료 금지), Task는 여전히 진행 중
    r = asyncio.run(tools["complete_task"].handler({"result": "다 했어요"}))
    assert "거부" in r["content"][0]["text"] and "실행" in r["content"][0]["text"]
    assert f.current is not None and f.current.status.status != "완료"
    # run으로 실제 실행 → verified=True, 시스템이 영수증(실제 출력) 캡처
    asyncio.run(tools["run"].handler({"command": "echo ok"}))
    assert f.current.verified is True and f.current.run_count == 1 and f.current.evidence
    # 마감 허용 — 결과엔 에이전트 '보고' 옆에 시스템 실행기록(실제 출력)이 떼어낼 수 없게 묶인다
    r2 = asyncio.run(tools["complete_task"].handler({"result": "검증 후 완료"}))
    assert "완료" in r2["content"][0]["text"] and f.current is None
    res = f.tasks[-1].status.result
    assert "검증 후 완료" in res and "시스템 실행기록" in res and "exit=0" in res


def test_close_flow_정상_clean_close():
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"})
    f = _flow(s.guide)                          # comm: [origin→11], alive=11
    s._close_flow(f, 11, "결과")
    assert f.comm.done                          # 리더가 alive → 정상 close


def test_close_flow_비정상베턴_강제드레인():
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"})
    f = _flow(s.guide)
    f.comm.request(11, 12, "leak", Kind.WORK)   # 닫히지 않은 프레임 → alive=12(비정상)
    assert not f.comm.done and f.comm.alive == 12
    s._close_flow(f, 11, "결과")                # 강제 드레인
    assert f.comm.done                          # 교착 없이 종료


def test_프로젝트_등록과_채널개입_라우팅():
    """create_project → 식별번호 등록+채널 앵커. 등록된 채널에 다시 명령 → '개입'으로 라우팅(맥락 유지)."""
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L", 12: "M"}, workspace="/ws")
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "M"})
    f.workspace = "/ws"
    f.register_project = lambda ch, name: s._register_project(ch, name, f.workspace, f.leader)
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "스네이크", "team": "12"}))   # 채널 9001 생성
    pid = s.projects[9001]["id"]
    assert pid.startswith("P-") and s.projects[9001]["workspace"] == "/ws"   # 내부 등록(채널 앵커는 안 박음)

    captured = {}
    async def fake_run_turn(flow, oid, body, kind, role):
        captured["flow"], captured["body"] = flow, body
        return "done"
    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(9001, 11, "즉사 버그 고쳐", root_id=None))   # 등록 채널 명령
    fl = captured["flow"]
    assert fl.intervention and fl.project_id == pid                              # 개입으로 인식
    assert fl.project_channel == 9001 and fl.workspace == "/ws"                  # 기존 맥락 유지
    assert "개입" in captured["body"] and "즉사 버그 고쳐" in captured["body"]
    # 미등록 채널은 일반 신규 흐름(개입 아님)
    asyncio.run(s.handle_user_input(777, 11, "새 일", root_id=None))
    assert captured["flow"].intervention is None and captured["flow"].workspace == "/ws"


def test_프로젝트_레지스트리_영속과_중복방지(tmp_path):
    """레지스트리를 디스크에 영속 → 프로세스가 끝나도 '원래 프로젝트'에 개입 가능. 같은 이름은 재사용."""
    p = str(tmp_path / "projects.json")
    s1 = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"}, projects_path=p)
    pid = s1._register_project(9001, "스네이크", "/ws", 11)
    # 같은 이름은 새 채널이어도 식별번호 '그대로 유지' + 채널만 갱신(번호 증가/중복 금지)
    assert s1._register_project(9999, "스네이크", "/ws2", 11) == pid
    assert 9999 in s1.projects and 9001 not in s1.projects     # 채널만 현재 것으로 이동
    assert s1.projects[9999]["id"] == pid and s1.projects[9999]["workspace"] == "/ws2"
    # 새 프로세스(새 Sys)가 같은 파일 로드 → 갱신된 채널·식별번호 그대로 복원
    s2 = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"}, projects_path=p)
    assert 9999 in s2.projects and s2.projects[9999]["id"] == pid
    assert s2.projects[9999]["workspace"] == "/ws2"


def test_단일흐름_진행중_명령은_큐잉():
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"})
    s.active_flow = Flow(g, 500, 1, 11, {11: "L"})    # 활성(미완) 흐름
    out = asyncio.run(s.handle_user_input(500, 11, "두번째 명령", root_id=None))
    assert out["mode"] == "queued"                    # 버리지 않고 큐에 적재
    assert s.queue and s.queue[0][2] == "두번째 명령"


def test_턴한도_미완이면_같은세션으로_이어서_완료():
    """리더가 턴 한도로 Task를 못 닫고 끝나면 SYS가 이어서 재호출해 완료까지 끌고 간다(中断 아님)."""
    import types
    g = FakeGuide()
    s = Sys(g, guild_id=1, organt_builder=None, bot_info={11: "L"}, workspace="/ws", max_continue=4)
    calls = []

    async def fake_run_turn(flow, oid, body, kind, role):
        calls.append(body)
        if len(calls) == 1:                            # 1차: Task 열어둔 채 턴 한도로 끊김
            flow.current = types.SimpleNamespace(
                task_id="t1", status=types.SimpleNamespace(status="진행", result=None))
            return "작업 중... (⚠ 턴 한도 도달 — 작업이 미완일 수 있음)"
        flow.current = None                            # 2차(이어서): 마감
        return "완료"

    s.run_turn = fake_run_turn
    asyncio.run(s.handle_user_input(500, 11, "큰 작업", root_id=None))
    assert len(calls) == 2 and "이어서 계속" in calls[1]          # 연속 실행 프롬프트로 재호출됨
    assert any(e["event"] == "continue_incomplete" for e in s.flow_log)


def test_새요청마다_세션초기화_앵커링차단(tmp_path):
    """새 최상위 요청 시작 시 organt_state_*.json를 지워 '이미 했다' 앵커링을 구조적으로 막는다."""
    sd = tmp_path
    (sd / "organt_state_11.json").write_text("{}")
    (sd / "organt_state_12.json").write_text("{}")
    s = Sys(FakeGuide(), guild_id=1, organt_builder=None, bot_info={11: "L"},
            workspace="/ws", session_dir=str(sd))

    async def fake_rt(flow, oid, body, kind, role):
        return "done"

    s.run_turn = fake_rt
    asyncio.run(s.handle_user_input(500, 11, "새 요청", root_id=None))
    assert not list(sd.glob("organt_state_*.json"))   # 세션 파일 초기화됨
    assert any(e["event"] == "reset_sessions" for e in s.flow_log)


def test_위임자에게_되묻기는_확인요청반환_에러아님():
    """직속 위임자에게 Info로 되물으면 '재진입 불가' 에러 대신 확인요청을 위임자에게 반환(협업 가능)."""
    g = FakeGuide()
    f = _flow(g)                                       # leader 11, member 12; start_root → alive=11
    tools11 = _tools(f, 11, "leader")
    asyncio.run(tools11["create_task"].handler({"purpose": "p", "goal": "g", "members": "12"}))
    f.comm.request(11, 12, "r1", Kind.WORK)            # 11→12 위임 → alive=12, 12의 직속위임자=11
    tools12 = _tools(f, 12, "member")
    r = asyncio.run(tools12["request"].handler(
        {"to_id": "11", "kind": "Info", "body": "필드명 X 맞나요?"}))
    txt = r["content"][0]["text"]
    assert "확인요청" in txt and "위임자" in txt and "거부" not in txt   # 더는 거부 에러가 아님
    assert f.pending_clarify == {"from": 12, "to": 11, "q": "필드명 X 맞나요?"}


def test_위임자측_확인요청_질문으로_표면화():
    """깨운 동료가 확인요청을 남기고 반환하면, 위임자에게 그 질문이 응답으로 떠올라 답·재위임하게 된다."""
    g = FakeGuide()
    f = _flow(g)
    tools11 = _tools(f, 11, "leader")
    asyncio.run(tools11["create_task"].handler({"purpose": "p", "members": "12"}))
    f.comm.history.append(("request", 11, 12, "r", Kind.INFO))   # 목표 합의 전 팀 Info 협의
    asyncio.run(tools11["set_goal"].handler({"goal": "g"}))   # Work 위임 전 Goal 확정

    async def wake(to, body, kind):                    # 12가 위임자(11)에게 확인요청 남기고 반환했다고 모의
        f.pending_clarify = {"from": 12, "to": 11, "q": "필드명 X 맞나요?"}
        return "(짧게 반환)"

    f.wake = wake
    r = asyncio.run(tools11["request"].handler({"to_id": "12", "kind": "Work", "body": "X 구현"}))
    txt = r["content"][0]["text"]
    assert "확인요청 from" in txt and "필드명 X 맞나요?" in txt   # 질문이 위임자 응답으로 표면화
    assert f.pending_clarify is None                            # 표면화하며 소거


def test_재위임은_Redo로_바운드_정당한첫위임은_허용():
    """docs Communication.md §5: 이미 '완료 응답'까지 받은 산출물을 같은 owner에게 또 Work로 보내면
    '새 위임'이 아니라 Redo(직전 결함 보완)로 처리되고, 한계를 넘으면 거부된다(반사적 중복요청 차단·보완은 허용)."""
    g = FakeGuide()
    f = _flow(g)
    waked = []

    async def wake(to, b, k):
        waked.append((to, b))
        return "완료"

    f.wake = wake
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "members": "12"}))
    f.comm.history.append(("request", 11, 12, "r", Kind.INFO))   # 목표는 팀 합의 산물
    asyncio.run(tools["set_goal"].handler({"goal": "GET/POST /todos 동작"}))
    # 1) 첫 Work 위임(정상) → owner=12, '완료 응답'까지 닫혀 delivered로 기록됨
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))
    assert f.comm.delivered_work(11, 12) and f.current.owner == 12
    # 위임 본문은 Goal을 계약으로 안고 owner에게 전달된다(리더 스펙 리파인이 아니라 목표가 계약)
    assert any("Goal" in b for _, b in waked)
    # 2) 같은 owner에 또 Work × 2 → Redo로 처리(여전히 깨워 '보완' 가능), history에 redo 2건
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "결함A 고쳐"}))
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "결함B 고쳐"}))
    assert sum(1 for ev in f.comm.history if ev[0] == "redo") == 2
    # 3) 한계(2) 초과 → 거부(반복 위임 차단), 동료를 더 깨우지 않음
    n_before = len(waked)
    r = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "또 고쳐"}))
    assert "한도" in r["content"][0]["text"] and len(waked) == n_before
    # 4) 새 Task를 열면 추적이 초기화 → 같은 동료라도 다시 '첫 위임'(다른 산출물)
    f.current.verified = True
    asyncio.run(tools["complete_task"].handler({"result": "ok"}))
    asyncio.run(tools["create_task"].handler({"purpose": "p2", "members": "12"}))
    assert not f.comm.delivered_work(11, 12)


def test_같은턴_병렬중복요청은_합쳐서_재호출안함():
    """같은 턴에 같은 동료에게 같은 요청을 다발로 보내면(병렬 중복), 동료를 다시 깨우지 않고 직전
    응답을 재사용한다 — 반사적 중복 wake를 구조적으로 차단(서로 다른 동료 병렬요청은 직렬화·거부 아님)."""
    g = FakeGuide()
    f = _flow(g)                                   # leader 11, member 12; alive=11
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "members": "12"}))
    f.comm.history.append(("request", 11, 12, "r", Kind.INFO))
    asyncio.run(tools["set_goal"].handler({"goal": "g"}))
    waked = []

    async def wake(to, b, k):
        waked.append(to)
        return "동료응답"

    f.wake = wake
    r1 = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Info", "body": "질문"}))
    assert waked == [12] and "동료응답" in r1["content"][0]["text"]   # 1차: 동료 깸·응답 캐시
    r2 = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Info", "body": "질문"}))
    assert waked == [12]                                              # 2차: 다시 깨우지 않음(합침)
    assert "재사용" in r2["content"][0]["text"] and "동료응답" in r2["content"][0]["text"]


def test_되묻기후_재위임은_Redo아님():
    """owner가 '되묻기(clarify)'만 하고 반환하면 미완이므로, 위임자가 다시 맡기는 건 '첫 구현'이지 Redo가 아니다."""
    g = FakeGuide()
    f = _flow(g)
    calls = {"n": 0}

    async def wake(to, b, k):
        calls["n"] += 1
        if calls["n"] == 1:                      # 1차: 되묻기만 남기고 반환(미완)
            f.pending_clarify = {"from": 12, "to": 11, "q": "필드명?"}
            return "(짧게 반환)"
        return "완료"                            # 2차: 실제 완료

    f.wake = wake
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "members": "12"}))
    f.comm.history.append(("request", 11, 12, "r", Kind.INFO))
    asyncio.run(tools["set_goal"].handler({"goal": "g"}))
    asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "구현"}))   # 되묻기 → 미완
    assert not f.comm.delivered_work(11, 12)                       # 완료 아님 → delivered 기록 안 됨
    r = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "구현(답 반영)"}))
    assert not any(ev[0] == "redo" for ev in f.comm.history)       # 재위임이지만 Redo 아님
    assert "응답" in r["content"][0]["text"]
