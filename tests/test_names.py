"""이름(닉네임) 안정 배정: 서버에 이미 있는 닉네임은 유지, 없는 봇만 새 이름 — 재시작·리클레임·
로스터 변동에도 같은 봇은 같은 이름(연결 순서 인덱스 배정의 '재시작마다 개명' 제거)."""
from src.main import KOREAN_NAMES, assign_stable_names


def test_기존닉네임은_유지_새봇만_새이름():
    existing = {101: "장도현"}                       # 서버에 이미 있는 닉(영속 진실원)
    out = assign_stable_names([101, 102], existing)
    assert out[101] == "장도현"                      # 연결 순서와 무관하게 유지(개명 없음)
    assert out[102] != "장도현" and out[102] in KOREAN_NAMES


def test_연결순서가_바뀌어도_기존이름_불변():
    existing = {101: "김민준", 102: "이서연"}
    a = assign_stable_names([101, 102], existing)
    b = assign_stable_names([102, 101], existing)    # 재시작에서 연결 순서 역전
    assert a == b == {101: "김민준", 102: "이서연"}


def test_중복없이_채움_풀소진시_번호확장():
    existing = {1: KOREAN_NAMES[0]}
    ids = list(range(1, 25))                         # 이름 풀(20) 초과
    out = assign_stable_names(ids, existing)
    assert len(set(out.values())) == len(ids)        # 전원 서로 다른 이름(충돌 없음)
    assert out[1] == KOREAN_NAMES[0]
