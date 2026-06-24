"""run_organt_sns — Organt 두뇌(SYS)를 SnsGuide로 띄우는 러너 (Phase 2: 라이브 OUTPUT).

  [아키텍처] 매체(SNS)만 교체한다 — SYS(중앙통제)·Agent(클로드 세션)는 그대로.
  main.py가 DiscordGuide + on_message 리스너로 하던 걸, 여기선 SnsGuide + GuideMessage 폴링으로 한다:

    INPUT : 스튜디오/채널에서 만든 요청(GuideMessage sender_id=0, type=request)을 폴링 →
            Request 재구성 → Sys.route_channel_request(channel_id, req)
    OUTPUT: Sys가 흐름을 돌리며 flow.guide(=SnsGuide).post/send_response/… 를 호출 →
            GuideMessage가 쌓이고 채널 메신저에 라이브로 뜬다.

  디스코드 비의존: Config는 직접 만든다(load_config는 SYSTEM_BOT 토큰을 강제) — 토큰/채널은 미사용 더미,
  model/workspace/audit만 실제. 라이브 디스코드 SYS와 *완전히 분리*된 상태 디렉토리를 쓴다(충돌 0).
"""
import asyncio
import os
import sys
import time
from pathlib import Path

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand

# Organt 두뇌(src) — protocol/Sys/builder. SnsGuide와 같은 경로 부트스트랩.
_PJT = os.environ.get("ORGANT_PJT", "/home/user/PJT")
if _PJT not in sys.path:
    sys.path.insert(0, _PJT)

from sns.sns_guide import SnsGuide          # noqa: E402
from sns.models import Agent, GuideMessage  # noqa: E402

# SNS 매체의 길드 스코프(디스코드 길드 id 자리) — SnsGuide는 대부분 무시. 고정 상수.
SNS_GUILD_ID = 1

# 두뇌 상태는 라이브 디스코드 SYS와 *분리*된 곳에 — projects.json/jobs.json/세션/audit.
_STATE = Path(_PJT) / "organt_sns_state"
_WORKSPACE = Path(_PJT) / "organt_sns_workspace"


def _build_config():
    """load_config 우회 — 디스코드 토큰 없이 Config를 직접 만든다(러너는 디스코드에 연결 안 함)."""
    from src.config import Config
    _STATE.mkdir(parents=True, exist_ok=True)
    _WORKSPACE.mkdir(parents=True, exist_ok=True)
    return Config(
        system_bot_token="",                                  # 미사용(디스코드 미연결)
        channel_id=0,                                         # 미사용(SNS 자체 channel_id로 라우팅)
        model=os.environ.get("ORGANT_MODEL", "").strip() or None,
        workspace_dir=_WORKSPACE,
        audit_log_path=_STATE / "audit.jsonl",
    )


def _load_roster_sync():
    """SNS Agent 테이블 → bot_info({bot_id: role})와 리더 bot_id."""
    from django.db.models import Count
    # event_count는 뷰 어노테이션 — 모델 필드가 아니다. 활동량은 actor 역참조(events)로 센다.
    bots = list(Agent.objects.annotate(_ec=Count("events")).order_by("-_ec"))
    bot_info = {int(a.bot_id): (a.role or "예비") for a in bots}
    leader = next((int(a.bot_id) for a in bots if a.is_leader), None)
    if leader is None and bots:
        leader = int(bots[0].bot_id)                          # 리더 미지정 시 최다활동 봇
    return bot_info, leader


def _pending_requests_sync(seen):
    """처리 안 된 '사용자/스튜디오 요청'(sender_id=0, type=request)만. 봇 위임(sender_id=bot)은 흐름 내부라 제외."""
    out = []
    for m in GuideMessage.objects.filter(msg_type="request", sender_id=0).order_by("msg_id"):
        if m.msg_id in seen or (m.payload or {}).get("picked"):
            continue
        out.append(m)
    return out


def _mark_picked_sync(msg_id, done=False):
    m = GuideMessage.objects.filter(msg_id=msg_id).first()
    if not m:
        return
    p = dict(m.payload or {})
    p["picked"] = True
    if done:
        p["done_ts"] = time.time()
    GuideMessage.objects.filter(msg_id=msg_id).update(payload=p)


class Command(BaseCommand):
    help = "Organt SYS를 SnsGuide로 띄워 채널 요청을 라이브 협업으로 처리한다(디스코드 비의존)."

    def add_arguments(self, parser):
        parser.add_argument("--bootstrap-check", action="store_true",
                            help="구성만 검증하고 종료(에이전트 미실행 — 토큰 0 소비).")
        parser.add_argument("--once", action="store_true",
                            help="현재 대기 중인 요청만 1회 처리하고 종료(테스트용 바운드).")
        parser.add_argument("--poll", type=float, default=2.0, help="폴링 간격(초).")

    def handle(self, *args, **opts):
        asyncio.run(self._main(opts))

    async def _main(self, opts):
        from src.sys_core import Sys
        from src.main import _make_builder
        from src.audit import AuditLog
        from src.protocol import Request, Kind

        cfg = _build_config()
        audit = AuditLog(cfg.audit_log_path)
        bot_info, leader = await sync_to_async(_load_roster_sync)()
        if not bot_info:
            self.stderr.write("로스터가 비어 있습니다 — 봇을 먼저 채용하세요(스튜디오). 종료.")
            return
        guide = SnsGuide()
        sysm = Sys(
            guide, SNS_GUILD_ID, _make_builder(cfg, audit, bot_info), bot_info=bot_info,
            workspace=str(cfg.workspace_dir),
            projects_path=str(_STATE / "projects.json"),
            session_dir=str(_STATE),
            jobs_path=str(_STATE / "jobs.json"),
            seed_path=None,
        )

        self.stdout.write(f"[SnsGuide 러너] 직원 {len(bot_info)}명 · 리더={bot_info.get(leader)}({leader})")
        self.stdout.write(f"  상태 디렉토리(라이브 SYS와 분리): {_STATE}")
        self.stdout.write(f"  작업공간: {cfg.workspace_dir} · 모델: {cfg.model or '(SDK 기본)'}")

        if opts["bootstrap_check"]:
            pend = await sync_to_async(_pending_requests_sync)(set())
            self.stdout.write(f"  대기 요청: {len(pend)}건")
            for m in pend[:10]:
                self.stdout.write(f"    · ch={m.channel_id} kind={m.kind} to={m.to_id} body={m.body[:50]!r}")
            self.stdout.write("[bootstrap-check] 구성 정상 — Sys/SnsGuide/builder 연결 OK. (에이전트 미실행)")
            return

        seen = set()
        self.stdout.write("요청 폴링 시작 — 채널/스튜디오에서 봇에게 요청하면 라이브로 처리됩니다. (Ctrl+C 종료)")
        while True:
            try:
                pend = await sync_to_async(_pending_requests_sync)(seen)
                for m in pend:
                    seen.add(m.msg_id)
                    to_id = int(m.to_id) if m.to_id else leader
                    kind = Kind.WORK if (m.kind or "W") == "W" else Kind.INFO
                    req = Request(to_id=to_id, kind=kind, body=m.body,
                                  from_id=0, message_id=str(m.msg_id))
                    await sync_to_async(_mark_picked_sync)(m.msg_id)
                    self.stdout.write(f"▶ 요청 처리: ch={m.channel_id} to={to_id} kind={m.kind} body={m.body[:48]!r}")
                    try:
                        await sysm.route_channel_request(int(m.channel_id), req)
                        await sync_to_async(_mark_picked_sync)(m.msg_id, done=True)
                        self.stdout.write(f"✓ 처리 완료: msg_id={m.msg_id}")
                    except Exception as e:
                        import traceback
                        self.stderr.write(f"✗ 처리 실패 msg_id={m.msg_id}: {e}\n{traceback.format_exc()}")
                if opts["once"]:
                    if not pend:
                        self.stdout.write("[--once] 대기 요청 없음 — 종료.")
                    else:
                        self.stdout.write("[--once] 대기 요청 처리 완료 — 종료.")
                    return
            except KeyboardInterrupt:
                self.stdout.write("종료 신호 — 폴링 중단.")
                return
            except Exception as e:
                import traceback
                self.stderr.write(f"폴링 루프 오류: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(opts["poll"])
