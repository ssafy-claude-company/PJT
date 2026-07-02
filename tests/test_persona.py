"""기능7 검증: 인격(CLAUDE.md) 로딩."""
from organt_core.organt import ORGANT_PERSONA, PERSONA_PATH, load_persona


def test_load_persona_파일읽기(tmp_path):
    p = tmp_path / "CLAUDE.md"
    p.write_text("# 테스트 인격\nOrgant 입니다.", encoding="utf-8")
    assert "테스트 인격" in load_persona(p)


def test_load_persona_없으면_기본(tmp_path):
    assert load_persona(tmp_path / "none.md") == ORGANT_PERSONA


def test_load_persona_빈파일이면_기본(tmp_path):
    p = tmp_path / "empty.md"
    p.write_text("   \n", encoding="utf-8")
    assert load_persona(p) == ORGANT_PERSONA


def test_organt_CLAUDEmd_존재하고_로딩됨():
    # 실제 organt/CLAUDE.md 가 존재하고 인격으로 로딩되는지
    assert PERSONA_PATH.exists()
    persona = load_persona()
    assert "Organt" in persona and "작업공간" in persona
