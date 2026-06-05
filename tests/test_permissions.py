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
