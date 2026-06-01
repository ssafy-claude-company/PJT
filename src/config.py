"""환경변수에서 런타임 설정을 읽어들인다."""
import os
from dataclasses import dataclass
from pathlib import Path

# 프로젝트 루트 (이 파일 기준 상위 디렉토리)
ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    """런타임 전역 설정."""

    system_bot_token: str   # System 봇 토큰 (관리자: 수집·라우팅)
    organt_bot_token: str   # Organt 봇 토큰 (발화 주체)
    channel_id: int         # 수집·반응 대상 채널 ID
    model: str | None       # Organt(LLM) 모델. None이면 SDK 기본 사용
    workspace_dir: Path     # Organt 작업공간(cwd)
    audit_log_path: Path    # audit 로그 파일 경로


def _require(name: str) -> str:
    """필수 환경변수를 읽고, 비어 있으면 명확한 에러를 낸다."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"필수 환경변수 {name} 가 비어 있습니다. .env.example 를 참고하세요.")
    return value


def load_config() -> Config:
    """환경변수에서 설정을 로드한다. python-dotenv가 있으면 .env도 보조 로딩한다."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass  # 환경변수가 직접 주입된 경우 .env가 없어도 된다

    workspace_dir = ROOT / "workspace"
    audit_log_path = ROOT / "logs" / "audit.jsonl"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    return Config(
        system_bot_token=_require("SYSTEM_BOT"),
        organt_bot_token=_require("TEST_BOT"),
        channel_id=int(_require("CHANNEL_ID")),
        model=os.environ.get("ORGANT_MODEL", "").strip() or None,
        workspace_dir=workspace_dir,
        audit_log_path=audit_log_path,
    )


if __name__ == "__main__":
    # 설정 헬스체크 (토큰 값은 출력하지 않는다)
    cfg = load_config()
    print("[설정 확인] (토큰 값은 출력하지 않음)")
    print(f"  채널 ID  : {cfg.channel_id}")
    print(f"  모델     : {cfg.model or '(SDK 기본)'}")
    print(f"  작업공간 : {cfg.workspace_dir}")
    print(f"  로그     : {cfg.audit_log_path}")
    print(f"  봇 토큰  : {'2개 설정됨' if cfg.system_bot_token and cfg.organt_bot_token else '누락'}")
