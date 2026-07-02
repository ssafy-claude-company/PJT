"""config 모듈 검증."""
import importlib
import tempfile
from pathlib import Path

import pytest


def _load(monkeypatch, **env):
    """주어진 환경변수만 세팅한 뒤 config 모듈을 새로 로드한다(실제 repo의 .env로부터 격리).
    ROOT를 .env 없는 임시 디렉토리로 돌린다 — 운영 시크릿(.env)이 실존하면 load_dotenv가 값을
    채워 '필수 누락' 케이스가 무효화되는 비헤르메틱을 차단(작업공간·로그 mkdir도 임시 쪽으로)."""
    for key in ("SYSTEM_BOT", "CHANNEL_ID", "ORGANT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import system.config as config
    importlib.reload(config)
    monkeypatch.setattr(config, "ROOT", Path(tempfile.mkdtemp(prefix="organt-config-test-")))
    return config


def test_정상_로딩(monkeypatch):
    config = _load(monkeypatch, SYSTEM_BOT="sys-token",
                   CHANNEL_ID="123", ORGANT_MODEL="opus")
    cfg = config.load_config()
    assert cfg.system_bot_token == "sys-token"
    assert cfg.channel_id == 123
    assert cfg.model == "opus"


def test_모델_미설정시_None(monkeypatch):
    config = _load(monkeypatch, SYSTEM_BOT="s", CHANNEL_ID="1")
    assert config.load_config().model is None


def test_채널ID_정수변환(monkeypatch):
    config = _load(monkeypatch, SYSTEM_BOT="s", CHANNEL_ID="987654321")
    cfg = config.load_config()
    assert isinstance(cfg.channel_id, int)
    assert cfg.channel_id == 987654321


def test_필수_누락시_에러(monkeypatch):
    config = _load(monkeypatch, CHANNEL_ID="1")  # SYSTEM_BOT 누락
    with pytest.raises(RuntimeError):
        config.load_config()


def test_작업공간은_repo_밖_격리(monkeypatch):
    monkeypatch.delenv("ORGANT_WORKSPACE", raising=False)
    config = _load(monkeypatch, SYSTEM_BOT="s", CHANNEL_ID="1")
    cfg = config.load_config()
    # repo 루트가 작업공간의 상위 경로에 없어야 한다(= repo 밖).
    assert config.ROOT not in cfg.workspace_dir.parents


def test_작업공간_env_override(monkeypatch, tmp_path):
    config = _load(monkeypatch, SYSTEM_BOT="s", CHANNEL_ID="1",
                   ORGANT_WORKSPACE=str(tmp_path / "myws"))
    assert config.load_config().workspace_dir == tmp_path / "myws"
