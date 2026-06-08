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


def test_개입아닌_새작업은_목표게이트_미적용():
    """개입이 아닌 일반 흐름(intervention 없음)에선 이 개입 게이트가 적용되지 않는다(기존 동작 보존)."""
    a = FakeAudit()
    flow = _FakeFlow2(_comm_with((0, 11, Kind.WORK)), current=_FakeTask(owner=0, goal=""), leader=11)
    # intervention 미설정 → 개입 게이트 통과(owner0이라 대리구현 게이트도 통과)
    assert _run(make_pre_tool_use_hook(a, ALLOWED, actor=11, flow=flow), "Write", {"file_path": "a.js"}) == {}
