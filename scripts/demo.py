"""라이브 데모 — Organt 팀이 실제 Discord에서 협업해 결과를 만든다.

실제 운영에선 사람(User)이 채널에 [Request]를 올리지만, 이 데모는 재현을 위해
System 봇으로 [Request]를 올린 뒤 SYS의 입구(route_channel_request)로 흘려보낸다.
(엔트리 `python -m organt_discord.main` 은 사람이 올린 [Request]를 on_message로 받아 같은 입구로 보낸다.)

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

from organt_core.audit import AuditLog, make_post_tool_use_hook
from organt_core.config import load_config
from organt_discord.discord_guide import DiscordGuide
from organt_core.guide_tools import COORD_TOOLS, FLOW_TOOLS, LEADER_TOOLS
from organt_discord.main import load_roster
from organt_core.organt import Organt, build_options
from organt_core.permissions import make_pre_tool_use_hook
from organt_core.protocol import Kind, format_request
from organt_core.sys_core import Sys

DEFAULT_TASK = (
    # 결과(outcome)만 말한다 — 누가 무엇을 맡을지·인터페이스는 팀이 협의로 정한다(미리 분배 금지).
    "할 일을 추가·완료·삭제할 수 있는 TODO 웹앱을 만들어줘. 실제로 동작하고 보기 좋게."
)


async def _connect(token, message_content=False):
    # 일시적 Discord WS/DNS 블립엔 재시도(main.py._connect와 동일) — 한 봇의 연결 실패로 데모가
    # 통째로 죽지 않도록. message_content 특권 인텐트는 System 봇만(내용 읽기) — Organt는 게시·타이핑만.
    intents = discord.Intents.default()
    intents.message_content = message_content
    last = None
    for attempt in range(4):
        c = discord.Client(intents=intents)
        ready = asyncio.Event()

        @c.event
        async def on_ready():
            ready.set()

        try:
            t = asyncio.create_task(c.start(token))
            await asyncio.wait_for(ready.wait(), 30)
            return c, t
        except Exception as e:
            last = e
            try:
                await c.close()
            except Exception:
                pass
            await asyncio.sleep(3 * (attempt + 1))
    raise last


def _collab_summary(flow, bot_info):
    """flow.comm.history로 실제 협업(누가 누구에게, Info/Work, 반복=재질문)을 분석·출력."""
    def name(i):
        lbl = bot_info.get(i, "User" if i == 0 else str(i))
        return lbl.split("(")[0]
    edges = {}   # (from,to,kind) -> count
    order = []
    for ev in flow.comm.history:
        if ev[0] == "request":
            _, frm, to, _, knd = ev
            if frm == 0:
                continue   # origin→leader(시작)은 제외
            k = getattr(knd, "value", str(knd))
            edges[(frm, to, k)] = edges.get((frm, to, k), 0) + 1
            order.append((frm, to, k))
    info = sum(c for (_, _, k), c in edges.items() if k.lower().startswith("i"))
    work = sum(c for (_, _, k), c in edges.items() if k.lower().startswith("w"))
    print("\n=== 협업 분석(검증) ===")
    print(f"P2P 요청 총 {len(order)}건  (Info 질의 {info} / Work 위임 {work})")
    print("흐름 순서: " + " | ".join(f"{name(f)}→{name(t)}[{k}]" for f, t, k in order))
    repeats = {e: c for e, c in edges.items() if c > 1}
    if repeats:
        print("재질문/반복 왕복:")
        for (f, t, k), c in repeats.items():
            print(f"  {name(f)}→{name(t)}[{k}] × {c}")
    else:
        print("재질문/반복: 없음")
    askers = {name(f) for f, _, _ in order}
    print(f"질문/위임을 '시작한' 주체 수: {len(askers)} ({', '.join(sorted(askers))})  "
          f"— 1명 초과면 단방향(리더독단) 아님")


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

    system_client, st = await _connect(cfg.system_bot_token, message_content=True)
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

    def organt_builder(organt_id, server, role, flow=None):
        # 리더도 한 명의 직원 — 구현 도구(Write/Edit)를 그대로 갖는다. 차이는 권한이 아니라
        # 역할: 목표는 팀 합의로 정하고(set_goal), Work 위임 본문은 '구현 스펙'이 아니라
        # '측정가능한 목표'이며, 받은 owner가 구현·검증·증거보고까지 끝까지 책임진다.
        allowed = ["Read", "Write", "Edit", "Glob", "Grep", "ToolSearch", *FLOW_TOOLS]
        turns = 60              # 동료가 한 산출물을 한 번의 위임으로 끝내도록 여유(턴한도 미완 반환 줄임)
        if role == "leader":
            allowed = allowed + LEADER_TOOLS
            turns = 220         # 대부분 빌드가 한 세그먼트로 끝나 '10분마다 continue 재호출' 경계가 드물게
        label = bot_info.get(organt_id, role)   # 협업 관찰성: 로그에 '누가' 남기기
        # 리더 추론 기록(관측): '왜 재호출하나'를 추측 말고 직접 보려고 매 발화를 audit에 남긴다.
        narrate = ((lambda t: audit.record("narration", actor=organt_id, role=label, text=t[:800]))
                   if role == "leader" else None)
        return Organt(cfg, build_options(
            cfg, allowed_tools=allowed, mcp_servers={"guide": server}, max_turns=turns,
            hooks={"PreToolUse": [HookMatcher(hooks=[make_pre_tool_use_hook(audit, allowed, actor=organt_id, role=label, flow=flow)])],
                   "PostToolUse": [HookMatcher(hooks=[make_post_tool_use_hook(audit, actor=organt_id, role=label)])]},
        ), state_path=str(cfg.audit_log_path.parent / f"organt_state_{organt_id}.json"), narrate=narrate)

    sysm = Sys(guide, channel.guild.id, organt_builder, bot_info=bot_info,
               workspace=cfg.workspace_dir,
               projects_path=str(cfg.audit_log_path.parent / "projects.json"),
               session_dir=str(cfg.audit_log_path.parent))

    # User [Request]를 채널에 올리고(데모: System 봇이 대신) SYS 입구로 라우팅.
    await guide.post(cfg.channel_id, leader_id, format_request(leader_id, Kind.WORK, task_text))
    print(f"[User→#{channel.name}] {task_text}\n")
    req = await sysm.read_latest_request(cfg.channel_id)
    out = await sysm.route_channel_request(cfg.channel_id, req)
    flow = out["flow"]

    print(f"\n=== project_channel={flow.project_channel} tasks={len(flow.tasks)} "
          f"comm_done={flow.comm.done} ===")
    for i, t in enumerate(flow.tasks, 1):
        print(f"\n[Task {i}/{len(flow.tasks)}] {t.status.task_id} "
              f"purpose={t.status.purpose!r} status={t.status.status}")
        await _dump(system_client, int(t.thread_id), f"Task {i} 협업 로그")
    await _dump(system_client, cfg.channel_id, f"#{channel.name} (보고)", n=3)
    _collab_summary(flow, bot_info)
    print("\n=== 산출물 ===")
    print(subprocess.run(["find", str(ws), "-type", "f", "-not", "-path", "*/node_modules/*"],
                         capture_output=True, text=True).stdout)

    for c in [system_client, *organts.values()]:
        await c.close()
    for t in tasks:
        t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
