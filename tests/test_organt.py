"""기능4 검증: Organt 본체 옵션 구성 (네트워크/LLM 없이 구조만 확인).

실제 LLM 파일 생성은 라이브 데모(scripts/demo)로 실측한다.
"""
from pathlib import Path

from src.config import Config
from src.organt import Organt, _is_transient_api_error, _strip_decoration, build_options


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


def test_일시적_API오류_판별():
    assert _is_transient_api_error("API Error: 529 Overloaded. ...") is True
    assert _is_transient_api_error("API Error: 429 rate_limit") is True
    assert _is_transient_api_error("API Error: Stream closed") is True          # 제어 스트림 닫힘 = 일시(재시도)
    assert _is_transient_api_error("API Error: process exited") is True
    assert _is_transient_api_error("API Error: 400 invalid request") is False   # 영구 오류는 재시도 안 함
    assert _is_transient_api_error("백엔드 완성했습니다") is False               # 정상 응답


def test_빈응답_무응답은_재시도(monkeypatch):
    """서브프로세스가 발화 없이 조용히 죽어 빈 응답('')이 오면 handle이 resume 재시도 → 다음 시도에 응답이
    오면 그걸 반환한다. (동료가 '무응답'으로 보여 리더가 충원·재처리로 churn하던 silent-failure 경로 차단.)"""
    import asyncio
    o = Organt(_cfg())
    calls = {"n": 0}

    async def fake_run_once(prompt):
        calls["n"] += 1
        return ("", None) if calls["n"] == 1 else ("서버 구현 완료", None)  # 1차 빈 응답 → 2차 성공

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(o, "_run_once", fake_run_once)
    monkeypatch.setattr("src.organt.asyncio.sleep", _no_sleep)   # 백오프 대기 제거(빠른 테스트)
    out = asyncio.run(o.handle("서버 만들어줘"))
    assert out == "서버 구현 완료" and calls["n"] == 2           # 빈 응답 후 재시도해 성공


def test_보고_장식수평선_제거():
    # '---' 같은 장식 수평선만 제거하고 내용은 보존
    out = _strip_decoration("백엔드 완료\n---\n프론트 연동됨")
    assert out == "백엔드 완료\n프론트 연동됨"
    assert _strip_decoration("결과만\n***\n___") == "결과만"


def test_메시지수신마다_하트비트_on_activity(monkeypatch):
    """_run_once가 메시지를 받을 때마다 on_activity를 호출한다 — 도구 호출이 없는 긴 모델 생성
    (거대 파일 단일 Write 직전의 장문 작성)이 침묵 워치독에 '행'으로 오인되지 않게, 도구 훅
    (Pre/Post) 사이 사각을 메시지 단위 하트비트로 메운다."""
    import asyncio
    beats = {"n": 0}
    o = Organt(_cfg(), on_activity=lambda: beats.__setitem__("n", beats["n"] + 1))

    class _FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def query(self, prompt):
            pass

        async def receive_response(self):
            for _ in range(3):
                yield object()   # 메시지 3건 — 타입 무관, 수신 자체가 활동 신호

    monkeypatch.setattr("src.organt.ClaudeSDKClient", _FakeClient)
    asyncio.run(o._run_once("p"))
    assert beats["n"] == 3


def test_세션_cwd고정_pinned_cwd(tmp_path):
    """[세션-cwd 고정] CLI 세션 저장소는 cwd 기준 — 상태 파일에 '세션이 시작된 cwd'를 영속하고,
    다음 빌드는 그 cwd로 resume한다(흐름 도중 작업공간 카빙에도 세션 불멸). 디렉터리가 사라졌으면
    None(새 출발)."""
    import json as _json
    from src.organt import pinned_cwd
    st = tmp_path / "organt_state_x.json"
    st.write_text(_json.dumps({"session_id": "s1", "cwd": str(tmp_path)}), encoding="utf-8")
    assert pinned_cwd(st) == str(tmp_path)                      # 살아있는 cwd → 고정
    st.write_text(_json.dumps({"session_id": "s1", "cwd": str(tmp_path / "없는폴더")}), encoding="utf-8")
    assert pinned_cwd(st) is None                               # 사라진 cwd → 고정 해제
    st.write_text(_json.dumps({"cwd": str(tmp_path)}), encoding="utf-8")
    assert pinned_cwd(st) is None                               # 세션 없으면 고정 무의미
    assert pinned_cwd(tmp_path / "없음.json") is None


def test_세션저장시_cwd_함께영속(tmp_path):
    import asyncio
    import json as _json
    o = Organt(_cfg(), state_path=str(tmp_path / "st.json"))
    o._save_session_id("sid-1")
    d = _json.loads((tmp_path / "st.json").read_text(encoding="utf-8"))
    assert d["session_id"] == "sid-1" and d["cwd"] == "/tmp/ws"   # build_options(_cfg()).cwd


def test_스테일세션은_재시도아닌_새세션_자가치유(monkeypatch, tmp_path):
    """[자가 치유] 'No conversation found'(resume 대상 부재)는 일시 오류가 아니라 영구 실패 —
    같은 세션 재시도 12회 헛돌이(라이브 관측) 대신, 세션을 버리고 즉시 새 세션으로 전진한다."""
    import asyncio
    import json as _json
    st = tmp_path / "st.json"
    st.write_text(_json.dumps({"session_id": "dead-sid", "cwd": str(tmp_path)}), encoding="utf-8")
    o = Organt(_cfg(), state_path=str(st))
    assert o.session_id == "dead-sid"
    calls = {"n": 0}

    async def fake_run_once(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("API Error: No conversation found with session ID: dead-sid", None)
        return ("기획 이어서 완료", "sid-new")

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(o, "_run_once", fake_run_once)
    monkeypatch.setattr("src.organt.asyncio.sleep", _no_sleep)
    out = asyncio.run(o.handle("이어서 진행"))
    assert out == "기획 이어서 완료" and calls["n"] == 2          # 1회 실패 → 즉시 새 세션 성공
    assert o.session_id == "sid-new"                              # 죽은 세션 폐기·새 세션 영속
    assert _json.loads(st.read_text(encoding="utf-8"))["session_id"] == "sid-new"
