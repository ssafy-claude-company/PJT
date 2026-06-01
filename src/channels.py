"""대상 채널 해석.

env CHANNEL_ID 가 (실수로) 길드(서버) ID 이거나 텍스트 채널 ID 일 수 있다.
연결된 봇으로 실제 '대상 텍스트 채널'을 해석한다.

우선순위:
  1) CHANNEL_ID 가 텍스트 채널이면 그대로 사용
  2) CHANNEL_ID 가 길드면 그 안에서:
       ORGANT_CHANNEL(env, 이름 또는 ID) → '일반' → 시스템 채널 → 첫 텍스트 채널
"""
import os

import discord


def choose_text_channel_id(text_channels, prefer=None, system_channel_id=None):
    """(id, name) 목록에서 대상 텍스트 채널 ID를 고른다. (순수 함수)

    text_channels: [(id, name), ...]
    prefer: 채널 이름 또는 ID 문자열(우선 선택)
    """
    if prefer:
        for cid, name in text_channels:
            if str(cid) == str(prefer) or name == str(prefer):
                return cid
    for cid, name in text_channels:
        if name == "일반":
            return cid
    if system_channel_id is not None:
        return system_channel_id
    if text_channels:
        return text_channels[0][0]
    return None


def _is_text_channel(obj) -> bool:
    return isinstance(obj, discord.TextChannel)


async def resolve_channel_id(client, configured_id, prefer=None):
    """연결된 client로 configured_id(채널 또는 길드)에서 대상 텍스트 채널 ID를 해석한다."""
    prefer = prefer or (os.environ.get("ORGANT_CHANNEL", "").strip() or None)

    # 1) 채널로 직접 시도
    obj = client.get_channel(configured_id)
    if obj is None:
        try:
            obj = await client.fetch_channel(configured_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            obj = None
    if obj is not None and _is_text_channel(obj):
        return obj.id

    # 2) 길드로 해석
    guild = client.get_guild(configured_id)
    if guild is None:
        try:
            guild = await client.fetch_guild(configured_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            guild = None
    if guild is None:
        raise RuntimeError(f"CHANNEL_ID={configured_id} 를 채널/길드로 해석할 수 없습니다.")

    texts = [(c.id, c.name) for c in getattr(guild, "text_channels", [])]
    sys_id = guild.system_channel.id if getattr(guild, "system_channel", None) else None
    chosen = choose_text_channel_id(texts, prefer, sys_id)
    if chosen is None:
        raise RuntimeError(f"길드 {guild.id} 에서 사용할 텍스트 채널을 찾지 못했습니다.")
    return chosen
