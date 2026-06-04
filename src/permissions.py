"""Organt 권한 통제: PreToolUse 훅으로 권한 밖 도구·작업공간 밖 접근을 차단한다.

Step 2 증명의 '권한 밖 툴 호출 시 훅이 차단하고 거부 사유가 로그에 남는다'를 담당한다.
"""
import os


def organt_allowed_tools(extra_tool_names=()):
    """Organt 공통 허용 도구: 파일(Read/Write/Edit) + 탐색(Glob) + 도구로딩(ToolSearch).

    동료 위임은 서브에이전트(Task/Agent)가 아니라 guide의 `request` 도구로 한다 — 그런
    흐름 도구(request / 리더의 create_project·create_task)는 호출부에서 extra_tool_names로
    더한다. 그 외(Bash, Web 등)는 PreToolUse 훅이 차단한다.
    """
    return ["Read", "Write", "Edit", "Glob", "ToolSearch", *extra_tool_names]


def _within(cwd, target) -> bool:
    """target 경로가 cwd 안(또는 cwd 자신)인지."""
    try:
        cwd_r = os.path.realpath(cwd)
        tgt = target if os.path.isabs(target) else os.path.join(cwd_r, target)
        tgt_r = os.path.realpath(tgt)
        return tgt_r == cwd_r or tgt_r.startswith(cwd_r + os.sep)
    except (OSError, ValueError):
        return False


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def make_pre_tool_use_hook(audit, allowed, actor=None, role=None):
    """허용 도구만 통과시키고, 파일 쓰기는 작업공간 안으로 제한하는 PreToolUse 훅.

    actor/role를 주면 거부 이벤트에도 '누가' 시도했는지 남는다 — 협업 관찰성."""
    allowed_set = set(allowed)

    async def hook(input_data, tool_use_id, context) -> dict:
        data = input_data if isinstance(input_data, dict) else {}
        tool = data.get("tool_name")
        tool_input = data.get("tool_input") or {}

        # 1) 허용 도구만 통과
        if tool not in allowed_set:
            audit.record("tool_denied", actor=actor, role=role, tool=tool,
                         reason="권한 밖 도구", tool_use_id=tool_use_id)
            return _deny(f"'{tool}' 은(는) Organt 허용 도구가 아닙니다.")

        # 2) 파일 쓰기는 작업공간(cwd) 안으로 제한
        if tool in ("Write", "Edit"):
            path = tool_input.get("file_path") or tool_input.get("path")
            cwd = data.get("cwd") or os.getcwd()
            if path and not _within(cwd, path):
                audit.record("tool_denied", actor=actor, role=role, tool=tool,
                             reason="작업공간 밖 경로", path=path, tool_use_id=tool_use_id)
                return _deny(f"작업공간 밖 경로에는 쓸 수 없습니다: {path}")

        return {}

    return hook
