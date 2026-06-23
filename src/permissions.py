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


# [파일 도메인 신호 — 흡수 게이트 file-aware(2026-06-23)] 공유 _CAPS의 need는 자연어 본문(한국어)용이라
# 코드 파일 경로/내용(영어·확장자)에선 도메인이 새어(예: model/train.js → AI인데 한국어 키워드 0). 그래서
# _CAPS 능력명별로 *파일 지향* 신호를 더해 교차도메인 Write를 식별한다. 키워드는 *구체적*으로만 — 'model'·
# 'recommend' 같은 일반어는 프론트의 ORM 인터페이스·추천 UI를 오판(false-positive)하므로 제외, 명확한 ML/
# 파이프라인/DevOps 신호만 넣는다(자기 도메인 Write를 막던 종전 마비를 되살리지 않기 위함).
_FILE_CAP_KW = {
    "AI/ML(모델 학습·예측)": (
        "train", "predict", "inference", "neural", "tensorflow", "pytorch",
        "torch", "sklearn", "scikit", "keras", ".ipynb", "model.fit", "model.predict",
        "딥러닝", "머신러닝", "신경망"),
    "실데이터 수집·파이프라인": (
        "pipeline", "etl", "crawl", "scrape", "ingest", "공공데이터", "fetch_data"),
    "데이터 영속·DB": (
        "schema.sql", "migration", "alembic", "create table", "createtable"),
    "배포·인프라(DevOps)": (
        "dockerfile", "docker-compose", "kubernetes", "terraform", "helm", "ci/cd", "cicd"),
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

        # 9) [흡수 차단 — '모르는 일까지 하지 말 것'(2026-06-21, 사용자 규명)] 어떤 행위자든(리더든 owner든)
        #    자기 도메인 *밖*의 일을, 그 도메인 전문가가 놀고 있는데 대신 하면 = 흡수다(P-026: 백엔드가 AI 엔지니어
        #    모델까지 다 씀; 사용자 "전문가가 놀고 있으면 대기하든가 왜 모르는 일까지 하는거야"). 신호: 이 Task 팀에
        #    '나와 도메인이 안 겹치는(distinct) + 실작업 0(idle) + 아직 Work 위임 0 + 도달 가능'한 동료가 있으면, 그건
        #    그 동료가 할 일을 내가 흡수하려는 상황 → Write/Edit 차단. request(Work)로 그에게 맡기거나(일하면 풀림)
        #    끝낼 때까지 대기하게 한다. 도메인이 같으면(동질) 차단 안 함(같은 분야끼리는 흡수 아님). [교착 방지]
        #    도달 가능자만 — 없으면 통과(맡길 사람 없으면 직접). [반-스래싱] 한 번 위임하면(work_delegated_to 진입)
        #    그 동료는 더는 블로커가 아니라 본인이 일 안 해도 진행 가능(기회는 줬다). [하위호환] _info 없거나 팀 빈
        #    흐름·테스트는 건너뜀(도메인 판정 불가).
        if (tool in ("Write", "Edit") and flow is not None and actor is not None
                and getattr(flow, "current", None) is not None
                and callable(getattr(flow, "_info", None))
                and (getattr(flow.current, "team", None) or [])):
            def _jobset(m):
                return {" ".join(j.split()).casefold()
                        for j in str((flow._info(m) or "")).split("·") if j.strip()}
            _mine = _jobset(actor)
            # [테스트 파일 면제 — 테스트는 흡수 아님(2026-06-23, 사용자)] *테스트/검증 파일*을 쓰는 건 그 도메인을
            # **검증**하는 것이지 **구현**(흡수)이 아니다. 테스트는 자기가 검증하는 도메인을 자연히 *언급*하므로
            # (라이브 규명: QA의 qa_test.js 주석 '비로그인'이 _CAPS의 DB 능력으로 오판돼 차단됨) 키워드 분류가
            # 거짓양성을 낸다. 파일명이 테스트 관례면 게이트를 건너뛴다 — *구현* 파일(server.js 등)엔 그대로 적용해
            # 'QA가 구현 흡수'는 여전히 막는다(검증자 전면 면제 아님 — 그건 QA 오버리치를 되살림).
            _fp = str(tool_input.get("file_path") or tool_input.get("path") or "").lower()
            _fname = _fp.rsplit("/", 1)[-1]
            _is_testfile = (any(s in _fname for s in ("test_", "_test", ".test", "test.", "qa_", ".spec", "spec."))
                            or "/tests/" in _fp or "/test/" in _fp or "/__tests__/" in _fp)
            if _mine and not any(x.startswith("예비") for x in _mine) and not _is_testfile:
                eng2 = getattr(getattr(flow, "comm", None), "engagement", None)
                scope2 = getattr(getattr(flow, "comm", None), "scope", None)
                abby2 = getattr(flow, "act_by", None) or {}
                deleg = getattr(flow.current, "work_delegated_to", None) or set()

                _lead = getattr(flow, "leader", None)
                # [사이클 방지] 지금 베턴 사슬에 들어와 있는 멤버(위임하고 잠든 상위자 포함)는 후보에서 제외 —
                # 안 그러면 A가 자기를 깨운 상위자 B(act_by 0·미수신이라 idle처럼 보임)에게 되위임하려다 교착.
                # 사슬 밖 '전혀 손 안 탄' 전문가만 흡수 대상으로 본다.
                _stack = getattr(getattr(flow, "comm", None), "open_requests", None) or []
                _engaged = set()
                for _fr in _stack:
                    for _at in ("from_id", "to_id"):
                        _v = getattr(_fr, _at, None)
                        if _v is not None:
                            _engaged.add(_v)

                def _idle_distinct_reach(m):
                    # 리더는 '흡수당하는 전문가' 후보가 아니다(조율자 — idle이 정상). actor 자신·사슬 참여·이미 위임·일한 멤버 제외.
                    if m == actor or m == _lead or m in _engaged or m in deleg or abby2.get(m, 0) != 0:
                        return False
                    lbl = str(flow._info(m) or "")
                    if lbl.startswith("예비") or not lbl.strip():
                        return False
                    mj = _jobset(m)
                    if not mj or (mj & _mine):          # 도메인 미상 또는 내 직군과 겹침 → 흡수 아님
                        return False
                    if eng2 is not None and scope2 is not None:
                        try:
                            if eng2.busy_elsewhere(m, scope2):
                                return False
                        except Exception:
                            pass
                    return True

                idle_specialists = [m for m in (getattr(flow.current, "team", None) or [])
                                    if _idle_distinct_reach(m)]
                # [file-domain 인지 — 자기 도메인 Write는 통과(2026-06-23, 사용자: '전문가는 자기 도메인
                #  야심껏')] 종전 게이트는 *쓰는 파일을 안 보고* idle 전문가가 하나라도 있으면 행위자의
                #  *자기 도메인* Write까지 막았다(라이브 규명: SYS가 배정한 프론트 전문가가 idle 동료 10명
                #  때문에 21분간 0파일 — 협업 마비). 흡수란 '*다른* 도메인 일을 대신함'이므로, 쓰는 *파일*이
                #  요구하는 능력(_CAPS) 중 행위자 직군이 못 덮고 idle 전문가가 덮는 게 있을 때만 흡수로 보고
                #  막는다. 자기 도메인 파일(교차 능력 신호 0)은 자유 — owner는 받은 일을 야심껏 구현한다.
                if idle_specialists:
                    try:
                        from .guide_tools import _CAPS
                        _ftext = " ".join(str(tool_input.get(_k) or "") for _k
                                          in ("file_path", "path", "content", "new_string")).lower()
                        _alabel = str(flow._info(actor) or "").lower()
                        # 파일이 요구하는 능력 중 *행위자 직군이 못 덮는* 것(= 교차도메인 신호). _CAPS need(자연어)
                        # 와 _FILE_CAP_KW(파일 지향 신호)를 함께 본다. 행위자가 덮는 능력은 자기 도메인 → 스킵.
                        _file_caps = set()
                        for _n, _need, _cov in _CAPS:
                            if _cov(_alabel):
                                continue
                            if _need(_ftext) or any(kw in _ftext for kw in _FILE_CAP_KW.get(_n, ())):
                                _file_caps.add(_n)
                        if not _file_caps:
                            idle_specialists = []          # 자기 도메인(교차 신호 0) → 흡수 아님, 통과
                        else:                              # 파일이 idle 전문가 능력 요구 → 그 전문가만 남김
                            idle_specialists = [m for m in idle_specialists
                                                if any(_cov(str(flow._info(m) or "").lower())
                                                       for _n, _need, _cov in _CAPS if _n in _file_caps)]
                    except Exception:
                        pass                               # 능력 판정 불가 시 종전 동작 보존(best-effort)
                if idle_specialists:
                    _nm = ", ".join(str(flow._info(m) or m) for m in idle_specialists)
                    audit.record("tool_denied", actor=actor, role=role, tool=tool,
                                 reason="타 도메인 전문가 일 흡수(전문가 idle)", tool_use_id=tool_use_id)
                    return _deny(
                        f"흡수 차단: 당신과 도메인이 다른 전문가 [{_nm}]가 이 Task에서 아직 아무 일도 안 하고(idle) "
                        f"위임도 못 받았습니다 — 그들의 도메인 일을 당신이 대신 하지 마세요(모르는 도메인을 대신 = 흡수, "
                        f"그 분야 깊이가 안 납니다). request(Work)로 그들에게 맡기거나(그가 일하면 자동으로 풀립니다) "
                        f"끝낼 때까지 대기하세요. 정말 불필요하면 한 번 위임해 본인이 판단하게 하세요(위임 후엔 진행 가능). "
                        f"전원 도달 불가하면 인프라 문제이니 사용자에게 보고(혼자 떠안지 말 것).")

        # 10) [막힘 흡수 차단 — '같은 사람 재요청'(2026-06-21, 사용자 규명)] 하위 담당이 막혀 베턴이 위임자에게
        #     되돌아온 순간(guide의 baton_recover가 flow._stall_victim에 막힌 사람을 기록), 위임자가 '내가 하지'로
        #     그 사람 일을 흡수하던 구멍(P-027: 백엔드 막힘 → AI엔지니어가 백엔드 Node 서버 대신 작성 → 백엔드
        #     Python 867줄 고아화). 막힌 사람과 도메인이 다른 행위자의 Write/Edit를 막아 '내가 하지'를 차단하고,
        #     재채용(양산 위험)이 아니라 '그 사람에게 request(Work)로 이어서 해'(같은 사람 재요청)를 유도한다.
        #     [해제] 막힌 사람이 다시 act하면(act_by 증가=돌아옴) 즉시 풀림. [교착 방지] 끝내 무응답이면 N회 차단
        #     후 폴백(victim 비우고 통과 — 진짜 죽은 동료에 빌드가 얼지 않음). 같은 도메인이면 차단 안 함(흡수 아님).
        if (tool in ("Write", "Edit") and flow is not None and actor is not None
                and getattr(flow, "_stall_victim", None) is not None
                and flow._stall_victim != actor
                and callable(getattr(flow, "_info", None))):
            v = flow._stall_victim
            _abby = getattr(flow, "act_by", None) or {}
            if _abby.get(v, 0) > getattr(flow, "_stall_victim_acts", 0):
                flow._stall_victim = None          # 막힌 사람이 다시 일함(돌아옴) → 보호 해제
            else:
                def _jset(m):
                    return {" ".join(j.split()).casefold()
                            for j in str(flow._info(m) or "").split("·") if j.strip()}
                _mine = _jset(actor); _vj = _jset(v)
                if _mine and _vj and not (_mine & _vj):   # 막힌 사람이 나와 도메인이 다를 때만(같은 분야면 흡수 아님)
                    flow._stall_blocks = getattr(flow, "_stall_blocks", 0) + 1
                    if flow._stall_blocks > 3:
                        flow._stall_victim = None      # 폴백: N회 막아도 안 돌아오면 통과(교착 방지)
                    else:
                        audit.record("tool_denied", actor=actor, role=role, tool=tool,
                                     reason="막힌 동료 일 흡수(재요청 대신 대신함)", tool_use_id=tool_use_id)
                        return _deny(
                            f"막힘 흡수 차단: 동료 [{flow._info(v) or v}]가 맡은 일을 하다 막혔습니다 — 그 일을 당신이 "
                            f"'내가 하지'로 대신 만들면 그 사람 작업이 통째로 버려집니다(P-027 실패: 백엔드 일을 다른 봇이 "
                            f"대신 만들어 867줄 폐기). request(Work)로 그 사람에게 '이어서 마저 해'를 다시 보내 기다리세요 "
                            f"— **같은 사람 재요청**이지 새로 뽑거나(recruit) 당신이 대신하는 게 아닙니다. 그 사람이 다시 "
                            f"손대면 자동으로 풀려 당신 일도 이어집니다. (끝내 무응답이면 인프라 문제이니 사용자에게 보고.)")

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
