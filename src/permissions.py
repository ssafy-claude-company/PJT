"""Organt 권한 통제: PreToolUse 훅으로 권한 밖 도구·작업공간 밖 접근을 차단한다.

Step 2 증명의 '권한 밖 툴 호출 시 훅이 차단하고 거부 사유가 로그에 남는다'를 담당한다.
"""
import os
import time


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

        # 진행 신호: 어떤 도구 호출이든 흐름의 '무진행 워치독' 시계를 갱신(행 오판 방지).
        if flow is not None:
            try:
                flow.last_activity = time.monotonic()
            except Exception:
                pass

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

        # 4) 이미 owner에게 Work로 위임된 Task의 산출물은 그 owner가 구현한다 — '리더'가 대신 Write/Edit하면
        #    거부(전문가 도메인 대리구현=독점, 그리고 owner가 일하는 중 리더가 앞질러 만들고 허위완료하는 패턴
        #    차단). owner가 늦거나 막히면 직접 떠안지 말고 request(Work) 재위임으로 기다리거나 recruit/재배정.
        #    리더가 위임 없이 자기 도메인을 직접 하는 Task는 owner==0이라 막지 않는다(리더도 한 직원).
        if (tool in ("Write", "Edit") and flow is not None and actor is not None
                and getattr(flow, "current", None) is not None
                and flow.current.owner and flow.current.owner != actor
                and actor == getattr(flow, "leader", None)):
            audit.record("tool_denied", actor=actor, role=role, tool=tool,
                         reason="위임된 owner 도메인 대리구현", tool_use_id=tool_use_id)
            return _deny(
                f"이 Task는 owner({flow.current.status.owner or flow.current.owner})에게 위임돼 있습니다 — "
                f"그 전문가의 산출물을 리더가 대신 만들지 마세요(독점·허위완료 금지). owner에게 request(Work)로 "
                f"맡겨 끝내게 하고(기다리세요), 끝내 무응답이면 recruit/재배정하세요. 직접 구현은 당신이 owner인 "
                f"(위임하지 않은) Task에서만.")

        # 5) 개입(기존 프로젝트 수정)도 '목표 먼저' — Task의 Goal이 확정되기 전엔 파일 수정 금지. 개입에서
        #    리더가 재현·합의 없이 개인 견해로 즉흥 수정하던 걸 구조적으로 차단(Purpose/Goal 없이 끝나는 문제).
        if (tool in ("Write", "Edit") and flow is not None and getattr(flow, "intervention", None)):
            cur = getattr(flow, "current", None)
            goal = (cur.status.goal or "").strip() if (cur and getattr(cur, "status", None)) else ""
            if not goal:
                audit.record("tool_denied", actor=actor, role=role, tool=tool,
                             reason="개입 목표 미확정 선수정", tool_use_id=tool_use_id)
                return _deny("개입 수정 거부: 먼저 create_task + set_goal로 Purpose·Goal을 확정한 뒤 고치세요 — "
                             "run으로 증상을 재현·확인하고 목표를 합의하기 전에 개인 견해로 즉흥 수정하지 마세요.")

        # 6) 리더 독식 차단(중앙집권의 핵심 구멍): 팀(다른 도메인 동료)이 있는 Task에서, 리더가 구현을
        #    '위임(Work) 없이' 혼자 다 쓰는 걸 막는다. 기존 #4 훅은 '위임된 owner 도메인 침범'만 잡아서,
        #    리더가 Info로 자문만 받고 한 번도 위임 안 하면(owner 미설정) 통째로 우회됐다. → 팀이 있으면
        #    리더는 한 파일(grace) 직접 쓴 뒤부턴 구현을 동료에게 request(Work)로 위임해야 한다.
        if (tool in ("Write", "Edit") and flow is not None and actor is not None
                and actor == getattr(flow, "leader", None)
                and getattr(flow, "current", None) is not None):
            cur = flow.current
            others = [m for m in getattr(cur, "team", []) if m != flow.leader]
            if others and getattr(cur, "work_delegated", 0) == 0 and getattr(cur, "leader_writes", 0) >= 1:
                audit.record("tool_denied", actor=actor, role=role, tool=tool,
                             reason="리더 독식(위임 없이 단독 구현)", tool_use_id=tool_use_id)
                return _deny(
                    "리더 단독 구현 차단: 이 Task엔 도메인 동료들이 있는데 당신이 위임(Work) 없이 혼자 다 만들고 "
                    "있습니다(중앙집권·독점). 나머지 구현은 적합한 도메인 동료에게 request(Work)로 맡기세요 — "
                    "owner가 자기 도메인을 구현합니다. 당신은 조율·통합·검증(run)·자기 도메인 일부만. "
                    "동료가 무응답이면 그건 인프라 문제니 사용자에게 보고하세요(혼자 떠안지 말 것).")
            cur.leader_writes = getattr(cur, "leader_writes", 0) + 1   # 통과한 리더 직접작성 집계

        # 작업공간을 실제로 바꾸는 도구(run/Write/Edit)는 act_count로 누계 — request 도구가 wake 전후 차이로
        # 'owner가 위임 도중 실제로 일했나'를 판정해 허위완료/독점을 막는다. deny를 모두 통과한 뒤에만 집계.
        if tool in ("Write", "Edit", "mcp__guide__run") and flow is not None:
            try:
                flow.act_count += 1
            except Exception:
                pass

        return {}

    return hook
