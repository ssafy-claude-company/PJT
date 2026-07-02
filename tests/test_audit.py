"""audit 검증: JSONL 기록 + PostToolUse 훅 (오프라인)."""
import asyncio
import json
from pathlib import Path

from system.audit import AuditLog, make_post_tool_use_hook
from system.config import Config


def test_record가_JSONL로_누적():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        log = AuditLog(Path(d) / "audit.jsonl")
        log.record("collect", author="사람", content="안녕")
        log.record("route", author="사람")
        lines = (Path(d) / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        e0 = json.loads(lines[0])
        assert e0["event"] == "collect" and e0["author"] == "사람" and "ts" in e0
        assert json.loads(lines[1])["event"] == "route"


def test_PostToolUse_훅이_툴호출_기록():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        log = AuditLog(Path(d) / "a.jsonl")
        hook = make_post_tool_use_hook(log)
        out = asyncio.run(hook(
            {"hook_event_name": "PostToolUse", "tool_name": "Write",
             "tool_input": {"file_path": "x.txt"}},
            "tu_1", None,
        ))
        assert out == {}
        e = json.loads((Path(d) / "a.jsonl").read_text(encoding="utf-8").strip())
        assert e["event"] == "tool_use" and e["tool"] == "Write"
        assert e["tool_use_id"] == "tu_1"


def test_PostToolUse_훅이_행위자_기록():
    """actor/role를 주면 '누가' 그 툴을 호출했는지 로그에 남는다 — 협업 관찰성."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        log = AuditLog(Path(d) / "a.jsonl")
        hook = make_post_tool_use_hook(log, actor=12345, role="봇 AI 전문가(먹이탐색)")
        asyncio.run(hook(
            {"hook_event_name": "PostToolUse", "tool_name": "Edit",
             "tool_input": {"file_path": "server.js"}}, "tu_2", None,
        ))
        e = json.loads((Path(d) / "a.jsonl").read_text(encoding="utf-8").strip())
        assert e["actor"] == 12345 and e["role"] == "봇 AI 전문가(먹이탐색)"
        assert e["tool"] == "Edit"


def test_redact_tool_input은_파일내용을_길이로_요약한다():
    """보안 핫픽스: 감사에 Write/Edit의 파일 내용 전체를 남기지 않고 길이로 요약(경로·도구는 보존)."""
    from system.audit import redact_tool_input
    big = "x" * 500
    out = redact_tool_input({"file_path": "/ws/a.js", "content": big})
    assert out["file_path"] == "/ws/a.js"                      # 경로 보존
    assert "500" in out["content"] and "chars" in out["content"]   # 내용 → 길이요약
    assert "xxxx" not in str(out)                              # 원본 내용 없음
    out2 = redact_tool_input({"old_string": "y" * 200, "new_string": "z" * 200})
    assert "chars" in out2["old_string"] and "chars" in out2["new_string"]
    assert redact_tool_input({"content": "short"})["content"] == "short"   # 짧은 값 보존
    assert redact_tool_input("notdict") == "notdict"          # 비-dict 그대로
