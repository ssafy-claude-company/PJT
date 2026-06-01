"""기능15 검증: Task 상태판 상태기계 + 단계별 갱신."""
import pytest

from src.task import Status, TaskBoard, TaskError


def test_전체_flow_상태판_단계별_갱신():
    b = TaskBoard("T1", "보고서 작성")
    assert b.status == Status.CREATED
    b.set_leader("A")
    assert b.status == Status.LEADER and b.leader == "A"
    b.set_goal("Q2 보고서 완성")
    assert b.status == Status.GOAL and "Q2" in b.goal
    t1 = b.add_todo("자료 수집")
    t2 = b.add_todo("초안 작성")
    b.assign(t1.todo_id, "B")
    assert b.status == Status.ASSIGNED      # 분배 시작
    b.assign(t2.todo_id, "C")
    b.complete(t1.todo_id)
    assert b.status == Status.ASSIGNED      # 아직 미완 남음
    b.complete(t2.todo_id)
    assert b.status == Status.DONE          # 모두 완료
    b.report("완료했습니다")
    assert b.status == Status.REPORTED and b.report_text == "완료했습니다"
    # 상태판 history가 단계마다 갱신됨
    assert b.history == [Status.CREATED, Status.LEADER, Status.GOAL,
                         Status.ASSIGNED, Status.DONE, Status.REPORTED]


def test_렌더_상태판_내용():
    b = TaskBoard("T2", "테스트")
    b.set_leader("A")
    b.set_goal("목표X")
    t = b.add_todo("할일1")
    b.assign(t.todo_id, "B")
    out = b.render()
    assert "Task #T2" in out and "분배" in out and "목표X" in out
    assert "[ ] 할일1 @B" in out
    b.complete(t.todo_id)
    assert "[x] 할일1 @B" in b.render()


def test_가드_리더없이_목표불가():
    b = TaskBoard("T3", "x")
    with pytest.raises(TaskError):
        b.set_goal("목표")


def test_가드_목표없이_Todo불가():
    b = TaskBoard("T4", "x")
    b.set_leader("A")
    with pytest.raises(TaskError):
        # 아직 GOAL 아님(LEADER) → Todo 불가
        b.add_todo("할일")


def test_가드_완료전_보고불가():
    b = TaskBoard("T5", "x")
    b.set_leader("A")
    b.set_goal("g")
    t = b.add_todo("할일")
    b.assign(t.todo_id, "B")
    with pytest.raises(TaskError):
        b.report("끝")  # 아직 완료 아님


def test_없는_Todo_완료_에러():
    b = TaskBoard("T6", "x")
    b.set_leader("A")
    b.set_goal("g")
    with pytest.raises(TaskError):
        b.complete(999)
