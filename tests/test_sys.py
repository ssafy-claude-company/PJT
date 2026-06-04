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


def test_member는_request_recruit_run():
    f = _flow(FakeGuide())
    assert {t.name for t in make_guide_tools(f, 12, "member")} == {"request", "recruit", "run"}


def test_leader는_project_task_도구():
    f = _flow(FakeGuide())
    names = {t.name for t in make_guide_tools(f, 11, "leader")}
    # 보고/답변 툴 없음(반환=Response). 흐름 도구(request·recruit·run)+리더 셋업·배포 도구.
    assert names == {"request", "recruit", "run",
                     "create_project", "create_task", "complete_task", "deploy"}


def test_run_안전가드():
    f = _flow(FakeGuide())
    rt = {t.name: t for t in make_guide_tools(f, 11, "leader")}["run"]
    f.workspace = None
    assert "작업공간" in asyncio.run(rt.handler({"command": "echo hi"}))["content"][0]["text"]
    f.workspace = "/tmp"
    assert "거부" in asyncio.run(rt.handler({"command": "rm -rf /tmp/x"}))["content"][0]["text"]
    assert "거부" in asyncio.run(rt.handler({"command": "git commit -am x"}))["content"][0]["text"]


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


def test_create_task_owner_분산():
    """create_task(owner=…) → 산출물별 단일 책임자 배정(구조적 분산). owner는 팀 자동 합류."""
    g = FakeGuide()
    f = Flow(g, channel_id=500, guild_id=1, leader_id=11, bot_info={11: "L", 12: "A백엔드", 13: "B프론트"})
    f.start_root("root")
    t = {x.name: x for x in make_guide_tools(f, 11, "leader")}
    asyncio.run(t["create_project"].handler({"name": "p", "team": "12,13"}))
    # 합의 없이 owner 배정은 거부(리더 독단 차단)
    blocked = asyncio.run(t["create_task"].handler({"purpose": "서버", "goal": "동작", "owner": "A백엔드"}))
    assert "합의" in blocked["content"][0]["text"] and not f.tasks
    f.comm.history.append(("request", 11, 12, "r", Kind.INFO))    # 리더가 owner와 먼저 협의했다고 기록
    asyncio.run(t["create_task"].handler({"purpose": "서버", "goal": "동작", "owner": "A백엔드", "members": ""}))
    assert f.current.owner == 12                                  # 협의 후 비리더를 owner로
    assert f.current.status.owner and "A백엔드" in f.current.status.owner
    assert 12 in f.current.team                                   # owner 팀 자동 합류
    f.current.verified = True                                     # run 검증됨으로 간주(허위완료 가드 통과)
    asyncio.run(t["complete_task"].handler({"result": "서버 완료"}))   # 마감해야 다음 Task
    # owner 미지정도 허용(공동) — 깨지지 않음
    asyncio.run(t["create_task"].handler({"purpose": "x", "goal": "g", "owner": "", "members": "13"}))
    assert f.current.owner == 0 and f.current.status.owner == ""


def test_request_동료_깨우고_베턴복귀():
    g = FakeGuide()
    f = _flow(g)
    waked = []

    async def wake(to, b, k):
        waked.append((to, b, k))
        return f"{b} 처리완료"

    f.wake = wake
    tools = _tools(f, 11, "leader")
    asyncio.run(tools["create_task"].handler({"purpose": "p", "goal": "g"}))
    res = asyncio.run(tools["request"].handler({"to_id": "12", "kind": "Work", "body": "백엔드"}))
    assert waked == [(12, "백엔드", Kind.WORK)]      # 동료 깨움
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
    assert f.tasks[1].status.status == "완료" and f.tasks[1].status.result == "프론트 완료"
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
    # 검증 전 마감 시도 → 거부(허위완료 금지), Task는 여전히 진행 중
    r = asyncio.run(tools["complete_task"].handler({"result": "다 했어요"}))
    assert "거부" in r["content"][0]["text"] and "검증" in r["content"][0]["text"]
    assert f.current is not None and f.current.status.status != "완료"
    # run으로 실제 실행 검증 → verified=True
    asyncio.run(tools["run"].handler({"command": "echo ok"}))
    assert f.current.verified is True
    # 이제 마감 허용
    r2 = asyncio.run(tools["complete_task"].handler({"result": "검증 후 완료"}))
    assert "완료" in r2["content"][0]["text"] and f.current is None


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
