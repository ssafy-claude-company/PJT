"""재구현 ② 검증: DiscordGuide (상태블록=채널, 대화=스레드, 보낸봇=Organt)."""
import asyncio

from src.discord_guide import DiscordGuide, _split_for_discord
from src.protocol import Kind, TaskStatus, format_request, format_task_status


def test_긴메시지_2000자_분할():
    # 2000자 초과는 한도 이하 조각들로 분할되어 '조용한 유실' 방지
    parts = _split_for_discord("줄\n" * 1500)          # 약 4500자
    assert len(parts) >= 2 and all(len(p) <= 1900 for p in parts)
    assert _split_for_discord("짧은 글") == ["짧은 글"]   # 짧으면 그대로 1개
    assert _split_for_discord("x" * 4000)               # 줄바꿈 없는 긴 줄도 강제 분할
    assert all(len(p) <= 1900 for p in _split_for_discord("x" * 4000))


class Msg:
    _n = 0

    def __init__(self, content, channel):
        Msg._n += 1
        self.id = 7000 + Msg._n
        self.content = content
        self.channel = channel
        self.thread = None

    async def create_thread(self, name):
        self.thread = Channel(9001, name)
        return self.thread

    async def edit(self, content):
        self.content = content
        return self

    async def reply(self, content):
        return await self.channel.send(content)


class Channel:
    def __init__(self, cid, name=""):
        self.id = cid
        self.name = name
        self.sent = []
        self._by = {}

    async def send(self, content):
        m = Msg(content, self)
        self.sent.append(m)
        self._by[m.id] = m
        return m

    async def fetch_message(self, mid):
        return self._by[int(mid)]


class Client:
    def __init__(self):
        self.channels = {}

    def get_channel(self, cid):
        return self.channels.setdefault(cid, Channel(cid))


def test_open_task_상태블록은_채널에_스레드_파생():
    sysc = Client()
    g = DiscordGuide(sysc, {})
    ts = TaskStatus(task_id="001", purpose="ToDo앱", status="생성")
    block_id, thread_id = asyncio.run(g.open_task(10, ts))
    ch = sysc.channels[10]
    assert ch.sent[0].content == format_task_status(ts)   # 상태블록이 채널에 게시
    assert ch.sent[0].thread is not None                  # 그 블록에서 스레드 파생
    assert thread_id == str(ch.sent[0].thread.id)


def test_update_status_채널블록_edit():
    sysc = Client()
    g = DiscordGuide(sysc, {})
    ts = TaskStatus(task_id="1", status="생성")
    block_id, _ = asyncio.run(g.open_task(10, ts))
    ts.status = "진행"
    asyncio.run(g.update_status(10, block_id, ts))
    assert "Status: 진행" in sysc.channels[10]._by[int(block_id)].content


def test_send_request_보낸봇으로_스레드에_구조화():
    sysc, org = Client(), Client()
    g = DiscordGuide(sysc, {111: org})
    asyncio.run(g.send_request(thread_id=500, sender_id=111, to_id=222, kind=Kind.WORK, body="해줘"))
    assert org.channels[500].sent[0].content == format_request(222, Kind.WORK, "해줘")


def test_send_response_보낸봇으로_reply():
    sysc, org = Client(), Client()
    g = DiscordGuide(sysc, {111: org})
    # 스레드에 먼저 요청이 있다고 가정(메시지 id 확보)
    thread = org.get_channel(500)
    req = asyncio.run(thread.send("[Request]..."))
    asyncio.run(g.send_response(thread_id=500, sender_id=111, request_msg_id=req.id, body="완료"))
    assert any(m.content == "[Response]\nBody: 완료" for m in thread.sent)
