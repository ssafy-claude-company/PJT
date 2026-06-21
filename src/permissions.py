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


# [네이티브 도구 → Organt 도구 리다이렉트] 봇은 Claude라 훈련된 기본 CLI 도구(Bash·Agent·Task*·
# TodoWrite 등)를 본능적으로 집는데, Organt는 게이트가 걸린 대체 도구만 노출한다. 그냥 '허용 아님'
# 이라고만 하면 봇이 어디로 가야 할지 몰라 표류한다 — 라이브: '권한 밖 도구' 거부 359건(대부분 Bash·
# TaskList·Agent), 그중 Bash 거부 163건의 74%(120건)가 run으로 복귀하지 못함(턴 낭비·베턴 고아의
# 한 원인). 거부에 '대신 이걸 써라'를 붙여 즉시 올바른 도구로 유도한다(프롬프트 강화보다 실효 — 본능을
# 이기는 게 아니라 본능을 받아 redirect).
_TOOL_REDIRECT = {
    "Bash": "셸 명령은 `run`(mcp__guide__run)으로 실행하세요 — 같은 command를 run으로 다시 부르면 됩니다(Organt는 게이트가 걸린 run만 씁니다).",
    "Agent": "일을 맡길 땐 서브에이전트가 아니라 `request`로 동료에게 위임하세요(To: 동료, Kind: Work/Info).",
    "Task": "서브에이전트(Task) 대신 `request`로 동료에게 위임하세요.",
    "TaskList": "별도 작업관리 도구는 없습니다 — 현재 Task·상태는 채널 상태블록에서 보고, Task는 리더가 create_task/complete_task로 다룹니다.",
    "TaskGet": "별도 작업조회 도구는 없습니다 — 현황은 채널 상태블록에서 봅니다.",
    "TaskUpdate": "별도 작업갱신 도구는 없습니다 — 진행은 실제 작업(run/Write/Edit)과 Response로 드러내세요.",
    "TodoWrite": "별도 할일 도구는 없습니다 — 계획은 Response로, 진행은 실제 작업으로.",
    "SendUserFile": "사용자에게 파일을 직접 보내지 않습니다 — 결과는 Response로 보고하고, 웹 산출물은 `deploy`로 배포하세요.",
    "Skill": "Skill 도구는 없습니다 — 필요한 일은 허용 도구(run/Write/Edit/request 등)로 직접 하세요.",
    "NotebookEdit": "노트북 편집 도구는 없습니다 — 파일은 Write/Edit로.",
    "MultiEdit": "MultiEdit는 없습니다 — Edit를 여러 번 쓰세요.",
}


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
            hint = _TOOL_REDIRECT.get(tool, "")
            return _deny(f"'{tool}' 은(는) Organt 허용 도구가 아닙니다." + (" " + hint if hint else ""))

        # 2) 파일 쓰기는 작업공간(cwd) 안으로 제한
        if tool in ("Write", "Edit"):
            path = tool_input.get("file_path") or tool_input.get("path")
            cwd = data.get("cwd") or os.getcwd()
            if path and not _within(cwd, path):
                audit.record("tool_denied", actor=actor, role=role, tool=tool,
                             reason="작업공간 밖 경로", path=path, tool_use_id=tool_use_id)
                return _deny(f"작업공간 밖 경로에는 쓸 수 없습니다: {path}")

        # 2.5) [쓰기 리스] 리스(flow.write_lease)가 배정된 행위자는 그 샌드박스 안에만 쓴다 — 병렬
        #      가지 간·본 작업물과의 파일 충돌이 구조적으로 불가능. 현재 호출부 없음(휴면 인프라 —
        #      병렬 Work/alive-집합 도입 시 재사용; 리스가 비면 비용 0).
        if tool in ("Write", "Edit") and flow is not None and actor is not None:
            lease = (getattr(flow, "write_lease", None) or {}).get(actor)
            path = tool_input.get("file_path") or tool_input.get("path")
            if lease and path:
                # 다중 리스: 병렬 Work(parallel_work)는 가지마다 '파일 목록' 리스를 배정한다(RFC-006).
                leases = list(lease) if isinstance(lease, (list, tuple)) else [lease]
                cwd = data.get("cwd") or os.getcwd()
                tgt = path if os.path.isabs(path) else os.path.join(cwd, path)
                if not any(_within(l, tgt) for l in leases):
                    audit.record("tool_denied", actor=actor, role=role, tool=tool,
                                 reason="쓰기 리스 밖", path=path, tool_use_id=tool_use_id)
                    return _deny(f"[쓰기 리스] 당신의 산출물은 배정된 영역 안에만 씁니다: {', '.join(map(str, leases))} "
                                 f"(시도한 경로: {path}) — 영역 밖 파일은 Read로 참고만 하고, 필요한 변경은 "
                                 f"보고의 [리스크/요청] 항목으로 알리세요(겹침 방지가 병렬의 전제).")

        # 3) 구현(Write/Edit)은 'Work 위임 맥락'에서만 — 협의(Info)로 깨워진 동료의 선구현 차단.
        #    나를 깨운 베턴 프레임(top, to=나)이 Work면 통과, Info면 거부. → 리더(origin Work)·
        #    Work 위임받은 owner는 구현 가능, Info 협의 중 동료는 '제안(Response)'만. 구조적으로
        #    '협의 → 합의(set_goal) → 위임(Work) → 구현(Write)' 순서를 강제(선구현 불가).
        #    fork 수집 가지(표결·회의 1라운드)는 comm 프레임을 열지 않으므로 flow.fork_kind가 같은
        #    게이트를 잇는다 — Info 가지의 선구현도 동일 차단(Work 가지 통과 경로는 휴면).
        if tool in ("Write", "Edit") and flow is not None and actor is not None:
            fk = (getattr(flow, "fork_kind", None) or {}).get(actor)
            stack = flow.comm.open_requests
            top = stack[-1] if stack else None
            woke_info = ((fk is not None and not _is_work_kind(fk))
                         or (fk is None and top is not None and top.to_id == actor
                             and not _is_work_kind(top.kind)))
            if woke_info:
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

        # 7) 개입 독식 차단('Task 개입' 강제): 개입 흐름에서 리더가 run으로 혼자 재현·수정·검증을 다 하는 걸
        #    막는다(사용자가 본 '자기 혼자 다 함'). #5·#6은 Write/Edit만 잡아 run 솔로 thrash는 통과됐다. 개입은
        #    create_task→팀이 Goal→owner에게 Work 위임 구조로 가야 한다. (a) Task도 안 열고 run하면 즉시 차단,
        #    (b) Task는 열었어도 위임(Work) 0인 채 run을 3회 넘게 반복하면 차단 → owner에게 위임 강제. 위임이
        #    한 번이라도 일어나면(검증 단계) 풀어준다(리더의 최종 검증 run 허용).
        if (tool == "mcp__guide__run" and flow is not None and actor is not None
                and actor == getattr(flow, "leader", None)
                and getattr(flow, "intervention", None)):
            others = [m for m in (getattr(flow, "project_team", None) or [])
                      if m != flow.leader and not str((flow._info(m) or "")).startswith("예비")]
            if others:
                cur = getattr(flow, "current", None)
                if cur is None:
                    audit.record("tool_denied", actor=actor, role=role, tool=tool,
                                 reason="개입 Task 미개설 단독 실행", tool_use_id=tool_use_id)
                    return _deny(
                        "개입 단독 실행 차단: 먼저 create_task로 'Task 개입'을 여세요 — 혼자 run으로 재현·수정하지 "
                        "말고, 문제 도메인 동료를 members로 넣어 Task를 만들고 팀과 Goal을 합의한 뒤 그 owner에게 "
                        "request(Work)로 맡기세요(그 owner가 재현·수정·run 검증). 당신은 조율·통합·최종 검증만.")
                delegated = sum(getattr(t, "work_delegated", 0) for t in getattr(flow, "tasks", []))
                flow.leader_runs = getattr(flow, "leader_runs", 0) + 1
                if delegated == 0 and flow.leader_runs > 3:
                    audit.record("tool_denied", actor=actor, role=role, tool=tool,
                                 reason="개입 위임없이 단독 run 독식", tool_use_id=tool_use_id)
                    return _deny(
                        "개입 단독 실행 차단(독식): 도메인 동료가 있는데 Work 위임을 한 번도 안 하고 혼자 run으로 "
                        "재현·수정·검증을 다 하고 있습니다(사용자가 지적한 '리더 혼자 다 함'). 문제 도메인 owner에게 "
                        "request(Work)로 맡기세요 — 그 owner가 직접 재현·수정·run 검증합니다. 당신은 조율·통합·최종 "
                        "검증만(혼자 다 하지 말 것). 동료 무응답이면 인프라 문제이니 사용자에게 보고.")

        # 8) [리더 흡수 차단 — 상대적] #6·#7은 'delegated==0'(한 번도 위임 안 함)만 잡아, '한두 번 위임해놓고
        #    나머지를 리더가 통째로 흡수'하는 패턴(P-026 실측: 일부 위임 후 리더가 혼자 255회 run)은 우회됐다.
        #    구조적 신호: 코디네이터의 직접 doing(act_by 리더)이 '팀 전체의 doing 합'을 넘으면 그건 분배가 아니라
        #    흡수다(리더가 팀보다 더 많이 일함 = 중앙집권). grace(lead_act>=8)로 초기 셋업·통합은 허용하되, 그
        #    이후 리더가 팀 합을 앞지르면 Write/Edit/run을 막아 검증은 QA·구현은 owner에게 위임을 강제한다.
        #    [교착 방지] 도달 가능한(예비 아님·타 흐름 비점유) 동료가 실제로 있을 때만 차단 — 솔로/전원 바쁨이면 통과.
        #    [자가치유] 위임이 일어나면 그 owner/QA의 act_by가 올라 팀 합이 늘고, 곧 리더가 다시 풀린다(분배 리듬).
        if (tool in ("Write", "Edit", "mcp__guide__run") and flow is not None and actor is not None
                and actor == getattr(flow, "leader", None)
                and getattr(flow, "current", None) is not None):
            abby = getattr(flow, "act_by", None) or {}
            lead_act = abby.get(actor, 0)
            team_act = sum(v for k, v in abby.items() if k != actor)
            if lead_act >= 8 and lead_act > team_act:
                eng = getattr(getattr(flow, "comm", None), "engagement", None)
                scope = getattr(getattr(flow, "comm", None), "scope", None)
                def _reachable(m):
                    if m == actor or str((flow._info(m) or "")).startswith("예비"):
                        return False
                    if eng is not None and scope is not None:
                        try:
                            if eng.busy_elsewhere(m, scope):
                                return False
                        except Exception:
                            pass
                    return True
                peers = [m for m in (getattr(flow, "project_team", None) or []) if _reachable(m)]
                if peers:
                    audit.record("tool_denied", actor=actor, role=role, tool=tool,
                                 reason="리더 흡수(팀 합보다 많이 doing)", tool_use_id=tool_use_id)
                    return _deny(
                        "리더 흡수 차단: 당신(코디네이터)이 팀 전체보다 더 많이 직접 doing하고 있습니다"
                        f"(리더 {lead_act}회 vs 팀 합 {team_act}회) — 리더의 일은 분배·조율이지 혼자 검증·디버깅·"
                        "구현이 아닙니다(직접 일함 = 흡수). 검증은 QA에게, 수정·구현은 해당 도메인 owner에게 "
                        "request(Work)로 위임하세요. 위임이 늘면 팀 활동이 올라 자연히 다시 풀립니다. "
                        "동료가 무응답이면 인프라 문제이니 사용자에게 보고하세요(혼자 떠안지 말 것).")

        # 작업공간을 실제로 바꾸는 도구(run/Write/Edit)는 act_count로 누계 — request 도구가 wake 전후 차이로
        # 'owner가 위임 도중 실제로 일했나'를 판정해 허위완료/독점을 막는다. deny를 모두 통과한 뒤에만 집계.
        if tool in ("Write", "Edit", "mcp__guide__run") and flow is not None:
            try:
                flow.act_count += 1
                # 행위자별 귀속도 함께 — 위임 측정창에서 '요청자 자신의 활동'(detach 후 리더의 폴링
                # run 등)을 빼고 재기 위함(단일활성이 흔들린 순간에도 인도/이어가기 신호가 오염되지 않게).
                if actor is not None and getattr(flow, "act_by", None) is not None:
                    flow.act_by[actor] = flow.act_by.get(actor, 0) + 1
                # [메커니즘② 저작 다양성] 파일 저작(Write/Edit, run 제외)을 '직군별'로 누계 — 완료 게이트가
                # '한 직군이 다 써버린 모놀리스'(도메인 전문가 부재 신호)를 잡는다. run은 검증/배포라 제외.
                if tool in ("Write", "Edit") and actor is not None \
                        and getattr(flow, "writes_by_role", None) is not None:
                    _role = str((getattr(flow, "bot_info", None) or {}).get(actor, "") or "?").split("·")[0].strip() or "?"
                    flow.writes_by_role[_role] = flow.writes_by_role.get(_role, 0) + 1
                # [일로 직업 획득 — 영속 승격] 잠정 채용된 봇이 *첫 실작업*(Write/Edit/run)을 하면 그 순간 직군을
                # 영속한다 — jobs.json은 동기로(여기서), Discord 역할은 SYS가 비동기로(role_earned_queue 드레인).
                # '직업=기억': 일한 봇만 직업이 박힌다. 끝까지 일 안 한 채용은 영속 안 돼 다음 흐름에 예비로 사라짐.
                if actor is not None and getattr(flow, "tentative_roles", None):
                    _trole = flow.tentative_roles.pop(actor, None)
                    if _trole:
                        _label = (getattr(flow, "bot_info", None) or {}).get(actor) or _trole
                        if getattr(flow, "persist_role", None):
                            try:
                                flow.persist_role(actor, _label)
                            except Exception:
                                pass
                        if getattr(flow, "role_earned_queue", None) is not None:
                            flow.role_earned_queue.append((actor, _label))
            except Exception:
                pass

        return {}

    return hook
