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
from sns.http_sns_guide import HttpSnsGuide, ORIGIN_CHANNEL   # noqa: E402
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
    out = []
    for m in GuideMessage.objects.filter(msg_type="request", sender_id=0).order_by("msg_id"):
        if m.msg_id in seen or (m.payload or {}).get("picked"):
            continue
        out.append({"msg_id": m.msg_id, "channel_id": m.channel_id, "to_id": m.to_id,
                    "kind": m.kind, "body": m.body, "route_to": _route_to(m.channel_id)})
    return out


def _local_pick(msg_id, done=False, touch=False):
    m = GuideMessage.objects.filter(msg_id=msg_id).first()
    if not m:
        return
    p = dict(m.payload or {})
    p["picked"] = True
    if done:
        p["done_ts"] = time.time()
    elif touch:
        p["picked_ts"] = time.time()             # 진행 갱신 — 긴 흐름이 '멎음'으로 오판되지 않게
    else:
        p.setdefault("picked_ts", time.time())   # 멎은 요청 판정(픽 후 무응답 경과)용
    GuideMessage.objects.filter(msg_id=msg_id).update(payload=p)


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

            async def mark_pick(mid, done=False, touch=False):
                await guide._post("/api/guide/pick/", {"msg_id": mid, "done": done, "touch": touch})

            async def _beat():
                await guide._post("/api/guide/heartbeat/", {"note": "remote"})

            async def _check_stop(ch):
                d = await guide._get(f"/api/guide/stops/?channel={ch}")
                return bool(d.get("stopped"))

            async def fetch_all_stops():
                d = await guide._get("/api/guide/stops/")          # 전체 중지신호 반환+소거(전역 스캔)
                return [int(c) for c in d.get("channels", [])]

            async def mark_channel_stopped(ch):
                await guide._post("/api/guide/stop_channel/", {"channel": int(ch)})

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

            async def mark_pick(mid, done=False, touch=False):
                await sync_to_async(_local_pick)(mid, done, touch)

            async def _beat():
                from sns.models import EngineHeartbeat
                await sync_to_async(EngineHeartbeat.beat)("local")

            async def _check_stop(ch):
                return await sync_to_async(_local_stop_pending)(ch)

            async def fetch_all_stops():
                return await sync_to_async(_local_all_stops)()

            async def mark_channel_stopped(ch):
                await sync_to_async(_local_stop_channel)(ch)

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
        inflight = {}                                    # msg_id → {"task":Task, "ch":int} — 동시 진행 흐름
        try:
            cap = max(1, int(os.environ.get("ORGANT_MAX_FLOWS", "4")))
        except ValueError:
            cap = 4
        try:
            max_age = max(60, int(os.environ.get("ORGANT_FLOW_MAX_AGE", "1500")))  # 절대 상한(초) — 병적 케이스 백스톱
        except ValueError:
            max_age = 1500
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
        self.stdout.write(f"요청 폴링 시작(동시 처리 — 상한 {cap}) — 빈 봇이면 여러 흐름이 함께 돕니다. (Ctrl+C 종료)")
        while True:
            try:
                _now = asyncio.get_event_loop().time()
                # ── 엔진 생존(heartbeat) + 진행(picked_ts) 갱신 — 모든 진행 중 요청(8초 throttle) ──
                if _now - last_beat > 8:
                    try:
                        await _beat()
                        for _mid in list(inflight):
                            await mark_pick(_mid, touch=True)
                    except Exception:
                        pass
                    last_beat = _now
                # ── 완료된 흐름 reap + 먹통(최대수명 초과) 흐름 취소로 슬롯·봇 점유 회수(다수 접속 데모 잠금 방지) ──
                for _mid, _info in list(inflight.items()):
                    if _info["task"].done():
                        del inflight[_mid]
                        try:
                            _info["task"].result()       # 예외 표출
                            await mark_pick(_mid, done=True)
                            self.stdout.write(f"✓ 처리 완료: msg_id={_mid}")
                        except asyncio.CancelledError:
                            self.stdout.write(f"■ 중지됨: msg_id={_mid}")   # 취소된 요청은 picked-미완→멎음으로 떠 재큐 가능
                        except Exception as _e:
                            import traceback
                            self.stderr.write(f"✗ 처리 실패 msg_id={_mid}: {_e}\n{traceback.format_exc()}")
                    else:
                        # [정체 기준 회수] '나이'가 아니라 '실제 무진행'으로 자른다 — 잘 도는 긴 빌드(계획→구현
                        # →검증→배포는 10분+ 정상)는 절대 안 끊고, 봇 활동(last_activity)이 멈춘 먹통만 회수한다.
                        # last_activity는 도구활동·턴시작마다 갱신되므로, 멈춰 있으면 = 진짜 정체(리더 워치독이
                        # 못 잡는 리더-턴 이후 정체 포함). 채널→흐름 조회는 request_cancel과 같은 user_channel 매칭.
                        idle = _flow_idle(sysm, _info["ch"])
                        stalled = idle is not None and idle > stall_timeout
                        too_old = _now - _info.get("t0", _now) > max_age      # 절대 백스톱(병적 케이스)
                        if stalled or too_old:
                            try:
                                sysm.request_cancel(_info["ch"])
                                _info["task"].cancel()
                                _why = f"무진행 {int(idle)}s" if stalled else f"최대수명 {max_age}s"
                                self.stdout.write(f"⏱ {_why} 초과 — 흐름 취소(슬롯 회수): msg={_mid} ch={_info['ch']}")
                            except Exception:
                                pass
                # ── 작업 중지: 매 폴 '모든' 미처리 중지신호를 확인(inflight 아닌 채널 포함) ──
                # [신뢰성] 종전엔 *도는 채널만* 중지를 봤다 → 흐름이 잠깐 안 도는 사이 누른 중지가 유실됐다
                # (라이브: 사용자 중지가 cancel 0건으로 통째 무시됨). 이제 전역 스캔으로 ① 도는 흐름은 즉시 취소,
                # ② 안 도는 채널은 픽 요청을 '중지됨'으로 종결(작업중 해제·재픽/재개 차단) — 중지가 항상 먹는다.
                try:
                    for _sch in await fetch_all_stops():
                        cancelled = sysm.request_cancel(_sch)        # 도는 중이면 흐름 취소(아니면 False)
                        await mark_channel_stopped(_sch)             # 픽 요청 '중지됨' 종결(화면·재픽 정정)
                        self.stdout.write(f"■ 작업 중지 — ch={_sch}(흐름취소={cancelled})")
                except Exception:
                    pass
                # ── 진행 중 흐름들의 개입 폴(채널별) ──
                for _mid, _info in list(inflight.items()):
                    _ch = _info["ch"]
                    try:
                        for _x in await _check_interject(_ch):
                            ok = sysm.deliver_human_info(_ch, _x.get("target_id"), _x.get("text"))
                            self.stdout.write(f"✎ 사람 개입 {'주입' if ok else '미주입(흐름없음)'} — ch={_ch}")
                    except Exception:
                        pass
                # ── 빈 봇 요청 픽 → 동시 흐름 띄우기(상한까지) ──
                pend = await fetch_pending(seen)
                busy_ch = {_i["ch"] for _i in inflight.values()}
                busy_lead = set()                        # 이번 폴에 막 띄운 리더(아직 engage 전 보호)
                for m in pend:
                    if len(inflight) >= cap:
                        break
                    mid = m["msg_id"]
                    # [A] 봇 미지정이면: 그 채널의 담당(route_to=지정 리더/최근 활동 봇) → 없을 때만 전역 리더.
                    # 종전엔 무조건 전역 리더(고은호)로 가 음식 추천이 '게임 기획자'에게 가던 문제.
                    to_id = int(m["to_id"]) if m["to_id"] else (int(m["route_to"]) if m.get("route_to") else leader)
                    ch = int(m["channel_id"])
                    # 같은 채널이 진행 중이거나 대상 봇이 타 흐름 점유 중이면 큐에 남김(두뇌가 직렬화) — seen 미추가로 다음 폴 재검토.
                    # ('진행 중 흐름에 개입으로 즉시 전달'은 단일턴 흐름에선 다음 턴이 없어 유실되므로 안 함 — 큐가 안전하게 각각 응답.)
                    if ch in busy_ch or to_id in busy_lead or sysm.engaged.holder(to_id) is not None:
                        continue
                    seen.add(mid)
                    kind = Kind.WORK if (m["kind"] or "W") == "W" else Kind.INFO
                    req = Request(to_id=to_id, kind=kind, body=m["body"], from_id=0, message_id=str(mid))
                    try:
                        await _check_stop(ch)            # stale stop 소거(이전 흐름 잔여 신호)
                    except Exception:
                        pass
                    await mark_pick(mid)
                    ORIGIN_CHANNEL.set(ch)               # task-로컬 라우팅(동시 안전 — create_task가 context 복사)
                    inflight[mid] = {"task": asyncio.create_task(sysm.route_channel_request(ch, req)), "ch": ch, "t0": _now}
                    busy_ch.add(ch); busy_lead.add(to_id)
                    self.stdout.write(f"▶ 요청 처리(동시 {len(inflight)}/{cap}): ch={ch} to={to_id} kind={m['kind']} body={m['body'][:42]!r}")
                if opts["once"] and not inflight and not pend:
                    self.stdout.write("[--once] 대기·진행 요청 없음 — 종료.")
                    return
                await asyncio.sleep(2)
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
