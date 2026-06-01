"""Task 통합: System 봇으로 Task=Thread를 만들고 상태판을 갱신한다.

- Task 생성 시 채널에 Thread를 만들고 상태판 메시지를 올린다.
- 흐름이 진행될 때마다 상태판 메시지를 edit로 갱신한다(System 봇).
- 완료/보고 시 Context/Archive(TaskStore)에 기록한다.
"""
import discord

from .archive import TaskStore
from .task import TaskBoard


class TaskGateway:
    """System 봇 클라이언트로 Task Thread + 상태판을 운영한다."""

    def __init__(self, system_client, channel_id, store: TaskStore = None):
        self.client = system_client
        self.channel_id = channel_id
        self.store = store
        self.thread = None
        self._board_msg = None

    async def _channel(self):
        ch = self.client.get_channel(self.channel_id)
        if ch is None:
            ch = await self.client.fetch_channel(self.channel_id)
        return ch

    async def create_task(self, board: TaskBoard):
        """Task용 Thread 생성 + 상태판 최초 게시."""
        channel = await self._channel()
        self.thread = await channel.create_thread(
            name=f"Task: {board.title}", type=discord.ChannelType.public_thread)
        self._board_msg = await self.thread.send(board.render())
        if self.store:
            self.store.append_context(board.task_id, f"created: {board.title}")
        return self.thread

    async def update(self, board: TaskBoard):
        """상태판 메시지를 현재 상태로 갱신(edit)한다."""
        if self._board_msg is not None:
            self._board_msg = await self._board_msg.edit(content=board.render())
        if self.store:
            self.store.append_context(board.task_id, f"status={board.status.value}")

    async def finish(self, board: TaskBoard):
        """마지막 갱신 + 완료 기록(Archive)."""
        await self.update(board)
        if self.store:
            self.store.archive(board.task_id, board.render())
