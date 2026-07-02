"""채널 해석 검증."""
import asyncio
from types import SimpleNamespace

from organt_discord.channels import choose_text_channel_id, resolve_channel_id

CHANS = [(100, "일반"), (200, "archive"), (300, "test")]


# --- 순수 선택 로직 ---

def test_prefer_이름():
    assert choose_text_channel_id(CHANS, prefer="test") == 300


def test_prefer_id():
    assert choose_text_channel_id(CHANS, prefer="200") == 200


def test_prefer가_일반보다_우선():
    assert choose_text_channel_id(CHANS, prefer="archive") == 200


def test_기본은_일반():
    assert choose_text_channel_id(CHANS) == 100


def test_일반없으면_시스템채널():
    assert choose_text_channel_id([(200, "archive")], system_channel_id=999) == 999


def test_일반도_시스템도_없으면_첫채널():
    assert choose_text_channel_id([(200, "archive"), (300, "test")]) == 200


def test_채널없으면_None():
    assert choose_text_channel_id([]) is None


# --- async 해석기: 길드 ID → 텍스트 채널 ---

def test_resolve_길드ID를_일반채널로(monkeypatch):
    monkeypatch.delenv("ORGANT_CHANNEL", raising=False)
    guild = SimpleNamespace(
        id=555,
        text_channels=[SimpleNamespace(id=100, name="일반"), SimpleNamespace(id=300, name="test")],
        system_channel=None,
    )

    async def _fetch_channel(cid):
        return None  # 채널로는 못 찾음(길드 ID라서)

    client = SimpleNamespace(
        get_channel=lambda cid: None,
        fetch_channel=_fetch_channel,
        get_guild=lambda cid: guild if cid == 555 else None,
    )
    assert asyncio.run(resolve_channel_id(client, 555)) == 100
