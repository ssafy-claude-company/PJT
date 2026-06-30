"""deploy 헬퍼 검증 — PAT 마스킹·orphan keep-set 보수 폴백 (오프라인, 네트워크 없음). 보안·정확성 핫픽스."""
from src.deploy import _mask_secret, _referenced_services


def test_mask_secret은_PAT를_에러문자열에서_가린다():
    pat = "ghp_SECRETTOKEN1234567890"
    err = f"fatal: unable to push https://x-access-token:{pat}@github.com/u/r.git"
    masked = _mask_secret(err, pat)
    assert pat not in masked and "***" in masked
    assert _mask_secret("hello", "") == "hello"        # 빈/짧은 비밀은 무시(오탐 방지)
    assert _mask_secret("hello", "ab") == "hello"


def test_referenced_services_보수폴백(tmp_path):
    # 파일 없음 → set()(프로젝트 0 = 정당한 빈 keep)
    assert _referenced_services(str(tmp_path / "none.json")) == set()
    # 읽기 실패(디렉터리를 가리킴 → read_text 예외) → None(판단 불가, 호출부가 고아 수거 중단)
    d = tmp_path / "adir"; d.mkdir()
    assert _referenced_services(str(d)) is None
    # 정상 파일 → set(타입), 참조 onrender 추출
    good = tmp_path / "projects.json"
    good.write_text('{"projects": {"1": {"deployed": "라이브: https://organt-p-001.onrender.com"}}}')
    refs = _referenced_services(str(good))
    assert refs is not None and isinstance(refs, set)
