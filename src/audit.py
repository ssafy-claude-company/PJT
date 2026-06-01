"""Audit 로그: 모든 흐름(수집·라우팅·툴 호출·응답)을 JSONL 한 줄씩 남긴다.

Step 1 증명의 '왕복이 로그에 그대로 남는다'를 담당한다.
"""
import json
import time
from pathlib import Path


class AuditLog:
    """append-only JSONL 기록기."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, **fields) -> dict:
        """이벤트 한 건을 기록하고, 기록한 entry를 돌려준다."""
        entry = {"ts": time.time(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry


def make_post_tool_use_hook(audit: AuditLog):
    """Organt의 모든 툴 호출을 audit에 남기는 PostToolUse 훅 콜백을 만든다.

    hooks={"PostToolUse": [HookMatcher(hooks=[이 콜백])]} 으로 옵션에 주입한다.
    """
    async def hook(input_data, tool_use_id, context) -> dict:
        data = input_data if isinstance(input_data, dict) else {}
        audit.record(
            "tool_use",
            tool=data.get("tool_name"),
            tool_input=data.get("tool_input"),
            tool_use_id=tool_use_id,
        )
        return {}

    return hook
