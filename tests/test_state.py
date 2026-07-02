"""기능9 검증: State(세션 resume) 보존."""
from pathlib import Path

from organt_core.config import Config
from organt.organt import Organt


def _cfg(tmp) -> Config:
    return Config(
        system_bot_token="s", channel_id=1, model=None,
        workspace_dir=Path(tmp) / "ws", audit_log_path=Path(tmp) / "logs" / "audit.jsonl",
    )


def test_세션ID_저장후_새인스턴스가_복원(tmp_path):
    sp = tmp_path / "state.json"
    o1 = Organt(_cfg(tmp_path), state_path=sp)
    assert o1.session_id is None
    o1._save_session_id("sess-abc")
    o2 = Organt(_cfg(tmp_path), state_path=sp)  # 재시작 시뮬
    assert o2.session_id == "sess-abc"


def test_options_for_call이_resume_적용(tmp_path):
    o = Organt(_cfg(tmp_path), state_path=tmp_path / "s.json")
    assert getattr(o._options_for_call(), "resume", None) is None
    o.session_id = "xyz"
    assert o._options_for_call().resume == "xyz"


def test_기본_state_경로는_logs_옆(tmp_path):
    cfg = _cfg(tmp_path)
    o = Organt(cfg)
    assert o.state_path == cfg.audit_log_path.parent / "organt_state.json"


def test_깨진_state파일이면_None(tmp_path):
    sp = tmp_path / "broken.json"
    sp.write_text("{not json", encoding="utf-8")
    assert Organt(_cfg(tmp_path), state_path=sp).session_id is None
