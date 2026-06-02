"""audit 검증: JSONL 기록 + PostToolUse 훅 (오프라인)."""
import asyncio
import json
from pathlib import Path

from src.audit import AuditLog, make_post_tool_use_hook
from src.config import Config


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
