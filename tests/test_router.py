"""기능3 검증: 메시지 수집·라우팅 판정 로직 (가짜 메시지로 단위 테스트)."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

from src.config import Config
from src.gateway import Gateway
from src.router import Router, RoutingDecision

CHANNEL = 1509794645327216640   # 대상 채널
ORGANT = 1510828738181595156    # Organt 봇 사용자 ID
OTHER = 222222222222222222      # 다른 사용자/봇 ID


def _msg(channel_id=CHANNEL, author_bot=False, author_id=999, mention_ids=(), content="안녕"):
    """discord.Message 형태의 가짜 메시지."""
    return SimpleNamespace(
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(bot=author_bot, id=author_id),
        mentions=[SimpleNamespace(id=i) for i in mention_ids],
        content=content,
    )


def _router():
    return Router(CHANNEL, ORGANT)


# --- Router 판정 로직 ---

def test_대상채널_사람_멘션없음_수집만():
    assert _router().decide(_msg()) == RoutingDecision(collect=True, route_to_organt=False)


def test_대상채널_사람_organt멘션_수집_및_라우팅():
    d = _router().decide(_msg(mention_ids=[ORGANT]))
    assert d == RoutingDecision(collect=True, route_to_organt=True)


def test_대상채널_사람_타인멘션_수집만():
    d = _router().decide(_msg(mention_ids=[OTHER]))
    assert d == RoutingDecision(collect=True, route_to_organt=False)


def test_봇_메시지는_무시():
    # Organt를 멘션했더라도 봇이 보낸 메시지면 수집/라우팅하지 않는다.
    d = _router().decide(_msg(author_bot=True, mention_ids=[ORGANT]))
    assert d == RoutingDecision(collect=False, route_to_organt=False)


def test_다른채널_메시지는_무시():
    d = _router().decide(_msg(channel_id=111, mention_ids=[ORGANT]))
    assert d == RoutingDecision(collect=False, route_to_organt=False)


def test_organt_미연결시_라우팅안함_수집만():
    # Organt 봇이 아직 연결 전(user_id=None)이면 멘션돼도 라우팅하지 않고 수집만.
    d = Router(CHANNEL, None).decide(_msg(mention_ids=[ORGANT]))
    assert d == RoutingDecision(collect=True, route_to_organt=False)


# --- Gateway on_message 배선 ---

def _fake_config() -> Config:
    return Config(
        system_bot_token="sys-tok",
        organt_bot_token="org-tok",
        channel_id=CHANNEL,
        model=None,
        workspace_dir=Path("/tmp"),
        audit_log_path=Path("/tmp/audit.jsonl"),
    )


def test_on_message가_사람메시지를_수집콜백으로_넘긴다():
    collected, routed = [], []
    gw = Gateway(_fake_config(), on_collect=collected.append, on_route=routed.append)
    msg = _msg()  # 대상 채널의 사람 메시지, 멘션 없음
    asyncio.run(gw.system_bot.on_message(msg))
    assert collected == [msg]
    assert routed == []


def test_on_message가_봇메시지는_무시한다():
    collected, routed = [], []
    gw = Gateway(_fake_config(), on_collect=collected.append, on_route=routed.append)
    asyncio.run(gw.system_bot.on_message(_msg(author_bot=True)))
    assert collected == [] and routed == []
