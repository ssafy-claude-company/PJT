"""재구현 ⑤ 검증: SYS 통합 흐름 (공용 fake Discord + 스크립트 policy)."""
import asyncio
import re
from types import SimpleNamespace

from src.discord_guide import DiscordGuide
from src.protocol import Request, Response
from src.sys_core import ORIGIN, Sys
from src.task_rule import Phase


# --- 공용 fake Discord (여러 봇이 같은 채널/스레드를 본다) ---

class Store:
    def __init__(self):
        self.ch = {}
        self._mid = 1000
        self._cid = 700

    def raw(self, cid):
        return self.ch.setdefault(cid, RawCh(cid, self))

    def new_thread(self, name):
        self._cid += 1
        t = RawCh(self._cid, self)
        t.name = name
        self.ch[self._cid] = t
        return t

    def mid(self):
        self._mid += 1
        return self._mid


class RawCh:
    def __init__(self, cid, store):
        self.id = cid
        self.store = store
        self.msgs = []
        self.name = ""


class RawMsg:
    def __init__(self, mid, content, author_id, ch, ref=None):
        self.id = mid
        self.content = content
        self.author = SimpleNamespace(id=author_id)
        self.ch = ch
        self.reference = SimpleNamespace(message_id=ref) if ref else None
        self.mentions = [SimpleNamespace(id=int(x)) for x in re.findall(r"<@(\d+)>", content)]


class ChView:
    def __init__(self, raw, uid):
        self.raw = raw
        self.uid = uid
        self.id = raw.id
        self.name = raw.name

    async def send(self, content):
        m = RawMsg(self.raw.store.mid(), content, self.uid, self.raw)
        self.raw.msgs.append(m)
        return MsgView(m, self.uid)

    async def fetch_message(self, mid):
        for m in self.raw.msgs:
            if m.id == int(mid):
                return MsgView(m, self.uid)
        raise KeyError(mid)

    async def history(self, limit=50):
        for m in reversed(self.raw.msgs[-limit:]):
            yield m


class MsgView:
    def __init__(self, raw, uid):
        self.raw = raw
        self.uid = uid
        self.id = raw.id

    async def create_thread(self, name):
        return ChView(self.raw.ch.store.new_thread(name), self.uid)

    async def edit(self, content):
        self.raw.content = content
        return self

    async def reply(self, content):
        m = RawMsg(self.raw.ch.store.mid(), content, self.uid, self.raw.ch, ref=self.raw.id)
        self.raw.ch.msgs.append(m)
        return MsgView(m, self.uid)


class FakeClient:
    def __init__(self, store, uid):
        self.store = store
        self.uid = uid

    def get_channel(self, cid):
        return ChView(self.store.raw(cid), self.uid)

    async def fetch_channel(self, cid):
        return ChView(self.store.raw(cid), self.uid)


class ScriptPolicy:
    def __init__(self, b, c):
        self.b, self.c = b, c

    async def plan(self, purpose, team):
        return ("CRUD 4기능 동작", [("백엔드 구현", self.b), ("프론트 구현", self.c)])

    async def do_work(self, todo):
        return f"{todo} 완료"

    async def review(self, goal, results):
        return (True, f"Goal 달성 — {len(results)}개 작업 완료")


def test_sys_통합_task_flow():
    store = Store()
    SYSID, L, B, C = 10, 11, 12, 13
    guide = DiscordGuide(FakeClient(store, SYSID),
                         {L: FakeClient(store, L), B: FakeClient(store, B), C: FakeClient(store, C)})
    sysc = Sys(guide, channel_id=500, bot_info={L: "leader", B: "dev", C: "dev"})

    out = asyncio.run(sysc.run_task("001", "ToDo앱 제작", leader=L, team=[L, B, C], policy=ScriptPolicy(B, C)))
    task, comm = out["task"], out["comm"]

    # 베턴: 흐름이 시작점으로 복귀하고 종료
    assert comm.done and comm.alive == ORIGIN
    # Task: 보고 단계 + 결과
    assert task.phase == Phase.REPORTED and task.result

    # 채널 상태블록(첫 메시지)이 최종 상태로 갱신됨
    block = store.raw(500).msgs[0]
    assert "Status: 보고" in block.content and "result:" in block.content
    assert "Purpose: ToDo앱 제작" in block.content

    # Thread 안에 구조화 Request/Response (root + todo 2 + 응답 3)
    parsed = asyncio.run(guide.read_thread(int(out["thread_id"])))
    reqs = [m for m in parsed if isinstance(m, Request)]
    resps = [m for m in parsed if isinstance(m, Response)]
    assert len(reqs) >= 3 and len(resps) >= 3


def test_sys_미달시_loop_후_종료():
    store = Store()
    L, B = 21, 22

    class Flaky:
        def __init__(self):
            self.n = 0

        async def plan(self, purpose, team):
            return ("동작", [("작업", B)])

        async def do_work(self, todo):
            return "했음"

        async def review(self, goal, results):
            self.n += 1
            return (self.n >= 2, f"라운드 {self.n}")   # 첫 판정 미달 → loop

    guide = DiscordGuide(FakeClient(store, 99), {L: FakeClient(store, L), B: FakeClient(store, B)})
    sysc = Sys(guide, 500)
    out = asyncio.run(sysc.run_task("002", "p", leader=L, team=[L, B], policy=Flaky(), max_rounds=3))
    assert out["comm"].done
    assert out["task"].rounds >= 2   # 미달 후 재판정
