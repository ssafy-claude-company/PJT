"""로스터 로딩: 같은 봇 토큰이 두 슬롯(예: ORGANT_BOT_3 ↔ TEST_OBT_2 폴백)에 들어와도 한 번만 —
이중 연결(같은 봇 유령 세션)과 뒤 슬롯 라벨이 앞 슬롯을 덮어쓰는 문제 방지(첫 슬롯=우선)."""
from src.main import load_roster


def test_같은토큰_두슬롯_dedupe_첫슬롯우선(monkeypatch):
    monkeypatch.setenv("ORGANT_ROSTER", "A:백엔드;B:프론트엔드;C:디자이너")
    monkeypatch.setenv("A", "tok-1")
    monkeypatch.setenv("B", "tok-2")
    monkeypatch.setenv("C", "tok-1")          # A와 같은 봇(같은 토큰)이 다른 라벨로 또 들어옴
    r = load_roster()
    assert r == [("tok-1", "백엔드"), ("tok-2", "프론트엔드")]   # 첫 슬롯 라벨 유지, 중복 제외


def test_빈슬롯은_자동제외(monkeypatch):
    monkeypatch.setenv("ORGANT_ROSTER", "A:백엔드;X:예비;B:QA")
    monkeypatch.setenv("A", "tok-1")
    monkeypatch.setenv("B", "tok-2")
    monkeypatch.delenv("X", raising=False)    # 토큰 없는 슬롯
    assert load_roster() == [("tok-1", "백엔드"), ("tok-2", "QA")]
