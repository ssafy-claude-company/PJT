"""기능14 검증: 통신 오케스트레이터 (베턴 + Discord 전송 배선). 가짜 sender로 오프라인."""
import asyncio

from src.orchestrator import CommGateway

IDS = {"A": 1, "B": 2, "C": 3}


class Bus:
    def __init__(self):
        self.events = []
        self._id = 1000

    def record(self, name, kind, content, ref=None):
        self._id += 1
        self.events.append({"by": name, "kind": kind, "id": self._id,
                            "content": content, "ref": ref})
        return self._id


class FakeSender:
    def __init__(self, name, bus):
        self.name = name
        self.bus = bus

    async def send(self, content):
        return self.bus.record(self.name, "send", content)

    async def reply(self, message_id, content):
        return self.bus.record(self.name, "reply", content, ref=message_id)


def _gw():
    bus = Bus()
    senders = {n: FakeSender(n, bus) for n in IDS}
    return CommGateway(senders, IDS, origin="A"), bus


def test_A_B_C_역순close_왕복():
    gw, bus = _gw()
    assert gw.alive_name() == "A"
    rid1 = asyncio.run(gw.request("A", "B", "B야 일해"))   # 활성 B
    assert gw.alive_name() == "B"
    rid2 = asyncio.run(gw.request("B", "C", "C야 일해"))   # 활성 C
    assert gw.alive_name() == "C"
    asyncio.run(gw.respond("C", rid2, "C 완료"))            # 활성 B
    assert gw.alive_name() == "B" and not gw.done
    asyncio.run(gw.respond("B", rid1, "B 완료"))            # 활성 A, 종료
    assert gw.done and gw.alive_name() == "A"

    kinds = [e["kind"] for e in bus.events]
    assert kinds == ["send", "send", "reply", "reply"]      # 요청2 → 응답2(역순)
    assert bus.events[0]["content"].startswith("[REQ:")
    assert bus.events[2]["content"].startswith("[RESP:")


def test_request는_대상을_멘션():
    gw, bus = _gw()
    asyncio.run(gw.request("A", "B", "해줘"))
    assert "<@2>" in bus.events[0]["content"]               # B(id=2) 멘션


def test_response는_request에_reply():
    gw, bus = _gw()
    rid = asyncio.run(gw.request("A", "B", "해줘"))
    asyncio.run(gw.respond("B", rid, "완료"))
    reply_ev = bus.events[1]
    assert reply_ev["kind"] == "reply" and str(reply_ev["ref"]) == rid
