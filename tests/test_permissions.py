"""기능8 검증: PreToolUse 권한 차단 훅."""
import asyncio

from src.permissions import _within, make_pre_tool_use_hook, organt_allowed_tools

ALLOWED = organt_allowed_tools(["mcp__discord__send_message", "mcp__discord__reply_message"])


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
