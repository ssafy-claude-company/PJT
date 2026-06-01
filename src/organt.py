"""Organt(LLM) 본체.

claude-agent-sdk의 ClaudeSDKClient로 Organt를 구동한다.
작업공간(cwd) 안에서 내장 파일 툴(Read/Write/Edit)로 파일을 다룬다.

기능4 범위: Organt 본체 구성 + 파일시스템 접근.
- 인격(CLAUDE.md)·세션 보존(resume)은 Step2,
- Discord 소통 툴은 기능5, audit 훅은 기능6에서 옵션 override로 붙인다.
"""
from typing import List, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

from .config import Config

# Organt 기본 인격(최소). Step2에서 CLAUDE.md 로딩으로 확장한다.
ORGANT_PERSONA = (
    "당신은 Discord 위에서 일하는 AI 직원 'Organt'입니다. "
    "요청을 받으면 현재 작업 디렉터리 안에서 '상대경로'로만 파일을 만들고 수정하며 일합니다 "
    "(절대경로 사용이나 작업공간 밖 탐색은 하지 않습니다). "
    "불필요한 단계 없이 요청만 처리하고, 간결하게 한국어로 답합니다."
)


def build_options(config: Config, **overrides) -> ClaudeAgentOptions:
    """Organt용 ClaudeAgentOptions를 만든다.

    기능5·6에서 mcp_servers/hooks/allowed_tools 등을 override로 주입한다.
    """
    opts = dict(
        model=config.model,                       # None이면 SDK 기본 모델
        system_prompt=ORGANT_PERSONA,
        cwd=str(config.workspace_dir),            # 작업공간 안에서만 파일 작업
        allowed_tools=["Read", "Write", "Edit", "Bash"],  # 내장 파일/셸 툴(Step1 범위)
        permission_mode="acceptEdits",            # 파일 편집 자동 승인(권한 훅은 Step2)
        max_turns=16,                             # 작업당 턴 상한(폭주 방지)
    )
    opts.update(overrides)
    return ClaudeAgentOptions(**opts)


class Organt:
    """파일시스템에 접근하는 Organt(LLM) 본체."""

    def __init__(self, config: Config, options: Optional[ClaudeAgentOptions] = None):
        self.config = config
        self.options = options or build_options(config)

    async def handle(self, prompt: str) -> str:
        """요청 한 건을 처리하고 Organt의 최종 텍스트 응답을 돌려준다.

        지속 세션(resume)으로 State를 보존하는 것은 Step2 범위이므로,
        여기서는 요청마다 단발 세션으로 처리한다.
        """
        texts: List[str] = []
        async with ClaudeSDKClient(options=self.options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            texts.append(block.text)
        return "".join(texts).strip()
