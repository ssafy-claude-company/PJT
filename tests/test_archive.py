"""기능16 검증: Context/Archive 파일 Guide."""
from src.archive import TaskStore
from src.task import TaskBoard


def test_context_저장_로드(tmp_path):
    s = TaskStore(tmp_path)
    assert s.load_context("T1") is None
    s.save_context("T1", "맥락 메모")
    assert s.load_context("T1") == "맥락 메모"


def test_context_누적(tmp_path):
    s = TaskStore(tmp_path)
    s.append_context("T1", "1줄")
    s.append_context("T1", "2줄")
    assert s.load_context("T1") == "1줄\n2줄\n"


def test_archive_저장_로드_목록(tmp_path):
    s = TaskStore(tmp_path)
    assert s.list_archived() == []
    s.archive("T1", "T1 완료 기록")
    s.archive("T2", "T2 완료 기록")
    assert s.load_archive("T1") == "T1 완료 기록"
    assert s.list_archived() == ["T1", "T2"]


def test_taskboard_렌더를_archive(tmp_path):
    b = TaskBoard("T9", "보고서")
    b.set_leader("A")
    b.set_goal("완성")
    s = TaskStore(tmp_path)
    s.archive(b.task_id, b.render())
    loaded = s.load_archive("T9")
    assert "Task #T9" in loaded and "목표확정" in loaded


def test_없는_archive는_None(tmp_path):
    assert TaskStore(tmp_path).load_archive("nope") is None
