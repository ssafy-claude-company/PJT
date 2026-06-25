"""run_organt_sns — Organt 두뇌(SYS)를 SnsGuide로 띄우는 러너 (Phase 2: 라이브 OUTPUT).

  [아키텍처] 매체(SNS)만 교체한다 — SYS(중앙통제)·Agent(클로드 세션)는 그대로. main.py가
  DiscordGuide + on_message 리스너로 하던 걸, 여기선 SnsGuide + 요청 폴링으로 한다:

    INPUT : 채널/스튜디오 요청(GuideMessage sender_id=0, type=request)을 폴링 →
            Request 재구성 → Sys.route_channel_request(channel_id, req)
    OUTPUT: Sys가 흐름을 돌리며 flow.guide.post/send_response/… 를 호출 → GuideMessage가
            쌓이고 채널 메신저에 라이브로 렌더.

  두 모드:
    (로컬)  ORM SnsGuide  — 같은 Django DB. 테스트/단일컨테이너용.   기본
    (원격)  HttpSnsGuide  — egress가 HTTPS 전용이라, 배포된 SNS(Render)에 guide_bridge로 말한다.
            --remote https://... (+ ORGANT_GUIDE_TOKEN). 두뇌는 여기(클로드 CLI), 매체는 Render.

  디스코드 비의존: load_config(SYSTEM_BOT 강제) 우회 — Config 직접 생성. 라이브 디스코드 SYS와
  분리된 상태 디렉토리(organt_sns_state, organt_sns_workspace) 사용.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand

_PJT = os.environ.get("ORGANT_PJT", "/home/user/PJT")
if _PJT not in sys.path:
    sys.path.insert(0, _PJT)

from sns.sns_guide import SnsGuide                  # noqa: E402
from sns.http_sns_guide import HttpSnsGuide         # noqa: E402
from sns.models import Agent, GuideMessage          # noqa: E402

SNS_GUILD_ID = 1
_STATE = Path(_PJT) / "organt_sns_state"
_WORKSPACE = Path(_PJT) / "organt_sns_workspace"


def _build_config():
    """load_config 우회 — 디스코드 토큰 없이 Config 직접 생성(러너는 디스코드 미연결)."""
    from src.config import Config
    _STATE.mkdir(parents=True, exist_ok=True)
    _WORKSPACE.mkdir(parents=True, exist_ok=True)
    return Config(
        system_bot_token="", channel_id=0,
        model=os.environ.get("ORGANT_MODEL", "").strip() or None,
        workspace_dir=_WORKSPACE, audit_log_path=_STATE / "audit.jsonl")


# ── 로컬(ORM) 입출력 ──────────────────────────────────────────────
def _local_roster():
    from django.db.models import Count
    bots = list(Agent.objects.annotate(_ec=Count("events")).order_by("-_ec"))
    bot_info = {int(a.bot_id): (a.role or "예비") for a in bots}
    leader = next((int(a.bot_id) for a in bots if a.is_leader), None)
    if leader is None and bots:
        leader = int(bots[0].bot_id)
    return bot_info, leader


def _local_models():
    """{bot_id: model} — per-agent 모델이 지정된 봇만(빈값=러너 전역 기본)."""
    return {int(a.bot_id): a.model.strip() for a in Agent.objects.exclude(model="") if a.model.strip()}


def _local_pending(seen):
    out = []
    for m in GuideMessage.objects.filter(msg_type="request", sender_id=0).order_by("msg_id"):
        if m.msg_id in seen or (m.payload or {}).get("picked"):
            continue
        out.append({"msg_id": m.msg_id, "channel_id": m.channel_id, "to_id": m.to_id,
                    "kind": m.kind, "body": m.body})
    return out


def _local_pick(msg_id, done=False):
    m = GuideMessage.objects.filter(msg_id=msg_id).first()
    if not m:
        return
    p = dict(m.payload or {})
    p["picked"] = True
    if done:
        p["done_ts"] = time.time()
    else:
        p.setdefault("picked_ts", time.time())   # 멎은 요청 판정(픽 후 무응답 경과)용
    GuideMessage.objects.filter(msg_id=msg_id).update(payload=p)


def _local_stop_pending(channel_id):
    """이 채널에 '작업 중지' 신호가 있으면 소거하고 True(러너 로컬 모드)."""
    from sns.models import StopSignal
    return StopSignal.objects.filter(channel_id=channel_id).delete()[0] > 0


def _local_interject_pending(channel_id):
    """이 채널의 '진행 중 개입' 신호들을 소거하고 [{target_id, text}] 반환(러너 로컬 모드)."""
    from sns.models import InterjectSignal
    sigs = list(InterjectSignal.objects.filter(channel_id=channel_id).order_by("id"))
    if sigs:
        InterjectSignal.objects.filter(id__in=[s.id for s in sigs]).delete()
    return [{"target_id": s.target_id, "text": s.text} for s in sigs]


class Command(BaseCommand):
    help = "Organt SYS를 SnsGuide로 띄워 채널 요청을 라이브 협업으로 처리한다(디스코드 비의존)."

    def add_arguments(self, parser):
        parser.add_argument("--bootstrap-check", action="store_true",
                            help="구성만 검증하고 종료(에이전트 미실행 — 토큰 0).")
        parser.add_argument("--once", action="store_true", help="대기 요청만 1회 처리 후 종료.")
        parser.add_argument("--poll", type=float, default=3.0, help="폴링 간격(초).")
        parser.add_argument("--remote", default="", help="원격 SNS base URL(예: https://organt-sns.onrender.com). "
                                                         "비우면 로컬 ORM 모드.")
        parser.add_argument("--token", default="", help="guide bridge 토큰(없으면 ORGANT_GUIDE_TOKEN env).")

    def handle(self, *args, **opts):
        asyncio.run(self._main(opts))

    async def _main(self, opts):
        from src.sys_core import Sys
        from src.main import _make_builder
        from src.audit import AuditLog
        from src.protocol import Request, Kind

        remote = opts["remote"].strip()
        token = opts["token"].strip() or os.environ.get("ORGANT_GUIDE_TOKEN", "").strip()
        cfg = _build_config()
        audit = AuditLog(cfg.audit_log_path)

        # ── 모드별 guide + 입출력 바인딩 ──────────────────────────────
        if remote:
            if not token:
                self.stderr.write("--remote 에는 토큰이 필요합니다(--token 또는 ORGANT_GUIDE_TOKEN). 종료.")
                return
            guide = HttpSnsGuide(remote, token)
            bot_info, leader, model_map = await self._remote_roster(guide)

            async def fetch_pending(seen):
                data = await guide._get("/api/guide/pending/")
                return [p for p in data.get("pending", []) if p["msg_id"] not in seen]

            async def mark_pick(mid, done=False):
                await guide._post("/api/guide/pick/", {"msg_id": mid, "done": done})

            async def _beat():
                await guide._post("/api/guide/heartbeat/", {"note": "remote"})

            async def _check_stop(ch):
                d = await guide._get(f"/api/guide/stops/?channel={ch}")
                return bool(d.get("stopped"))

            async def _check_interject(ch):
                d = await guide._get(f"/api/guide/interjects/?channel={ch}")
                return d.get("infos", [])
            where = f"원격 {remote}"
        else:
            guide = SnsGuide()
            bot_info, leader = await sync_to_async(_local_roster)()
            model_map = await sync_to_async(_local_models)()

            async def fetch_pending(seen):
                return await sync_to_async(_local_pending)(seen)

            async def mark_pick(mid, done=False):
                await sync_to_async(_local_pick)(mid, done)

            async def _beat():
                from sns.models import EngineHeartbeat
                await sync_to_async(EngineHeartbeat.beat)("local")

            async def _check_stop(ch):
                return await sync_to_async(_local_stop_pending)(ch)

            async def _check_interject(ch):
                return await sync_to_async(_local_interject_pending)(ch)
            where = "로컬 ORM"

        if not bot_info:
            self.stderr.write("로스터가 비어 있습니다 — 봇을 먼저 채용하세요(스튜디오). 종료.")
            return

        # per-agent 모델 — 빌더 팩토리에 model_map을 넘겨 봇별 LLM 주입(빈 맵이면 디스코드 경로와 동일).
        sysm = Sys(
            guide, SNS_GUILD_ID, _make_builder(cfg, audit, bot_info, model_map), bot_info=bot_info,
            workspace=str(cfg.workspace_dir), projects_path=str(_STATE / "projects.json"),
            session_dir=str(_STATE), jobs_path=str(_STATE / "jobs.json"), seed_path=None)

        self.stdout.write(f"[SnsGuide 러너 · {where}] 직원 {len(bot_info)}명 · 리더={bot_info.get(leader)}({leader})")
        self.stdout.write(f"  상태(라이브 SYS와 분리): {_STATE} · 전역 모델: {cfg.model or '(SDK 기본)'}")
        if model_map:
            self.stdout.write(f"  per-agent 모델 {len(model_map)}명: " +
                              ", ".join(f"{bot_info.get(b, b)}={m}" for b, m in list(model_map.items())[:8]))

        if opts["bootstrap_check"]:
            pend = await fetch_pending(set())
            self.stdout.write(f"  대기 요청: {len(pend)}건")
            for m in pend[:10]:
                self.stdout.write(f"    · ch={m['channel_id']} kind={m['kind']} to={m['to_id']} body={m['body'][:46]!r}")
            self.stdout.write("[bootstrap-check] 구성 정상 — Sys/Guide/builder 연결 OK. (에이전트 미실행)")
            return

        seen = set()
        last_beat = 0.0
        self.stdout.write("요청 폴링 시작 — 채널/스튜디오에서 봇에게 요청하면 라이브로 처리됩니다. (Ctrl+C 종료)")
        while True:
            try:
                _now = asyncio.get_event_loop().time()      # 엔진 생존 신호 — 폴마다(8초 throttle)
                if _now - last_beat > 8:
                    try:
                        await _beat()
                    except Exception:
                        pass
                    last_beat = _now
                pend = await fetch_pending(seen)
                for m in pend:
                    mid = m["msg_id"]
                    seen.add(mid)
                    to_id = int(m["to_id"]) if m["to_id"] else leader
                    kind = Kind.WORK if (m["kind"] or "W") == "W" else Kind.INFO
                    req = Request(to_id=to_id, kind=kind, body=m["body"], from_id=0, message_id=str(mid))
                    await mark_pick(mid)
                    self.stdout.write(f"▶ 요청 처리: ch={m['channel_id']} to={to_id} kind={m['kind']} body={m['body'][:46]!r}")
                    # 협업을 '요청이 온 채널'에 라우팅 — 위임·작업이 사용자 채널에 보이게.
                    ch = int(m["channel_id"])
                    setattr(guide, "_origin_channel", ch)
                    try:
                        # [stale stop 방지] 픽 직전 잔여(이전 흐름의) 중지 신호를 소거 — 짧은 흐름이 폴 전에
                        # 끝나 신호가 남았다가, 같은 채널의 새 흐름을 시작하자마자 죽이는 것 차단.
                        try:
                            await _check_stop(ch)
                        except Exception:
                            pass
                        # 흐름을 태스크로 돌리며 '작업 중지' 신호를 폴 — 사용자 트리거를 진행 중 흐름에
                        # 협조적 취소(SYS.request_cancel)로 잇는다(러너=두뇌와 같은 이벤트루프).
                        flow_task = asyncio.create_task(sysm.route_channel_request(ch, req))
                        while not flow_task.done():
                            d, _ = await asyncio.wait({flow_task}, timeout=2)
                            if flow_task in d:
                                break
                            try:
                                if await _check_stop(ch):
                                    sysm.request_cancel(ch)
                                    self.stdout.write(f"■ 작업 중지 요청 수신 — ch={ch}")
                            except Exception:
                                pass
                            try:
                                # 사람 '진행 중 개입' — 폴해서 대상 봇 다음 턴 프롬프트에 주입(deliver_human_info).
                                for info in await _check_interject(ch):
                                    ok = sysm.deliver_human_info(ch, info.get("target_id"), info.get("text"))
                                    self.stdout.write(f"✎ 사람 개입 {'주입' if ok else '미주입(흐름없음)'} — ch={ch}")
                            except Exception:
                                pass
                        await flow_task
                        await mark_pick(mid, done=True)
                        self.stdout.write(f"✓ 처리 완료: msg_id={mid}")
                    except Exception as e:
                        import traceback
                        self.stderr.write(f"✗ 처리 실패 msg_id={mid}: {e}\n{traceback.format_exc()}")
                if opts["once"]:
                    self.stdout.write(f"[--once] {'대기 요청 없음' if not pend else '처리 완료'} — 종료.")
                    return
            except KeyboardInterrupt:
                self.stdout.write("종료 신호 — 폴링 중단.")
                return
            except Exception as e:
                import traceback
                self.stderr.write(f"폴링 루프 오류: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(opts["poll"])

    async def _remote_roster(self, guide):
        """원격 /api/agents/ → bot_info({bot_id: role}), 리더, model_map({bot_id: model})."""
        data = await guide._get("/api/agents/")
        rows = data if isinstance(data, list) else data.get("results", [])
        bot_info, leader, model_map = {}, None, {}
        best = -1
        for a in rows:
            bid = int(a["bot_id"])
            bot_info[bid] = a.get("role") or "예비"
            if (a.get("model") or "").strip():
                model_map[bid] = a["model"].strip()
            if a.get("is_leader"):
                leader = bid
            if (a.get("event_count") or 0) > best:
                best, _fallback = a.get("event_count") or 0, bid
        if leader is None and rows:
            leader = int(rows[0]["bot_id"])
        return bot_info, leader, model_map
