"""기능17 검증: Task 통합(Thread 생성 + 상태판 갱신). 가짜 client로 오프라인."""
import asyncio

from src.archive import TaskStore
from src.task import TaskBoard
from src.task_gateway import TaskGateway


class FakeMsg:
    def __init__(self, content):
        self.content = content
        self.edits = []

    async def edit(self, content):
        self.content = content
        self.edits.append(content)
        return self


class FakeThread:
    def __init__(self, name):
        self.name = name
        self.sent = []

    async def send(self, content):
        m = FakeMsg(content)
        self.sent.append(m)
        return m


class FakeChannel:
    def __init__(self):
        self.threads = []

    async def create_thread(self, name, type=None):
        t = FakeThread(name)
        self.threads.append(t)
        return t


class FakeClient:
    def __init__(self, ch):
        self._ch = ch

    def get_channel(self, cid):
        return self._ch


def test_create_task_thread_및_상태판(tmp_path):
    ch = FakeChannel()
    tg = TaskGateway(FakeClient(ch), 5, TaskStore(tmp_path))
    board = TaskBoard("T1", "데모 보고서")
    asyncio.run(tg.create_task(board))
    assert ch.threads and "데모 보고서" in ch.threads[0].name
    assert "Task #T1" in ch.threads[0].sent[0].content


def test_update가_상태판_edit(tmp_path):
    ch = FakeChannel()
    tg = TaskGateway(FakeClient(ch), 5)
    board = TaskBoard("T1", "데모")
    asyncio.run(tg.create_task(board))
    board.set_leader("A")
    asyncio.run(tg.update(board))
    assert "리더확정" in tg._board_msg.content and tg._board_msg.edits


def test_finish가_archive_기록(tmp_path):
    ch = FakeChannel()
    store = TaskStore(tmp_path)
    tg = TaskGateway(FakeClient(ch), 5, store)
    board = TaskBoard("T9", "데모")
    asyncio.run(tg.create_task(board))
    board.set_leader("A")
    board.set_goal("완성")
    t = board.add_todo("할일")
    board.assign(t.todo_id, "B")
    board.complete(t.todo_id)
    board.report("끝")
    asyncio.run(tg.finish(board))
    archived = store.load_archive("T9")
    assert archived and "보고" in archived
    # context도 단계별로 누적됨
    assert "created" in store.load_context("T9")
