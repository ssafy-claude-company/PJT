"""[Core] 도구명 상수 — MCP 도구 식별자·역할별 도구셋. 매체-중립 leaf 모듈.
guide_tools에서 분리: organt_runtime.builder가 guide_tools를 통째로 import하던 결합을 끊어
run 도구를 organt_runtime(Organt 개인)으로 옮길 수 있게 한다(순환 차단)."""

ORIGIN = 0
REQUEST_TOOL = "mcp__guide__request"
RECRUIT_TOOL = "mcp__guide__recruit"
RUN_TOOL = "mcp__guide__run"
# 모든 Organt 공통 흐름 도구(요청/채용/실행검증). 리더 전용 셋업 도구는 LEADER_TOOLS.
FLOW_TOOLS = [REQUEST_TOOL, RECRUIT_TOOL, RUN_TOOL]
# 리더(코디네이터) 흐름 도구: 조율만(run 없음) — 구현·실행은 owner/QA가 한다.
COORD_TOOLS = [REQUEST_TOOL, RECRUIT_TOOL]
LEADER_TOOLS = [f"mcp__guide__{n}" for n in
                ("create_project", "create_task", "set_goal", "complete_task", "deploy", "send_file",
                 "vote", "meet", "parallel_work")]
