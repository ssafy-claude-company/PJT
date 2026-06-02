"""재구현 ④ 검증: Task Rule (docs Task.md 8단계 flow + 미달 loop)."""
import pytest

from src.task_rule import Phase, Task, TaskError

LEADER, B, C = 1, 2, 3


def _full_to_check():
    t = Task("001", purpose="ToDo앱 제작", leader=LEADER)
    t.recruit(B)
    t.recruit(C)
    t.set_goal("CRUD 4기능 동작")
    t1 = t.add_todo("백엔드")
    t2 = t.add_todo("프론트")
    t.distribute(t1.todo_id, B)
    t.distribute(t2.todo_id, C)
    t.complete(t1.todo_id)
    t.complete(t2.todo_id)
    return t, t1, t2


def test_전체_flow_단계_진행():
    t, _, _ = _full_to_check()
    assert t.phase == Phase.CHECK
    t.judge_goal(True)
    t.report("CRUD 동작 확인, 완수")
    assert t.phase == Phase.REPORTED and t.result.startswith("CRUD")
    assert t.history[0] == Phase.CREATED and t.history[-1] == Phase.REPORTED
    assert set(t.team) == {LEADER, B, C}


def test_Goal_미달시_Todo로_loop():
    t, _, _ = _full_to_check()
    t.judge_goal(False)             # 미달 → ④로
    assert t.phase == Phase.GOAL_SET and t.rounds == 1
    t3 = t.add_todo("버그 수정")
    t.distribute(t3.todo_id, B)
    t.complete(t3.todo_id)          # 기존 done + 신규 done → 다시 완료확인
    assert t.phase == Phase.CHECK
    t.judge_goal(True)
    t.report("수정 후 완수")
    assert t.phase == Phase.REPORTED and t.rounds == 2


def test_가드_goal전_todo불가():
    t = Task("1", "p", LEADER)
    with pytest.raises(TaskError):
        t.add_todo("x")


def test_가드_판정전_보고불가():
    t, _, _ = _full_to_check()
    with pytest.raises(TaskError):
        t.report("성급")            # judge_goal 전


def test_status_블록_docs필드():
    t, _, _ = _full_to_check()
    st = t.status(bot_info={LEADER: "leader", B: "dev", C: "dev"})
    assert st.task_id == "001" and st.purpose == "ToDo앱 제작"
    assert st.status == "완료확인" and st.goal == "CRUD 4기능 동작"
    assert (f"<@{LEADER}>", "leader") in st.group
