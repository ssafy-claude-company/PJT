"""deploy 헬퍼 검증 — PAT 마스킹·orphan keep-set 보수 폴백 (오프라인, 네트워크 없음). 보안·정확성 핫픽스."""
from system.deploy import _mask_secret, _referenced_services


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


def test_oversized_files_100MB초과만_감지(tmp_path):
    """>100MB 예방 게이트 — 스테이징된 파일 중 GitHub 100MB 한도 초과만 잡는다(sparse 파일로 가볍게)."""
    import subprocess, os
    from system.deploy import _oversized_files
    d = tmp_path / "repo"; d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    with open(d / "big.bin", "wb") as f:
        f.truncate(101 * 1024 * 1024)          # 101MB sparse(디스크 안 씀)
    (d / "app.js").write_text("ok")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    names = [r for r, s in _oversized_files(str(d))]
    assert "big.bin" in names                  # 초과 감지
    assert "app.js" not in names               # 작은 건 아님
    os.remove(d / "big.bin")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    assert _oversized_files(str(d)) == []      # 제거하면 통과
