"""Audit 로그: 모든 흐름(수집·라우팅·툴 호출·응답)을 JSONL 한 줄씩 남긴다.

Step 1 증명의 '왕복이 로그에 그대로 남는다'를 담당한다.
"""
import json
import time
from pathlib import Path


def redact_tool_input(tool_input):
    """감사에 파일 *내용 전체*를 남기지 않는다 — Write/Edit의 content/new_string/old_string을 길이 요약으로
    대체(경로·도구명은 보존). 민감 내용 유출·audit 비대화 방지(보안 핫픽스 2026-06). 다른 필드는 그대로."""
    if not isinstance(tool_input, dict):
        return tool_input
    out = dict(tool_input)
    for k in ("content", "new_string", "old_string"):
        v = out.get(k)
        if isinstance(v, str) and len(v) > 80:
            out[k] = f"<{len(v)} chars 생략>"
    return out


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


def make_post_tool_use_hook(audit: AuditLog, actor=None, role=None, flow=None):
    """Organt의 모든 툴 호출을 audit에 남기는 PostToolUse 훅 콜백을 만든다.

    actor/role를 주면 '누가(어느 봇·역할)' 그 툴을 호출했는지 기록한다 — 협업 관찰성.
    flow를 주면 툴 '완료' 시점에도 무진행 시계(last_activity)를 갱신한다 — 오래 걸리는 단일 작업
    (예: 빌드·설치 run)이 시작(PreToolUse)만 찍히고 도중에 '행'으로 오인돼 잘리는 것을 막는다.
    hooks={"PostToolUse": [HookMatcher(hooks=[이 콜백])]} 으로 옵션에 주입한다.
    """
    async def hook(input_data, tool_use_id, context) -> dict:
        data = input_data if isinstance(input_data, dict) else {}
        if flow is not None:                 # 도구 완료도 '진행' 신호 — 긴 단일 작업 보호
            try:
                flow.last_activity = time.monotonic()
            except Exception:
                pass
        audit.record(
            "tool_use",
            actor=actor,
            role=role,
            tool=data.get("tool_name"),
            tool_input=redact_tool_input(data.get("tool_input")),
            tool_use_id=tool_use_id,
        )
        return {}

    return hook
