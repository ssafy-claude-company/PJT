"""프로젝트 '개입' 데모 — 기존 프로젝트 채널에 명령을 넣어 중간 개입시킨다.

버그 있는 스네이크 산출물이 든 워크스페이스를 프로젝트로 등록하고, 그 프로젝트 채널에
개입 명령을 넣어 팀이 원인 규명→수정→(결과 기반)검증→재배포하게 한다.
실행: python -m scripts.intervene ["개입 명령"]
"""
import asyncio
import os
import subprocess
import sys

import discord
from claude_agent_sdk import HookMatcher

from src.audit import AuditLog, make_post_tool_use_hook
from src.config import load_config
from src.discord_guide import DiscordGuide
from src.guide_tools import COORD_TOOLS, FLOW_TOOLS, LEADER_TOOLS
from src.main import load_roster
from src.organt import Organt, build_options
from src.permissions import make_pre_tool_use_hook
from src.protocol import Kind, format_request
from src.sys_core import Sys

CMD = sys.argv[1] if len(sys.argv) > 1 else (
    "이 스네이크 게임은 접속하면 뱀이 시작하자마자(1초 안에) 바로 죽어버리는 치명적 버그가 있어. "
    "server.js를 Read해 원인을 규명하고 고쳐줘. 그리고 run 툴로 실제로 한 클라이언트가 접속해 "
    "최소 5초 동안 alive=false가 되지 않고(즉사하지 않고) 살아 움직이는지 직접 재현해 검증한 뒤, "
    "통과하면 deploy 툴로 'slither-multiplayer-organt' 이름으로 다시 배포해줘."
)


async def _connect(token):
    intents = discord.Intents.default()
    intents.message_content = True
    last = None
    for attempt in range(4):           # 일시적 TLS/클럭 스큐 블립에 재시도(전체 기동이 한 봇에 안 죽게)
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


async def main():
    cfg = load_config()
    # SYS의 배포누락 차단(=_ensure_deploy)이 쓸 서비스 이름. 리더가 deploy를 빼먹어도
    # SYS가 이 이름으로 강제 배포한다(자격증명이 환경에 있을 때). env로 덮어쓸 수 있음.
    os.environ.setdefault("DEPLOY_NAME", "slither-multiplayer-organt")
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
    guide = DiscordGuide(system_client, organts)
    channel = (system_client.get_channel(cfg.channel_id)
               or await system_client.fetch_channel(cfg.channel_id))

    def organt_builder(organt_id, server, role):
        # 담당자(리더)도 같은 직군 기여자(Write/run 보유) + 구조적 조율 도구(LEADER_TOOLS).
        allowed = ["Read", "Write", "Edit", "Glob", "Grep", "ToolSearch", *FLOW_TOOLS]
        turns = 34
        if role == "leader":
            allowed = allowed + LEADER_TOOLS
            turns = 70          # 7인 팀 + 품질게이트(비평·되밀기)로 턴 더 필요
        label = bot_info.get(organt_id, role)   # 협업 관찰성: 로그에 '누가' 남기기
        return Organt(cfg, build_options(
            cfg, allowed_tools=allowed, mcp_servers={"guide": server}, max_turns=turns,
            hooks={"PreToolUse": [HookMatcher(hooks=[make_pre_tool_use_hook(audit, allowed, actor=organt_id, role=label)])],
                   "PostToolUse": [HookMatcher(hooks=[make_post_tool_use_hook(audit, actor=organt_id, role=label)])]}),
            state_path=str(cfg.audit_log_path.parent / f"organt_state_{organt_id}.json"))

    sysm = Sys(guide, channel.guild.id, organt_builder, bot_info=bot_info,
               workspace=cfg.workspace_dir,
               projects_path=str(cfg.audit_log_path.parent / "projects.json"),
               session_dir=str(cfg.audit_log_path.parent))

    # 기존 산출물 워크스페이스를 프로젝트로 — 같은 이름 채널 재사용(중복 방지). 레지스트리는
    # 지우지 않는다(같은 이름이면 _register_project가 식별번호 유지 + 채널만 갱신 → ID 안 바뀜).
    PNAME = "slither-multiplayer"
    pch = await guide.create_project_channel(channel.guild.id, PNAME)
    pid = sysm._register_project(pch, PNAME, str(cfg.workspace_dir), leader_id)  # 내부 등록만(채널 앵커 X)
    print(f"프로젝트 등록: {pid} channel={pch} workspace={cfg.workspace_dir}\n개입 명령: {CMD}\n", flush=True)

    # 프로젝트 채널에 개입 명령 → 라우팅(개입 자동 감지)
    await guide.post(int(pch), leader_id, format_request(leader_id, Kind.WORK, CMD))
    req = await sysm.read_latest_request(pch)
    out = await sysm.route_channel_request(pch, req)
    flow = out.get("flow")

    print(f"\n=== 개입 결과 project={flow.project_id} tasks={len(flow.tasks)} comm_done={flow.comm.done} ===")
    for i, tk in enumerate(flow.tasks, 1):
        print(f"[Task {i}] {tk.status.task_id} {tk.status.purpose[:44]!r} status={tk.status.status} "
              f"owner={tk.status.owner!r}")
    print("산출물 server.js:",
          subprocess.run(["ls", "-la", str(cfg.workspace_dir / "server.js")],
                         capture_output=True, text=True).stdout.strip())

    for c in [system_client, *organts.values()]:
        await c.close()
    for t in tasks:
        t.cancel()


if __name__ == "__main__":
    asyncio.run(main())
