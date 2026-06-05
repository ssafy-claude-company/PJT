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


def _is_work_kind(kind) -> bool:
    """베턴 프레임의 kind가 Work인지 (Kind enum 또는 'work'/'Work' 문자열 모두 인식)."""
    return str(getattr(kind, "value", kind)).strip().lower() == "work"


def make_pre_tool_use_hook(audit, allowed, actor=None, role=None, flow=None):
    """허용 도구만 통과시키고, 파일 쓰기는 작업공간 안으로 제한하는 PreToolUse 훅.

    actor/role를 주면 거부 이벤트에도 '누가' 시도했는지 남는다 — 협업 관찰성.
    flow를 주면 '협의(Info) 중 선구현'도 차단한다 — 구현은 Work 위임 맥락에서만."""
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

        # 3) 구현(Write/Edit)은 'Work 위임 맥락'에서만 — 협의(Info)로 깨워진 동료의 선구현 차단.
        #    나를 깨운 베턴 프레임(top, to=나)이 Work면 통과, Info면 거부. → 리더(origin Work)·
        #    Work 위임받은 owner는 구현 가능, Info 협의 중 동료는 '제안(Response)'만. 구조적으로
        #    '협의 → 합의(set_goal) → 위임(Work) → 구현(Write)' 순서를 강제(선구현 불가).
        if tool in ("Write", "Edit") and flow is not None and actor is not None:
            stack = flow.comm.open_requests
            top = stack[-1] if stack else None
            if top is not None and top.to_id == actor and not _is_work_kind(top.kind):
                audit.record("tool_denied", actor=actor, role=role, tool=tool,
                             reason="협의(Info) 중 선구현", tool_use_id=tool_use_id)
                return _deny("협의(Info) 단계에서는 구현(파일 작성)을 할 수 없습니다 — 제안은 "
                             "Response(말)로 하고, Goal 합의 후 Work로 위임받은 owner만 구현하세요.")

        return {}

    return hook
