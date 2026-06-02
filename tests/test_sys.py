"""재구현 ⑤ 검증(신모델): Organt 주도 Guide 도구 + 단일흐름 보존."""
import asyncio

from src.guide_tools import Flow, make_guide_tools
from src.sys_core import Sys


class FakeGuide:
    def __init__(self):
        self.calls = []
        self._n = 0

    async def post(self, ch, sender, content, reply_to=None):
        self.calls.append(("post", ch, sender, content))
        return "m1"

    async def create_project_channel(self, gid, name):
        self.calls.append(("create_channel", name))
        return 9001

    async def open_task(self, ch, status):
        self.calls.append(("open_task", ch, status.purpose))
        return "blk", "thr"

    async def update_status(self, ch, blk, status):
        self.calls.append(("update", status.status))
        return blk

    async def send_request(self, thr, sender, to, kind, body):
        self._n += 1
        self.calls.append(("req", sender, to, body))
        return f"req{self._n}"

    async def send_response(self, thr, sender, req, body):
        self.calls.append(("resp", sender, body))
        return "r"


class FakeOrgant:
    async def handle(self, work):
        return f"{work} 완료"


def _tools(flow):
    return {t.name: t for t in make_guide_tools(flow)}


def test_answer_question_그자리답변_흐름종료():
    g = FakeGuide()
    flow = Flow(g, channel_id=500, guild_id=1, leader_id=11, teammates={})
    flow.start_root("root")
    asyncio.run(_tools(flow)["answer_question"].handler({"body": "그건 X입니다"}))
    assert flow.done and flow.comm.done
    assert any(c[0] == "post" for c in g.calls)
    assert not any(c[0] == "create_channel" for c in g.calls)  # 질문은 채널 안 팜


def test_project_생성_위임_베턴복귀():
    g = FakeGuide()
    flow = Flow(g, 500, guild_id=1, leader_id=11, teammates={12: FakeOrgant()},
                bot_info={11: "leader", 12: "dev"})
    flow.start_root("root")
    tools = _tools(flow)
    asyncio.run(tools["create_project"].handler({"name": "todo-app"}))
    asyncio.run(tools["create_task"].handler({"purpose": "ToDo앱", "goal": "CRUD 동작"}))
    res = asyncio.run(tools["delegate"].handler({"member_id": 12, "work": "백엔드 구현"}))
    assert ("create_channel", "todo-app") in g.calls
    assert ("req", 11, 12, "백엔드 구현") in g.calls
    assert any(c[0] == "resp" and c[1] == 12 for c in g.calls)
    assert flow.comm.alive == 11          # 위임 후 베턴이 Leader로 복귀
    assert "완료" in res["content"][0]["text"]


def test_delegate_자기자신_차단():
    g = FakeGuide()
    flow = Flow(g, 500, 1, leader_id=11, teammates={12: FakeOrgant()})
    flow.start_root("root")
    tools = _tools(flow)
    asyncio.run(tools["create_task"].handler({"purpose": "p", "goal": "g"}))
    r = asyncio.run(tools["delegate"].handler({"member_id": 11, "work": "x"}))
    assert "오류" in r["content"][0]["text"]   # from==to 위임 불가


def test_report_흐름종료():
    g = FakeGuide()
    flow = Flow(g, 500, 1, 11, {12: FakeOrgant()}, bot_info={11: "L"})
    flow.start_root("root")
    tools = _tools(flow)
    asyncio.run(tools["create_task"].handler({"purpose": "p", "goal": "g"}))
    asyncio.run(tools["report"].handler({"body": "완수 보고"}))
    assert flow.done and flow.comm.done and flow.status.status == "보고"


def test_단일흐름_보존_개입은_advice():
    g = FakeGuide()
    s = Sys(g, guild_id=1)
    s.active_flow = Flow(g, 500, 1, 11, {})       # 활성 흐름 중
    out = asyncio.run(s.handle_user_input(500, 11, {}, "중간 개입!", leader_factory=None))
    assert out["mode"] == "advice" and "중간 개입!" in s.active_flow.advice
