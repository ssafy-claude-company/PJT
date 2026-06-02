"""라이브 데모 — Organt 팀이 실제 Discord에서 협업해 결과를 만든다.

실제 운영에선 사람(User)이 채널에 [Request]를 올리지만, 이 데모는 재현을 위해
System 봇으로 [Request]를 올린 뒤 SYS의 입구(route_channel_request)로 흘려보낸다.
(엔트리 `python -m src.main` 은 사람이 올린 [Request]를 on_message로 받아 같은 입구로 보낸다.)

설정은 .env/환경변수로 받는다(하드코딩 없음):
  SYSTEM_BOT, CHANNEL_ID, ORGANT_ROSTER  (config/.env.example 참고)

실행:
  python -m scripts.demo                       # 기본 과제(TODO 웹앱)
  python -m scripts.demo "원하는 과제 문장"     # 과제 직접 지정
"""
import asyncio
import os
import shutil
import subprocess
import sys

import discord
from claude_agent_sdk import HookMatcher

from src.audit import AuditLog, make_post_tool_use_hook
from src.config import load_config
from src.discord_guide import DiscordGuide
from src.guide_tools import LEADER_TOOLS, REQUEST_TOOL
from src.main import load_roster
from src.organt import Organt, build_options
from src.permissions import make_pre_tool_use_hook
from src.protocol import Kind, format_request
from src.sys_core import Sys

DEFAULT_TASK = (
    "할 일 추가/완료/삭제가 되는 TODO 웹앱을 만들어줘. 담당자가 백엔드를 직접 만들고, "
    "프론트엔드·디자인 동료에게 화면과 스타일을 맡겨 실제로 연동되게 해줘."
)


async def _connect(token):
    intents = discord.Intents.default()
    intents.message_content = True
    c = discord.Client(intents=intents)
    ready = asyncio.Event()

    @c.event
    async def on_ready():
        ready.set()

    t = asyncio.create_task(c.start(token))
    await asyncio.wait_for(ready.wait(), 30)
    return c, t


async def _dump(client, channel_id, label, n=16):
    ch = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
    msgs = [m async for m in ch.history(limit=n)]
    msgs.reverse()
    print(f"--- {label} ({channel_id}) ---")
    for m in msgs:
        print(f"  [{m.author.name}] {m.content[:120].replace(chr(10), ' / ')}")


async def main():
    task_text = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TASK
    cfg = load_config()
    audit = AuditLog(cfg.audit_log_path)

    system_client, st = await _connect(cfg.system_bot_token)
    organts, bot_info, tasks, leader_id = {}, {}, [st], None
    for i, (token, role) in enumerate(load_roster()):
        c, t = await _connect(token)
        organts[c.user.id] = c
        bot_info[c.user.id] = role
        tasks.append(t)
        if i == 0:
            leader_id = c.user.id
    print(f"리더={bot_info[leader_id]}({leader_id}) 팀={list(bot_info.values())}\n")

    guide = DiscordGuide(system_client, organts)
    channel = (system_client.get_channel(cfg.channel_id)
               or await system_client.fetch_channel(cfg.channel_id))

    # 깨끗한 작업공간/세션에서 시작
    ws = cfg.workspace_dir
    if ws.exists():
        for p in ws.iterdir():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    for sp in cfg.audit_log_path.parent.glob("organt_state_*.json"):
        sp.unlink()

    def organt_builder(organt_id, server, role):
        allowed = ["Read", "Write", "Edit", "Glob", "ToolSearch", REQUEST_TOOL]
        if role == "leader":
            allowed = allowed + LEADER_TOOLS
        return Organt(cfg, build_options(
            cfg, allowed_tools=allowed, mcp_servers={"guide": server}, max_turns=26,
            hooks={"PreToolUse": [HookMatcher(hooks=[make_pre_tool_use_hook(audit, allowed)])],
                   "PostToolUse": [HookMatcher(hooks=[make_post_tool_use_hook(audit)])]},
        ), state_path=str(cfg.audit_log_path.parent / f"organt_state_{organt_id}.json"))

    sysm = Sys(guide, channel.guild.id, organt_builder, bot_info=bot_info)

    # User [Request]를 채널에 올리고(데모: System 봇이 대신) SYS 입구로 라우팅.
    await guide.post(cfg.channel_id, leader_id, format_request(leader_id, Kind.WORK, task_text))
    print(f"[User→#{channel.name}] {task_text}\n")
    req = await sysm.read_latest_request(cfg.channel_id)
    out = await sysm.route_channel_request(cfg.channel_id, req)
    flow = out["flow"]

    print(f"\n=== project_channel={flow.project_channel} comm_done={flow.comm.done} ===")
    if flow.project_channel and flow.thread_id:
        await _dump(system_client, int(flow.thread_id), "TASK 스레드 (협업 로그)")
    await _dump(system_client, cfg.channel_id, f"#{channel.name} (보고)", n=3)
    print("\n=== 산출물 ===")
    print(subprocess.run(["find", str(ws), "-type", "f", "-not", "-path", "*/node_modules/*"],
                         capture_output=True, text=True).stdout)

    for c in [system_client, *organts.values()]:
        await c.close()
    for t in tasks:
        t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
