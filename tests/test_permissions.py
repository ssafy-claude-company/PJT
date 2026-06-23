"""기능8 검증: PreToolUse 권한 차단 훅."""
import asyncio

from src.communication import CommunicationManager
from src.permissions import _within, make_pre_tool_use_hook, organt_allowed_tools
from src.protocol import Kind

ALLOWED = organt_allowed_tools(["mcp__discord__send_message", "mcp__discord__reply_message"])


class _FakeFlow:
    """hook이 보는 건 flow.comm.open_requests(베턴 스택)뿐 — 최소 구성."""
    def __init__(self, comm):
        self.comm = comm


class FakeAudit:
    def __init__(self):
        self.records = []

    def record(self, event, **fields):
        self.records.append((event, fields))
        return {}


def _run(hook, tool_name, tool_input=None, cwd="/ws"):
    return asyncio.run(hook({
        "tool_name": tool_name, "tool_input": tool_input or {}, "cwd": cwd,
    }, "tu_1", None))


def test_허용도구는_통과():
    a = FakeAudit()
    out = _run(make_pre_tool_use_hook(a, ALLOWED), "Write", {"file_path": "a.txt"})
    assert out == {}
    assert a.records == []


def test_권한밖도구_Bash_차단_및_로그():
    a = FakeAudit()
    out = _run(make_pre_tool_use_hook(a, ALLOWED), "Bash", {"command": "ls"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records and a.records[0][0] == "tool_denied"
    assert a.records[0][1]["tool"] == "Bash" and a.records[0][1]["reason"] == "권한 밖 도구"


def test_디스코드툴_허용():
    a = FakeAudit()
    out = _run(make_pre_tool_use_hook(a, ALLOWED), "mcp__discord__send_message", {"content": "hi"})
    assert out == {}


def test_작업공간밖_절대경로_쓰기_차단():
    a = FakeAudit()
    out = _run(make_pre_tool_use_hook(a, ALLOWED), "Write", {"file_path": "/etc/passwd"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[0][1]["reason"] == "작업공간 밖 경로"


def test_작업공간_안_상대경로는_허용():
    a = FakeAudit()
    out = _run(make_pre_tool_use_hook(a, ALLOWED), "Edit", {"file_path": "sub/b.txt"})
    assert out == {}


def test_within_판정():
    assert _within("/ws", "a.txt") is True
    assert _within("/ws", "/ws/sub/x") is True
    assert _within("/ws", "/etc/x") is False
    assert _within("/ws", "../escape") is False


def _comm_with(*reqs):
    """reqs=[(from,to,kind), ...] 순서대로 베턴에 쌓아 alive=마지막 to 로 만든다."""
    c = CommunicationManager(origin_id=0)
    for frm, to, kind in reqs:
        c.request(frm, to, f"r{to}", kind)
    return c


def test_협의Info중_선구현_차단():
    """flow를 주면, Info(협의)로 깨워진 동료의 Write/Edit는 차단된다 — 선구현 금지."""
    a = FakeAudit()
    flow = _FakeFlow(_comm_with((0, 11, Kind.WORK), (11, 12, Kind.INFO)))  # 12가 Info로 깨워짐
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=12, flow=flow)
    out = _run(hook, "Write", {"file_path": "server.js"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "협의(Info) 중 선구현"


def test_Work위임받은_owner_구현_허용():
    """Work로 깨워진 owner의 Write는 허용된다(구현은 위임 맥락에서)."""
    a = FakeAudit()
    flow = _FakeFlow(_comm_with((0, 11, Kind.WORK), (11, 12, Kind.WORK)))  # 12가 Work로 깨워짐
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=12, flow=flow)
    assert _run(hook, "Write", {"file_path": "server.js"}) == {}


def test_리더_origin_Work_구현_허용():
    """리더(origin→leader Work 프레임)의 Write는 허용된다(기여자 역할 보존)."""
    a = FakeAudit()
    flow = _FakeFlow(_comm_with((0, 11, Kind.WORK)))  # 11=리더 alive
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=11, flow=flow)
    assert _run(hook, "Write", {"file_path": "app.js"}) == {}


def test_flow없으면_게이트_미적용():
    """flow 미주입(기존 호출부·테스트)에선 협의게이트가 적용되지 않는다(하위호환)."""
    a = FakeAudit()
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=12)
    assert _run(hook, "Write", {"file_path": "x.txt"}) == {}


class _FakeTask:
    """Fix B용 최소 Task: owner·status.owner(표시용)·status.goal(개입 게이트용)."""
    def __init__(self, owner, owner_label="프A", goal=""):
        self.owner = owner
        self.status = type("S", (), {"owner": owner_label, "goal": goal})()


class _FakeFlow2(_FakeFlow):
    """current(owner 지정)·leader·act_count를 갖춘 흐름 — owner 도메인 대리구현 게이트 검증용."""
    def __init__(self, comm, current=None, leader=11):
        super().__init__(comm)
        self.current = current
        self.leader = leader
        self.act_count = 0


def test_리더는_위임된_owner도메인_대리구현_차단():
    """이미 owner(12)에게 Work로 위임된 Task의 산출물을 리더(11)가 Write하면 거부 — 독점·허위완료 차단.
    (사용자가 잡은 '리더가 프론트 파일을 직접 만들고 완료' 패턴의 차단점.)"""
    a = FakeAudit()
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=_FakeTask(owner=12), leader=11)
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=11, flow=flow)
    out = _run(hook, "Write", {"file_path": "public/app.js"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "위임된 owner 도메인 대리구현"
    assert flow.act_count == 0   # 거부됐으니 작업 집계 안 됨


def test_owner본인_구현은_허용되고_act집계():
    """owner(12) 본인이 자기 Task 산출물을 Write하는 건 허용되고 act_count가 +1 — '검증된 인도' 측정 신호."""
    a = FakeAudit()
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK), (11, 12, Kind.WORK)),
                      current=_FakeTask(owner=12), leader=11)
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=12, flow=flow)
    assert _run(hook, "Write", {"file_path": "public/app.js"}) == {}
    assert flow.act_count == 1


def test_위임전_owner0_Task는_리더구현_허용():
    """아직 위임 안 한(owner==0) Task에선 리더도 직접 구현 가능 — 리더도 한 직원(중앙집권 아님)."""
    a = FakeAudit()
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=_FakeTask(owner=0), leader=11)
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=11, flow=flow)
    assert _run(hook, "Write", {"file_path": "server.js"}) == {}
    assert flow.act_count == 1


def test_개입은_목표확정_전_수정차단():
    """개입(기존 프로젝트 수정)에서 Task Goal이 없으면 Write/Edit 거부 — 재현·목표합의 전 즉흥수정(개인 견해
    선반영) 차단. Goal이 확정되면 허용 → '목표 먼저, 그다음 수정' 순서를 구조적으로 강제."""
    a = FakeAudit()
    task = _FakeTask(owner=0, goal="")                       # 목표 미확정 상태
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=task, leader=11)
    flow.intervention = {"id": "P-001"}                      # 개입 흐름
    out = _run(make_pre_tool_use_hook(a, ALLOWED, actor=11, flow=flow), "Edit", {"file_path": "server.js"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "개입 목표 미확정 선수정"
    # Goal 확정되면 허용
    task.status.goal = "충돌 판정이 정상 동작(평타가 적에게 적중)"
    a2 = FakeAudit()
    assert _run(make_pre_tool_use_hook(a2, ALLOWED, actor=11, flow=flow), "Edit", {"file_path": "server.js"}) == {}


def test_리더_위임없이_단독구현_차단():
    """팀이 있는 Task에서 리더가 Work 위임 없이 혼자 다 쓰는 걸 차단 — '자문(Info)만 받고 독식'(QA가 12파일
    단독 작성) 구멍을 막는다. 한 파일은 grace, 그다음부턴 위임해야 함. 위임하면 다시 허용. 단독 Task는 무제한."""
    a = FakeAudit()
    task = _FakeTask(owner=0, goal="g")
    task.team = [11, 12, 13]       # 리더 11 + 도메인 동료 12·13
    task.work_delegated = 0
    task.leader_writes = 0
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=task, leader=11)
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=11, flow=flow)
    assert _run(hook, "Write", {"file_path": "server.js"}) == {} and task.leader_writes == 1   # 1파일 grace
    out = _run(hook, "Write", {"file_path": "game.js"})                                        # 2파일째 차단
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "리더 독식(위임 없이 단독 구현)"
    task.work_delegated = 1                                                                     # 위임하면 다시 허용
    assert _run(hook, "Write", {"file_path": "cfg.js"}) == {}
    # 동료 없는 단독 Task: 제한 없음
    solo = _FakeTask(owner=0, goal="g"); solo.team = [11]; solo.work_delegated = 0; solo.leader_writes = 9
    f2 = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=solo, leader=11)
    assert _run(make_pre_tool_use_hook(FakeAudit(), ALLOWED, actor=11, flow=f2), "Write", {"file_path": "x"}) == {}


def test_개입아닌_새작업은_목표게이트_미적용():
    """개입이 아닌 일반 흐름(intervention 없음)에선 이 개입 게이트가 적용되지 않는다(기존 동작 보존)."""
    a = FakeAudit()
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=_FakeTask(owner=0, goal=""), leader=11)
    # intervention 미설정 → 개입 게이트 통과(owner0이라 대리구현 게이트도 통과)
    assert _run(make_pre_tool_use_hook(a, ALLOWED, actor=11, flow=flow), "Write", {"file_path": "a.js"}) == {}


def test_개입_리더_단독run_독식_차단():
    """[근본] 개입에서 리더가 'Task도 안 열고' 또는 '위임 없이 run만 반복'하면 차단 — 사용자가 본 '리더가
    혼자 다 함'(Task 개입 아님). create_task+위임 후엔 풀린다(리더의 최종 검증 run 허용)."""
    a = FakeAudit()
    run_allowed = ALLOWED + ["mcp__guide__run"]              # 리더는 실제로 run 권한 보유(FLOW_TOOLS)
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=None, leader=11)
    flow.intervention = {"id": "P-001"}
    flow.project_team = [11, 12, 13]                          # 리더 11 + 도메인 동료 12·13
    flow.tasks = []
    flow._info = lambda i: {11: "백엔드", 12: "프론트엔드", 13: "VFX 아티스트"}.get(i, "")
    hook = make_pre_tool_use_hook(a, run_allowed, actor=11, flow=flow)
    # (a) Task 없이 run → 즉시 차단(먼저 create_task로 'Task 개입' 열라)
    out = _run(hook, "mcp__guide__run", {"command": "node server.js"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "개입 Task 미개설 단독 실행"
    # (b) Task는 열었지만 위임 0 → 초기 run 3회는 허용(재현용), 4회째 독식 차단
    task = _FakeTask(owner=0, goal="g"); task.work_delegated = 0
    flow.current = task; flow.tasks = [task]
    for _ in range(3):
        assert _run(hook, "mcp__guide__run", {"command": "ls"}) == {}
    out = _run(hook, "mcp__guide__run", {"command": "ls"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "개입 위임없이 단독 run 독식"
    # (c) owner에게 위임(work_delegated>=1)하면 다시 허용 — 리더의 최종 검증 run
    task.work_delegated = 1
    assert _run(hook, "mcp__guide__run", {"command": "curl localhost:3000"}) == {}
    # (d) 개입이 아니면(일반 흐름) 이 run 게이트는 적용 안 됨
    f2 = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=None, leader=11)
    f2.project_team = [11, 12]; f2.tasks = []; f2._info = lambda i: "백엔드"
    assert _run(make_pre_tool_use_hook(FakeAudit(), run_allowed, actor=11, flow=f2),
                "mcp__guide__run", {"command": "ls"}) == {}


def test_쓰기리스_샌드박스밖_차단_안은_허용():
    """[경쟁 구현 — 쓰기 리스] 리스가 배정된 행위자는 자기 샌드박스 안에만 쓴다 — 경쟁자 간·본
    작업물과의 파일 충돌(덮어쓰기→재작업)이 구조적으로 불가능. 리스 없는 행위자는 종전대로."""
    a = FakeAudit()
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK), (11, 12, Kind.WORK)),
                      current=_FakeTask(owner=12, goal="g"), leader=11)
    flow.write_lease = {12: "/ws/_compete/t-12"}
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=12, flow=flow)
    out = _run(hook, "Write", {"file_path": "server.js"})              # /ws/server.js — 리스 밖
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "쓰기 리스 밖"
    assert _run(hook, "Write", {"file_path": "_compete/t-12/server.js"}) == {}   # 리스 안 허용
    f2 = _FakeFlow2(_comm_with((0, 11, Kind.WORK), (11, 13, Kind.WORK)),
                    current=_FakeTask(owner=13, goal="g"), leader=11)
    assert _run(make_pre_tool_use_hook(FakeAudit(), ALLOWED, actor=13, flow=f2),
                "Write", {"file_path": "server.js"}) == {}             # 리스 없으면 작업공간 전체


def test_fork가지_Info수집은_선구현차단_Work가지는_허용():
    """fork 수집 가지는 comm 프레임을 열지 않으므로 flow.fork_kind가 선구현 게이트를 잇는다 —
    표결·회의 1라운드(Info 가지)의 Write는 종전과 동일하게 차단, 경쟁 구현(Work 가지)은 허용."""
    a = FakeAudit()
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=_FakeTask(owner=0, goal="g"), leader=11)
    flow.fork_kind = {12: Kind.INFO}                          # 표결/회의 1R 수집 중
    out = _run(make_pre_tool_use_hook(a, ALLOWED, actor=12, flow=flow), "Write", {"file_path": "x.js"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "협의(Info) 중 선구현"
    flow.fork_kind = {12: Kind.WORK}                          # 경쟁 구현 가지
    assert _run(make_pre_tool_use_hook(FakeAudit(), ALLOWED, actor=12, flow=flow),
                "Write", {"file_path": "x.js"}) == {}


def test_리더_흡수_상대적_차단_팀합_초과():
    """[리더 흡수 차단 #8] #6·#7은 'delegated==0'(한 번도 위임 안 함)만 잡아 '일부 위임 후 흡수'(P-026 실측:
    일부 위임 뒤 리더가 혼자 255회 run)가 우회됐다. 코디네이터의 직접 doing(act_by 리더)이 '팀 전체 합'을
    넘으면 그건 분배가 아니라 흡수 — grace(>=8) 이후 Write/Edit/run을 막아 검증·구현을 동료에게 위임 강제.
    위임으로 팀 활동이 오르면(자가치유) 다시 풀린다. owner가 직접 마감하는 게 아니라 리더가 분배하게 만든다."""
    a = FakeAudit()
    run_allowed = ALLOWED + ["mcp__guide__run"]
    task = _FakeTask(owner=0, goal="g"); task.team = [11, 12, 13]
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=task, leader=11)
    flow.project_team = [11, 12, 13]
    flow._info = lambda i: {11: "백엔드", 12: "프론트엔드", 13: "데이터"}.get(i, "")
    # 리더 doing(8)이 팀 합(3+2=5)을 넘고 grace(>=8) 충족 → 흡수 차단
    flow.act_by = {11: 8, 12: 3, 13: 2}
    hook = make_pre_tool_use_hook(a, run_allowed, actor=11, flow=flow)
    out = _run(hook, "mcp__guide__run", {"command": "node server.js"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "리더 흡수(팀 합보다 많이 doing)"
    # [자가치유] 위임으로 팀 활동이 올라 팀 합(5+4=9)이 리더(8)를 넘으면 다시 허용
    flow.act_by = {11: 8, 12: 5, 13: 4}
    assert _run(hook, "mcp__guide__run", {"command": "curl localhost:3000"}) == {}
    # [grace] 리더 doing이 8 미만이면 초기 셋업·통합 허용(팀보다 많아도)
    flow.act_by = {11: 5, 12: 1, 13: 0}
    assert _run(hook, "mcp__guide__run", {"command": "ls"}) == {}
    # Write도 동일 차단(work_delegated>=1로 #6 우회, owner=0으로 #4 우회 → 순수 #8 검증)
    task.work_delegated = 1; task.leader_writes = 0
    flow.act_by = {11: 10, 12: 2, 13: 1}
    out = _run(hook, "Write", {"file_path": "server.js"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "리더 흡수(팀 합보다 많이 doing)"


def test_리더_흡수_차단_교착방지_도달동료없으면_통과():
    """[교착 방지] 도달 가능한 동료가 없으면(솔로 또는 전원 예비) 흡수 차단이 풀린다 — 혼자뿐인 흐름에서
    리더가 멈추면 그게 교착이다. 차단은 '맡길 사람이 실제로 있을 때'만(안정성 우선)."""
    a = FakeAudit()
    run_allowed = ALLOWED + ["mcp__guide__run"]
    task = _FakeTask(owner=0, goal="g"); task.team = [11]
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=task, leader=11)
    flow.project_team = [11]                       # 동료 없음(솔로)
    flow._info = lambda i: "백엔드"
    flow.act_by = {11: 30}                         # 흡수처럼 보여도 맡길 사람이 없음 → 통과
    assert _run(make_pre_tool_use_hook(a, run_allowed, actor=11, flow=flow),
                "mcp__guide__run", {"command": "ls"}) == {}
    # 예비(reserve)만 있는 경우도 도달 가능 동료 0 → 통과
    flow.project_team = [11, 99]; flow._info = lambda i: ("예비 봇" if i == 99 else "백엔드")
    assert _run(make_pre_tool_use_hook(FakeAudit(), run_allowed, actor=11, flow=flow),
                "mcp__guide__run", {"command": "ls"}) == {}


def test_흡수차단_타도메인_전문가_idle이면_Write차단():
    """[게이트 #9 흡수 차단] 자기 도메인 밖 일을, 그 도메인 전문가가 idle인데 대신 Write하면 차단 — 사용자
    "전문가가 놀고 있으면 대기하든가, 왜 모르는 일까지 하느냐". request(Work)로 맡기거나(work_delegated_to
    진입) 그가 일하면(act_by>0) 풀린다. P-026: 백엔드가 AI 엔지니어 모델까지 흡수한 패턴을 흐름 중 차단."""
    a = FakeAudit()
    # 백엔드(12)=owner가 AI 도메인(모델) Write 시도, AI 엔지니어(13)는 distinct·idle·미위임·도달가능
    task = _FakeTask(owner=12, goal="g"); task.team = [11, 12, 13]; task.work_delegated_to = set()
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK), (11, 12, Kind.WORK)), current=task, leader=11)
    flow._info = lambda i: {11: "게임 기획자", 12: "백엔드", 13: "AI 엔지니어"}.get(i, "")
    flow.act_by = {12: 3, 13: 0}              # 백엔드는 일함, AI 엔지니어 idle
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=12, flow=flow)
    out = _run(hook, "Write", {"file_path": "model/train.js"})   # 백엔드가 AI 도메인 흡수 시도
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "타 도메인 전문가 일 흡수(전문가 idle)"
    task.work_delegated_to.add(13)                               # 위임하면(기회 부여) 풀림
    assert _run(hook, "Write", {"file_path": "model/train.js"}) == {}
    task.work_delegated_to.discard(13); flow.act_by[13] = 2      # 위임 대신 그가 실제로 일해도 풀림
    assert _run(hook, "Write", {"file_path": "server.js"}) == {}


def test_흡수차단_같은도메인_idle은_차단안함_그리고_도달불가_통과():
    """[게이트 #9 정밀성·교착방지] (a) 같은 도메인 idle 동료엔 발동 안 함(동질끼린 흡수 아님).
    (b) distinct 전문가라도 예비/도달불가면 통과(맡길 사람 없으면 직접 — 안정성 우선)."""
    # (a) 같은 도메인(백엔드 둘) — 13 idle이어도 차단 안 함
    task = _FakeTask(owner=12, goal="g"); task.team = [11, 12, 13]; task.work_delegated_to = set()
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK), (11, 12, Kind.WORK)), current=task, leader=11)
    flow._info = lambda i: {11: "게임 기획자", 12: "백엔드", 13: "백엔드"}.get(i, "")
    flow.act_by = {12: 3, 13: 0}
    assert _run(make_pre_tool_use_hook(FakeAudit(), ALLOWED, actor=12, flow=flow),
                "Write", {"file_path": "server.js"}) == {}
    # (b) distinct지만 예비 → 도달 불가 → 통과
    task2 = _FakeTask(owner=12, goal="g"); task2.team = [11, 12, 13]; task2.work_delegated_to = set()
    f2 = _FakeFlow2(_comm_with((0, 11, Kind.WORK), (11, 12, Kind.WORK)), current=task2, leader=11)
    f2._info = lambda i: {11: "게임 기획자", 12: "백엔드", 13: "예비 봇"}.get(i, "")
    f2.act_by = {12: 3, 13: 0}
    assert _run(make_pre_tool_use_hook(FakeAudit(), ALLOWED, actor=12, flow=f2),
                "Write", {"file_path": "server.js"}) == {}


def test_흡수차단_자기도메인_Write는_idle전문가있어도_통과_그러나_타도메인파일은_차단():
    """[게이트 #9 file-aware(2026-06-23, 사용자: '전문가는 자기 도메인 야심껏')] 흡수란 '*다른* 도메인 일을
    대신함'이다. 종전 게이트는 쓰는 *파일을 안 보고* idle 전문가가 있기만 하면 행위자의 *자기 도메인* Write
    까지 막아 협업을 마비시켰다(라이브 규명: SYS가 배정한 프론트 전문가가 idle 동료 10명 때문에 21분 0파일).
    이제 쓰는 *파일*이 idle 전문가 능력을 요구할 때만 차단 — 자기 도메인 파일은 통과한다."""
    task = _FakeTask(owner=12, goal="g"); task.team = [11, 12, 13, 14]; task.work_delegated_to = set()
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK), (11, 12, Kind.WORK)), current=task, leader=11)
    flow._info = lambda i: {11: "게임 기획자", 12: "프론트엔드", 13: "AI 엔지니어", 14: "데이터"}.get(i, "")
    flow.act_by = {12: 1, 13: 0, 14: 0}              # AI·데이터 전문가 idle(미위임·미작업)
    hook = make_pre_tool_use_hook(FakeAudit(), ALLOWED, actor=12, flow=flow)
    # (a) 자기 도메인(프론트) 파일 — idle 전문가가 있어도 통과(종전엔 차단됐던 마비 지점)
    assert _run(hook, "Write", {"file_path": "src/components/RecommendList.tsx"}) == {}
    # (b) 같은 프론트가 AI 도메인 파일(train) 흡수 시도 — 여전히 차단(AI 엔지니어 idle·미위임)
    out = _run(hook, "Write", {"file_path": "model/train.py"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"].startswith("흡수 차단")


def test_막힘흡수차단_막힌동료_일_대신못함_재요청유도():
    """[게이트 #10] 하위 담당이 막혀 베턴이 위임자에게 돌아온 순간(flow._stall_victim 기록), 위임자가 막힌
    사람과 도메인이 다른 일을 '내가 하지'로 대신 Write하면 차단 — request로 '같은 사람 재요청' 유도(재채용 X).
    막힌 사람이 다시 act하면 해제. P-027: 백엔드 막힘 → AI엔지니어 흡수(867줄 폐기) 패턴 차단."""
    a = FakeAudit()
    task = _FakeTask(owner=12, goal="g"); task.team = [11, 12, 13]
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK), (11, 12, Kind.WORK)), current=task, leader=11)
    flow._info = lambda i: {11: "게임 기획자", 12: "AI 엔지니어", 13: "백엔드"}.get(i, "")
    flow.act_by = {12: 5, 13: 2}              # 백엔드(13)가 2번 일하다 막힘
    flow._stall_victim = 13; flow._stall_victim_acts = 2; flow._stall_blocks = 0
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=12, flow=flow)   # AI엔지니어가
    out = _run(hook, "Write", {"file_path": "server.js"})            # 백엔드 일(server.js) 대신 작성 시도
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "막힌 동료 일 흡수(재요청 대신 대신함)"
    flow.act_by[13] = 3                                              # 막힌 백엔드가 다시 일함 → 해제
    assert _run(hook, "Write", {"file_path": "server.js"}) == {}
    assert flow._stall_victim is None


def test_막힘흡수차단_같은도메인_허용_그리고_무응답_폴백():
    """[게이트 #10 정밀성·교착방지] (a) 막힌 사람과 같은 도메인이면 차단 안 함(같은 분야 이어받기는 흡수 아님).
    (b) 막힌 사람이 끝내 무응답이면 N회 차단 후 폴백(통과 — 죽은 동료에 빌드가 얼지 않음)."""
    # (a) 같은 도메인(둘 다 백엔드) — 막혔어도 차단 안 함
    task = _FakeTask(owner=11, goal="g"); task.team = [11, 12, 13]
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=task, leader=11)
    flow._info = lambda i: {11: "게임 기획자", 12: "백엔드", 13: "백엔드"}.get(i, "")
    flow.act_by = {12: 5, 13: 2}; flow._stall_victim = 13; flow._stall_victim_acts = 2; flow._stall_blocks = 0
    assert _run(make_pre_tool_use_hook(FakeAudit(), ALLOWED, actor=12, flow=flow),
                "Write", {"file_path": "server.js"}) == {}          # 같은 백엔드라 통과
    # (b) 무응답 폴백: distinct 도메인이라 3회 막다가 4회째 통과
    f2 = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=task, leader=11)
    f2._info = lambda i: {11: "게임 기획자", 12: "AI 엔지니어", 13: "백엔드"}.get(i, "")
    f2.act_by = {12: 5, 13: 2}; f2._stall_victim = 13; f2._stall_victim_acts = 2; f2._stall_blocks = 0
    h2 = make_pre_tool_use_hook(FakeAudit(), ALLOWED, actor=12, flow=f2)
    for _ in range(3):
        assert _run(h2, "Write", {"file_path": "server.js"})["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _run(h2, "Write", {"file_path": "server.js"}) == {}      # 4회째 폴백 통과
    assert f2._stall_victim is None


def test_쓰기리스_다중경로_병렬Work():
    """[RFC-006 parallel_work] 가지마다 '파일 목록' 리스 — 목록 중 어느 하나(파일 자신 일치 포함)면
    허용, 전부 밖이면 차단. Work 가지(fork_kind=WORK)의 구현 허용과 결합된 실제 병렬 형상 그대로."""
    a = FakeAudit()
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=_FakeTask(owner=12, goal="g"), leader=11)
    flow.fork_kind = {12: Kind.WORK}
    flow.write_lease = {12: ["/ws/public/app.js", "/ws/public/style.css"]}
    hook = make_pre_tool_use_hook(a, ALLOWED, actor=12, flow=flow)
    assert _run(hook, "Write", {"file_path": "public/app.js"}) == {}          # 목록 내(상대경로)
    assert _run(hook, "Edit", {"file_path": "/ws/public/style.css"}) == {}    # 목록 내(절대경로)
    out = _run(hook, "Write", {"file_path": "server.js"})                     # 목록 전부 밖
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert a.records[-1][1]["reason"] == "쓰기 리스 밖"
