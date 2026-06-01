"""기능10 검증: 서브에이전트 위임 설정."""
from src.permissions import organt_allowed_tools
from src.subagents import WRITER, organt_subagents


def test_writer_서브에이전트_정의():
    subs = organt_subagents()
    assert WRITER in subs
    w = subs[WRITER]
    assert "Write" in w.tools
    assert "Bash" not in (w.tools or [])
    assert isinstance(w.description, str) and w.description
    assert isinstance(w.prompt, str) and w.prompt


def test_메인이_위임하려면_위임툴_허용():
    # 이 환경의 서브에이전트 위임 툴은 'Agent'(버전에 따라 'Task').
    assert "Agent" in organt_allowed_tools([])


def test_위임은_허용_Bash는_불가():
    allowed = organt_allowed_tools(["mcp__discord__send_message"])
    assert "Agent" in allowed and "Bash" not in allowed
