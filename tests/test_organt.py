"""기능4 검증: Organt 본체 옵션 구성 (네트워크/LLM 없이 구조만 확인).

실제 LLM 파일 생성은 라이브 데모(scripts/demo)로 실측한다.
"""
from pathlib import Path

from src.config import Config
from src.organt import Organt, build_options


def _cfg(model=None) -> Config:
    return Config(
        system_bot_token="s", channel_id=1,
        model=model, workspace_dir=Path("/tmp/ws"),
        audit_log_path=Path("/tmp/audit.jsonl"),
    )


def test_옵션_작업공간이_cwd():
    assert build_options(_cfg()).cwd == "/tmp/ws"


def test_옵션_파일툴_허용():
    allowed = set(build_options(_cfg()).allowed_tools)
    assert {"Read", "Write", "Edit"}.issubset(allowed)


def test_옵션_권한모드_파일쓰기가능():
    assert build_options(_cfg()).permission_mode == "acceptEdits"


def test_옵션_모델_config반영():
    assert build_options(_cfg(model="opus")).model == "opus"
    assert build_options(_cfg(model=None)).model is None


def test_옵션_override_주입():
    # 기능5·6에서 mcp_servers/hooks/allowed_tools를 주입하는 경로.
    opts = build_options(_cfg(), allowed_tools=["Read"], max_turns=3)
    assert opts.allowed_tools == ["Read"]
    assert opts.max_turns == 3


def test_organt_기본옵션_인격_CLAUDEmd():
    sp = Organt(_cfg()).options.system_prompt
    assert isinstance(sp, str) and "Organt" in sp
