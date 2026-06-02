"""Organt(LLM) 본체.

claude-agent-sdk의 ClaudeSDKClient로 Organt를 구동한다.
작업공간(cwd) 안에서 내장 파일 툴(Read/Write/Edit)로 파일을 다룬다.

기능4 범위: Organt 본체 구성 + 파일시스템 접근.
- 인격(CLAUDE.md)·세션 보존(resume)은 Step2,
- Discord 소통 툴은 기능5, audit 훅은 기능6에서 옵션 override로 붙인다.
"""
import dataclasses
import json
from pathlib import Path
from typing import Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
)

from .config import ROOT, Config

# Organt 기본 인격(최소). Step2에서 CLAUDE.md 로딩으로 확장한다.
ORGANT_PERSONA = (
    "당신은 Discord 위에서 일하는 AI 직원 'Organt'입니다. "
    "요청을 받으면 현재 작업 디렉터리 안에서 '상대경로'로만 파일을 만들고 수정하며 일합니다 "
    "(절대경로 사용이나 작업공간 밖 탐색은 하지 않습니다). "
    "불필요한 단계 없이 요청만 처리하고, 간결하게 한국어로 답합니다."
)

# Organt 인격 파일(CLAUDE.md) 경로. 인격·기억·Rule/Guide 목록을 담는다.
PERSONA_PATH = ROOT / "organt" / "CLAUDE.md"


def load_persona(path=None) -> str:
    """Organt 인격(CLAUDE.md)을 읽어 system_prompt로 쓴다. 없거나 비면 기본 인격."""
    p = Path(path) if path is not None else PERSONA_PATH
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return ORGANT_PERSONA
    return text or ORGANT_PERSONA


def _strip_decoration(text: str) -> str:
    """보고에서 장식용 수평선('---' 등)만 제거한다(내용은 보존)."""
    lines = [ln for ln in (text or "").splitlines()
             if ln.strip() not in ("---", "***", "___", "—", "──────")]
    return "\n".join(lines).strip()


def build_options(config: Config, **overrides) -> ClaudeAgentOptions:
    """Organt용 ClaudeAgentOptions를 만든다.

    기능5·6에서 mcp_servers/hooks/allowed_tools 등을 override로 주입한다.
    """
    opts = dict(
        model=config.model,                       # None이면 SDK 기본 모델
        system_prompt=load_persona(),             # CLAUDE.md 인격 로딩(없으면 기본)
        cwd=str(config.workspace_dir),            # 작업공간 안에서만 파일 작업
        allowed_tools=["Read", "Write", "Edit", "Bash"],  # 내장 파일/셸 툴(Step1 범위)
        permission_mode="acceptEdits",            # 파일 편집 자동 승인(권한 훅은 Step2)
        max_turns=16,                             # 작업당 턴 상한(폭주 방지)
    )
    opts.update(overrides)
    return ClaudeAgentOptions(**opts)


class Organt:
    """파일시스템에 접근하고, 세션 resume로 State를 보존하는 Organt(LLM) 본체."""

    def __init__(self, config: Config, options: Optional[ClaudeAgentOptions] = None,
                 state_path=None):
        self.config = config
        self.options = options or build_options(config)
        # State(작업 맥락)는 세션 ID로 보존한다. 재시작(새 인스턴스) 시 파일에서 복원.
        self.state_path = (Path(state_path) if state_path is not None
                           else config.audit_log_path.parent / "organt_state.json")
        self.session_id = self._load_session_id()

    def _load_session_id(self) -> Optional[str]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")).get("session_id")
        except (OSError, ValueError):
            return None

    def _save_session_id(self, sid: str) -> None:
        self.session_id = sid
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"session_id": sid}), encoding="utf-8")
        except OSError:
            pass

    def _options_for_call(self) -> ClaudeAgentOptions:
        """직전 세션이 있으면 resume를 붙인 옵션을 만든다."""
        if self.session_id:
            return dataclasses.replace(self.options, resume=self.session_id)
        return self.options

    async def handle(self, prompt: str) -> str:
        """요청 한 건을 처리하고 **최종 발화**(=보고/응답)만 돌려준다.

        턴마다의 중간 narration("이제 X 하겠습니다")은 버리고 마지막 메시지만 반환한다 →
        Response가 장식 없이 간결해진다. 직전 세션이 있으면 resume로 이어간다(State 보존).
        """
        final_text = ""
        captured_sid: Optional[str] = None
        async with ClaudeSDKClient(options=self._options_for_call()) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                sid = getattr(msg, "session_id", None)
                if sid:
                    captured_sid = sid
                if isinstance(msg, AssistantMessage):
                    t = "".join(b.text for b in msg.content if isinstance(b, TextBlock)).strip()
                    if t:
                        final_text = t   # 마지막 비어있지 않은 발화만 유지
        if captured_sid:
            self._save_session_id(captured_sid)
        return _strip_decoration(final_text)
