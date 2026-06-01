"""Organt 서브에이전트 정의.

메인 Organt는 요청을 받고, 실작업은 서브에이전트에 위임(Task 툴)한다.
서브에이전트는 격리된 컨텍스트에서 작업한 뒤 결과를 메인에 보고하고,
메인이 그 결과를 종합한다.
"""
from claude_agent_sdk import AgentDefinition

WRITER = "writer"


def organt_subagents():
    """Organt가 위임할 수 있는 서브에이전트 목록."""
    return {
        WRITER: AgentDefinition(
            description="파일 작성·정리 등 실작업을 처리하는 Organt의 작업자. 메인이 위임할 때 사용.",
            prompt=(
                "당신은 Organt의 작업자(subagent)입니다. "
                "위임받은 작업만 현재 작업공간 안에서 상대경로 파일로 처리하고, "
                "결과를 한 줄로 보고합니다."
            ),
            tools=["Read", "Write", "Edit"],
            maxTurns=8,
        ),
    }
