"""기능6 검증: audit JSONL 기록 + PostToolUse 훅 + App 수집 배선 (오프라인)."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from src.app import App
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


def _cfg(d) -> Config:
    return Config(
        system_bot_token="s", organt_bot_token="o", channel_id=1, model=None,
        workspace_dir=Path(d) / "ws", audit_log_path=Path(d) / "audit.jsonl",
    )


def test_App_수집이_audit에_기록():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        app = App(_cfg(d))  # 구성은 네트워크 없이 가능
        msg = SimpleNamespace(author="사람", id=7, content="@Organt 안녕")
        app._on_collect(msg)
        e = json.loads((Path(d) / "audit.jsonl").read_text(encoding="utf-8").strip())
        assert e["event"] == "collect" and e["content"] == "@Organt 안녕"
        assert e["message_id"] == 7
