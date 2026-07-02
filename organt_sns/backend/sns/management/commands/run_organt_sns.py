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
from guide.http_sns_guide import HttpSnsGuide, ORIGIN_CHANNEL   # noqa: E402
from sns.models import Agent, GuideMessage          # noqa: E402

SNS_GUILD_ID = 1
_STATE = Path(_PJT) / "organt_sns_state"
_WORKSPACE = Path(_PJT) / "organt_sns_workspace"


def _build_config():
    """load_config 우회 — 디스코드 토큰 없이 Config 직접 생성(러너는 디스코드 미연결)."""
    from organt_core.config import Config
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
    persona_map = {int(a.bot_id): a.persona for a in bots if (a.persona or "").strip()}
    leader = next((int(a.bot_id) for a in bots if a.is_leader), None)
    if leader is None and bots:
        leader = int(bots[0].bot_id)
    return bot_info, leader, persona_map


def _local_models():
    """{bot_id: model} — per-agent 모델이 지정된 봇만(빈값=러너 전역 기본)."""
    return {int(a.bot_id): a.model.strip() for a in Agent.objects.exclude(model="") if a.model.strip()}


def _route_to(channel_id):
    """봇 미지정 요청의 기본 담당 — ① 채널(프로젝트)에 지정된 리더, ② 없으면 그 채널에서 최근 활동한 봇.
    전역 임의 리더(8명이 is_leader라 그중 하나=고은호로 쏠림) 대신 '이 채널의 담당'으로. 둘 다 없으면 None."""
    from sns.models import Project, GuideMessage
    p = Project.objects.filter(id=channel_id).first()
    if p and p.leader_id:
        return int(p.leader.bot_id)
    last = (GuideMessage.objects.filter(channel_id=channel_id).exclude(sender_id=0)
            .order_by("-msg_id").first())
    return int(last.sender_id) if last and last.sender_id else None


def _local_pending(seen):
    import time as _t
    _RESUME_AFTER = 180   # [잘린 빌드 재개] picked_ts가 이만큼 멈췄으면 그 러너 사망 → 다시 큐로(guide_bridge.pending과 동일)
    now = _t.time()
    responded = set(GuideMessage.objects.filter(msg_type="response").exclude(reply_to=None)
                    .values_list("reply_to", flat=True))
    out = []
    for m in GuideMessage.objects.filter(msg_type="request", sender_id=0).order_by("msg_id"):
        if m.msg_id in seen:
            continue
        p = m.payload or {}
        if p.get("done_ts") or p.get("stopped"):
            continue
        if p.get("picked"):
            if m.msg_id in responded or (now - (p.get("picked_ts") or now)) < _RESUME_AFTER:
                continue
        out.append({"msg_id": m.msg_id, "channel_id": m.channel_id, "to_id": m.to_id,
                    "kind": m.kind, "body": m.body, "route_to": _route_to(m.channel_id)})
    return out


def _local_pick(msg_id, done=False, touch=False, unpick=False, idle=None):
    # [원자성 — pick 레이스 차단, guide_bridge.pick과 동형] select_for_update로 행 잠금.
    # claim(집기)을 이미 집힌 행에 시도하면 False 반환 → 호출자가 이중 처리 방지.
    from django.db import transaction
    is_claim = not (unpick or done or touch)
    with transaction.atomic():
        m = GuideMessage.objects.select_for_update().filter(msg_id=msg_id).first()
        if not m:
            return False
        p = dict(m.payload or {})
        if is_claim and p.get("picked"):
            return False                             # 레이스 패배 — 재처리 금지
        if unpick:                                   # 백스톱 컷 재개 — 픽 해제(다시 큐로)
            p.pop("picked", None); p.pop("done_ts", None); p.pop("picked_ts", None)
            GuideMessage.objects.filter(msg_id=msg_id).update(payload=p)
            return True
        if idle is not None:
            p["idle_s"] = int(idle)                  # 실제 무진행(초) — 정직한 '조용' 표시용
        p["picked"] = True
        if done:
            p["done_ts"] = time.time()
        elif touch:
            p["picked_ts"] = time.time()             # 진행 갱신 — 긴 흐름이 '멎음'으로 오판되지 않게
        else:
            p.setdefault("picked_ts", time.time())   # 멎은 요청 판정용
        GuideMessage.objects.filter(msg_id=msg_id).update(payload=p)
    return True


def _local_stop_pending(channel_id):
    """이 채널에 '작업 중지' 신호가 있으면 소거하고 True(러너 로컬 모드)."""
    from sns.models import StopSignal
    return StopSignal.objects.filter(channel_id=channel_id).delete()[0] > 0


def _local_all_stops():
    """미처리 중지신호의 채널 전체를 반환+소거(러너 로컬 모드) — 매 폴 전역 스캔용(inflight 무관)."""
    from sns.models import StopSignal
    chans = list(StopSignal.objects.values_list("channel_id", flat=True))
    if chans:
        StopSignal.objects.filter(channel_id__in=chans).delete()
    return [int(c) for c in chans]


def _local_stop_channel(channel_id):
    """채널의 픽됨·미응답·미완 요청을 '중지됨'으로 종결(러너 로컬 모드) — guide_bridge.stop_channel과 동일."""
    import time as _t
    from sns.models import GuideMessage
    responded = set(GuideMessage.objects.filter(channel_id=channel_id, msg_type="response")
                    .exclude(reply_to=None).values_list("reply_to", flat=True))
    n = 0
    for m in GuideMessage.objects.filter(channel_id=channel_id, sender_id=0, msg_type="request").order_by("-msg_id"):
        p = m.payload or {}
        if p.get("picked") and not p.get("done_ts") and m.msg_id not in responded:
            p = dict(p); p["stopped"] = True; p["done_ts"] = _t.time()
            GuideMessage.objects.filter(msg_id=m.msg_id).update(payload=p)
            n += 1
    return n


def _local_interject_pending(channel_id):
    """이 채널의 '진행 중 개입' 신호들을 소거하고 [{target_id, text}] 반환(러너 로컬 모드)."""
    from sns.models import InterjectSignal
    sigs = list(InterjectSignal.objects.filter(channel_id=channel_id).order_by("id"))
    if sigs:
        InterjectSignal.objects.filter(id__in=[s.id for s in sigs]).delete()
    return [{"target_id": s.target_id, "text": s.text} for s in sigs]


def _flow_idle(sysm, ch):
    """이 채널 활성 흐름의 '무진행 시간'(초) — 봇 활동(last_activity)이 멈춘 지 얼마나 됐나. 흐름 없으면 None.
    채널→흐름 매칭은 request_cancel과 동일(user_channel). 정체 기준 슬롯 회수의 신호."""
    for f in list(getattr(sysm, "active_flows", {}).values()):
        if getattr(f, "user_channel", None) == int(ch) and not getattr(f, "done", False):
            la = getattr(f, "last_activity", None)
            return None if la is None else max(0.0, time.monotonic() - la)
    return None


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
        from organt_core.sys_core import Sys
        from organt.builder import _make_builder   # [계층 분리] Core 빌더 — 종전 src.main(Discord 진입)에서 가져와 discord를 transitively 끌어오던 누수 해소
        from organt_core.audit import AuditLog
        from organt_core.protocol import Request, Kind

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
            bot_info, leader, model_map, persona_map = await self._remote_roster(guide)

            # [배달=Guide 구현체] 수신·pick·heartbeat 등은 HttpSnsGuide가 구현 — 여기선 그 계약을 호출만(모드분기 제거 진행)
            async def fetch_pending(seen):
                return [p for p in await guide.get_pending() if p["msg_id"] not in seen]

            async def mark_pick(mid, done=False, touch=False, unpick=False, idle=None):
                return await guide.pick(mid, done=done, touch=touch, unpick=unpick, idle=idle)

            async def _beat():
                await guide.heartbeat("remote")

            async def _check_stop(ch):
                return await guide.check_stop(ch)

            async def fetch_all_stops():
                return await guide.all_stops()

            async def mark_channel_stopped(ch):
                await guide.mark_stopped(ch)

            async def _check_interject(ch):
                return await guide.check_interject(ch)
            where = f"원격 {remote}"
        else:
            guide = SnsGuide()
            bot_info, leader, persona_map = await sync_to_async(_local_roster)()
            model_map = await sync_to_async(_local_models)()

            # [배달=Guide 구현체] 로컬도 SnsGuide가 같은 계약을 ORM으로 구현 — 여기선 호출만(모드분기 제거)
            async def fetch_pending(seen):
                return [p for p in await guide.get_pending() if p["msg_id"] not in seen]

            async def mark_pick(mid, done=False, touch=False, unpick=False, idle=None):
                return await guide.pick(mid, done=done, touch=touch, unpick=unpick, idle=idle)

            async def _beat():
                await guide.heartbeat("local")

            async def _check_stop(ch):
                return await guide.check_stop(ch)

            async def fetch_all_stops():
                return await guide.all_stops()

            async def mark_channel_stopped(ch):
                await guide.mark_stopped(ch)

            async def _check_interject(ch):
                return await guide.check_interject(ch)
            where = "로컬 ORM"

        if not bot_info:
            self.stderr.write("로스터가 비어 있습니다 — 봇을 먼저 채용하세요(스튜디오). 종료.")
            return

        # per-agent 모델 — 빌더 팩토리에 model_map을 넘겨 봇별 LLM 주입(빈 맵이면 디스코드 경로와 동일).
        sysm = Sys(
            guide, SNS_GUILD_ID, _make_builder(cfg, audit, bot_info, model_map, persona_map), bot_info=bot_info,
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
        cut_resumes = {}                                 # msg_id → 백스톱 컷 후 재개 횟수(무한 루프 방지 상한)
        last_beat = 0.0
        inflight = {}                                    # msg_id → {"task":Task, "ch":int} — 동시 진행 흐름
        try:
            cap = max(1, int(os.environ.get("ORGANT_MAX_FLOWS", "4")))
        except ValueError:
            cap = 4
        try:
            max_age = max(60, int(os.environ.get("ORGANT_FLOW_MAX_AGE", "7200")))  # 절대 백스톱(초) — 긴 빌드 보호(2h 기본).
        except ValueError:                                                        # 컷돼도 '체크포인트 후 재개'라 치명적 아님.
            max_age = 7200
        try:
            # 무진행(조용함) 상한(초) — 봇 활동(last_activity)이 이만큼 멈추면 '먹통'으로 보고 취소.
            # 나이가 아니라 '실제 정체'로 자른다(잘 도는 긴 빌드는 절대 안 끊음). 워커 턴 8분·리더 워치독
            # 12분보다 길게 잡아 정상 턴 오살 방지 — 기본 900초(15분). 진짜 멈춤만 회수.
            stall_timeout = max(120, int(os.environ.get("ORGANT_FLOW_STALL", "900")))
        except ValueError:
            stall_timeout = 900
        # [동시 처리] 두뇌(Sys)는 active_flows·engaged로 이미 병렬을 지원하는데, 종전 러너는 흐름을
        # 하나씩 await해 직렬화했다 — 무관한 다른 채널·다른 봇 요청까지 큐에 막혔다(라이브 관측).
        # 이제 빈 봇의 요청은 동시 task로 띄우고(점유 충돌은 engaged.holder로 사전 차단), 같은 봇/채널만
        # 자연 직렬화한다. 출력 라우팅은 contextvar(ORIGIN_CHANNEL)로 흐름별 격리(공유속성 레이스 제거).
        # [로그 가시성] SYS.run이 진행상황을 log.info로 남긴다(구 self.stdout 대체) — 진입(러너)이 그걸
        # journal(stdout)로 흘리도록 핸들러를 붙인다. 진입=앱이 로깅을 설정한다(SYS는 계약만).
        import logging as _lg
        _sl = _lg.getLogger("organt.sys"); _sl.setLevel(_lg.INFO)
        if not _sl.handlers:
            _h = _lg.StreamHandler(); _h.setFormatter(_lg.Formatter("%(asctime)s %(message)s"))
            _sl.addHandler(_h); _sl.propagate = False
        # [매체 무관 실행] 루프는 이제 SYS.run이 소유 — 러너는 guide·builder 조립 후 이것만 호출(진입 얇아짐)
        await sysm.run(guide, leader, cap=cap, poll=opts["poll"],
                       stall_timeout=stall_timeout, max_age=max_age, once=opts["once"])

    async def _remote_roster(self, guide):
        """원격 /api/agents/ → bot_info({bot_id: role}), 리더, model_map, persona_map({bot_id: persona})."""
        data = await guide._get("/api/agents/")
        rows = data if isinstance(data, list) else data.get("results", [])
        bot_info, leader, model_map, persona_map = {}, None, {}, {}
        best = -1
        for a in rows:
            bid = int(a["bot_id"])
            bot_info[bid] = a.get("role") or "예비"
            if (a.get("model") or "").strip():
                model_map[bid] = a["model"].strip()
            if (a.get("persona") or "").strip():
                persona_map[bid] = a["persona"]
            if a.get("is_leader"):
                leader = bid
            if (a.get("event_count") or 0) > best:
                best, _fallback = a.get("event_count") or 0, bid
        if leader is None and rows:
            leader = int(rows[0]["bot_id"])
        return bot_info, leader, model_map, persona_map
