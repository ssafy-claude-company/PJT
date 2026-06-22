"""SYS — Organt 주도 + P2P Communication.

User 입력 → SYS가 담당(리더)을 깨움 → Organt가 판단·행동(파일/Guide 도구).
필요하면 어떤 Organt든 `request`로 동료를 부르고, SYS가 그 동료를 중첩 베턴으로
깨워(run_turn) 응답을 돌려준다. 항상 1명만 활성(단일흐름) → 사이드이펙트·토큰 절약.

SYS는 얇다: 깨우기(wake) 제공 + 단일흐름 lock + 라우팅. 베턴/권한 강제는 Rule·Hook.
Organt 생성(모델·권한·State)은 organt_builder로 주입받는다.
"""
import asyncio
import glob
import json
import os
import re
import time
from typing import Dict, Optional

from .communication import CommError, Engagement
from .guide_tools import Flow, TaskRef, build_guide_server, make_guide_tools
from .protocol import Kind, Request, TaskStatus, format_response

# 턴 한도로 작업이 끊겼을 때 같은 세션으로 이어가게 하는 지시(구조적 연속 실행).
_CONTINUE_BODY = (
    "[이어서 계속 — 처음부터 다시 하지 말 것] 직전 턴이 작업 도중 '턴 한도'로 끊겼습니다. "
    "진행 중이던 Task가 아직 열려 있을 수 있습니다. 현재 작업공간 상태를 Read/run으로 먼저 확인한 뒤, "
    "이미 한 부분은 건너뛰고 남은 부분만 마저 진행해 그 Task를 complete_task로 마감하세요. "
    "마감(또는 명시적 완료)까지가 목표입니다.\n"
    "[중요 — 동료는 '비동기로' 일하지 않습니다] 동료는 당신이 request로 깨운 그 호출 동안만 일하고, "
    "호출이 끝나면 멈춥니다. 백그라운드 작업·'시차를 두고 도착하는 파일' 같은 것은 없습니다 — "
    "ls/wc로 파일 도착을 기다리며 폴링하는 것은 아무것도 진행시키지 않는 낭비입니다(이 재호출 기회만 "
    "소모). 산출물이 미완이면 지금 즉시 그 owner에게 request(Work)로 '이어서 남은 부분(예: 빠진 "
    "파일)을 마저 끝내라'고 재위임하세요 — 그래야만 작업이 계속됩니다."
)



class Sys:
    def __init__(self, guide, guild_id, organt_builder, bot_info: Optional[Dict[int, str]] = None,
                 workspace=None, projects_path=None, session_dir=None, max_continue=6,
                 jobs_path=None, seed_path=None):
        self.guide = guide
        self.guild_id = guild_id
        self.organt_builder = organt_builder   # (organt_id, guide_server, role) -> Organt
        self.bot_info = bot_info or {}
        # 로스터 원본 라벨(직군). recruit(role=…)로 '예비'를 런타임 직군으로 채용하면 bot_info가 바뀌므로,
        # 새 흐름 시작 때 이걸로 원복한다(예비는 다음 흐름에서 다른 직군으로 다시 채용 가능).
        self._roster_labels = dict(self.bot_info)
        # '직업 기억' 디스크 영속: recruit한 직군(예: 게임 기획자)을 jobs.json에 저장해, 프로세스 재시작
        # 뒤에도 '예비'로 원복되지 않게 한다(매번 다른 봇이 그 직군으로 뽑히던 문제의 근본 해결; 1봇 1직군).
        # Discord 역할(권한)도 또 다른 영속 진실원이라, main이 시작 때 역할에서 복원해 bot_info에 미리 반영한다.
        self.jobs_path = jobs_path
        self._load_jobs()
        self._origin_request = ""   # 이번 흐름의 '사용자 원문 요청'(담당자 paraphrase 아닌 원문) — 모든 프롬프트에 주입
        self.workspace = workspace             # run 툴 cwd(작업공간 경로)
        self.session_dir = session_dir         # organt_state_*.json 위치(새 요청마다 세션 초기화)
        # 턴 한도로 미완 시 같은 세션으로 이어가는 최대 횟수(ORGANT_MAX_CONTINUE로 운영 조정 가능).
        self.max_continue = int(os.environ.get("ORGANT_MAX_CONTINUE", max_continue))
        # 워커 턴 '침묵' 타임아웃(초): 도구 활동(last_activity)이 이 시간 동안 '한 번도' 갱신되지 않으면
        # (=진짜 행) 포기하고 '인프라 실패'로 반환한다. 벽시계 총 실행시간이 아니라 '무활동' 기준이라,
        # 오래 걸려도 일하는 워커는 안 자르고 완전히 멈춘 것만 끊는다(일하는 owner 절단·좀비의 근본 교정).
        self.turn_timeout = int(os.environ.get("ORGANT_TURN_TIMEOUT", "480"))   # 기본 8분(무활동 기준)
        # 흐름 '무진행(행)' 워치독: 요청·파일작성·실행 등 어떤 진행도 이 시간(초) 동안 없으면 흐름이 행으로
        # 멈춘 것(리더 서브프로세스 행 포함 — 리더 턴엔 타임아웃이 없어 생기는 구멍)으로 보고 자동 중단·보고한다.
        # 워커 타임아웃(turn_timeout=8분)보다 넉넉히 커야 워커 1회 행→복구를 '무진행'으로 오인하지 않는다.
        self.idle_timeout = int(os.environ.get("ORGANT_IDLE_TIMEOUT", "720"))   # 기본 12분(>8분 워커 타임아웃)
        # [병렬 작업(Feat 4단계)] 흐름 '안'의 단일활성(베턴)은 불변 — 완화는 '서로 다른 프로젝트'의
        # 흐름 동시 진행만. 같은 스코프(프로젝트/신규)는 직렬 큐. 흐름 간 안전은 임의 숫자 상한이
        # 아니라 **전역 점유 장부(Engagement)** 가 보장한다: 한 봇은 한 시점에 한 흐름에만 참여
        # (리더 포함 — 같은 리더의 프로젝트들은 자연히 직렬). 동시 작업량의 자연 한도 = 직원 수.
        # ORGANT_MAX_FLOWS는 토큰 동시 사용을 묶고 싶을 때만 쓰는 운영 노브(기본 0=무제한,
        # 1=종전과 동일한 완전 직렬).
        self.active_flows: Dict[str, Flow] = {}   # scope(P-XXX|main) → 진행 중 Flow
        self.max_flows = int(os.environ.get("ORGANT_MAX_FLOWS", "0"))
        self.engaged = Engagement(is_live=self._scope_live)   # 봇 단위 전역 점유(흐름 간 배타성)
        self.queue = []                        # 진행 중 들어온 명령(순차 처리 대기)
        self.flow_log = []
        self.flow_log_path = (os.path.join(session_dir, "flow.jsonl") if session_dir else None)
        self.projects_path = projects_path     # 레지스트리 영속 경로(없으면 인메모리)
        self.seed_path = seed_path             # 커밋된 시드(리클레임으로 디스크 유실 시 폴백)
        self.projects: Dict[int, dict] = {}    # channel_id → 프로젝트 컨텍스트(개입 진입점)
        # 직군별 '직무 기준'(craft profile): {직군: 기준 텍스트}. 시스템이 정답을 정하지 않는다 —
        # 각 직군의 전문가(그 봇)가 첫 작업 때 스스로 작성하고(보고의 [직무기준] 블록을 SYS가 흡수),
        # Discord(sys-roles)에 영속돼 이후 모든 작업 프롬프트에 자기검수 기준으로 주입된다.
        # QA·백엔드·프론트·런타임 채용 직군 모두 같은 메커니즘 하나로 '각자의 일'이 고도화된다.
        self.role_profiles: Dict[str, str] = {}
        self.role_experience: Dict[str, list] = {}   # 직군별 '일하며 쌓인 경험' 최근 요점(Skill 강화 v1)
        self.profiles_path = (os.path.join(session_dir, "role_profiles.json") if session_dir else None)
        self._load_profiles()
        self._proj_n = 0
        self._load_projects()

    @property
    def active_flow(self):
        """호환: 진행 중 흐름이 하나라도 있으면 그 중 하나(없으면 None) — 수면 사이클 등이 사용."""
        for f in self.active_flows.values():
            if not f.done:
                return f
        return None

    def _scope_live(self, scope) -> bool:
        """점유 장부의 유령 자가 치유용: 그 스코프의 흐름이 아직 살아 있는가.
        '__distill__'(수면 증류)은 흐름이 아닌 짧은 점유라 항상 살아있다고 본다(finally에서 해제)."""
        if scope == "__distill__":
            return True
        f = self.active_flows.get(str(scope))
        return f is not None and not f.done

    def _load_projects(self):
        """디스크에서 프로젝트 레지스트리 복원 — 프로세스가 끝나도 '원래 작업'에 개입 가능.
        디스크(logs/)가 없으면(컨테이너 리클레임으로 유실) 커밋된 시드에서 복원하되 'seeded' 마커를
        남긴다 — 시드는 커밋 시점에 멈춘 과거라, 부팅 reconcile에서 Discord 채널 토픽(런타임마다
        갱신되는 영속 진실원)이 있으면 그쪽이 이긴다(리더 재지정·워크스페이스가 시드로 원복되던 한계 해소)."""
        path, seeded = self.projects_path, False
        if not path or not os.path.exists(path):
            if self.seed_path and os.path.exists(self.seed_path):
                path, seeded = self.seed_path, True
            else:
                return
        try:
            data = json.load(open(path, encoding="utf-8"))
            self.projects = {int(k): v for k, v in data.get("projects", {}).items()}
            self._proj_n = data.get("n", len(self.projects))
            if seeded:
                for p in self.projects.values():
                    p["seeded"] = True
                self._save_projects()   # logs에 물질화(마커 포함 — reconcile이 보고 토픽 우선 적용)
                self._log("projects_seed_restored", n=len(self.projects))
        except Exception:
            pass

    def _save_projects(self):
        if not self.projects_path:
            return
        try:
            data = {"n": self._proj_n,
                    "projects": {str(k): v for k, v in self.projects.items()}}
            # 원자적 저장: 임시파일에 다 쓰고 flush+fsync 후 교체 → 쓰는 도중 프로세스가 죽어도
            # 원본 projects.json이 '반쪽(깨진 JSON)'으로 남지 않는다(개입 레지스트리 유실 방지).
            tmp = f"{self.projects_path}.tmp-{time.monotonic_ns()}"   # 병렬 흐름 동시 저장 경합 방지
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.projects_path)
        except Exception:
            pass

    def _load_jobs(self):
        """디스크(jobs.json)에 영속된 '직업 기억'(예비→채용 직군)을 roster 라벨·현재 라벨에 덮어쓴다 —
        프로세스 재시작 뒤에도 채용했던 직군(예: 게임 기획자)이 '예비'로 원복되지 않게(1봇 1직군 유지)."""
        if not self.jobs_path or not os.path.exists(self.jobs_path):
            return
        try:
            data = json.load(open(self.jobs_path, encoding="utf-8"))
            for k, v in (data.get("jobs") or {}).items():
                kid = int(k)
                self._roster_labels[kid] = v
                if kid in self.bot_info:
                    self.bot_info[kid] = v
        except Exception:
            pass

    def _save_jobs(self):
        """현재 '직업 기억'(예비가 아닌 라벨)을 jobs.json에 원자적 저장 — 재시작 넘어 직군 유지."""
        if not self.jobs_path:
            return
        try:
            jobs = {str(k): v for k, v in self._roster_labels.items()
                    if v and not str(v).startswith("예비")}
            tmp = f"{self.jobs_path}.tmp-{time.monotonic_ns()}"   # 병렬 흐름 동시 저장 경합 방지
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"jobs": jobs}, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.jobs_path)
        except Exception:
            pass

    def _persist_job(self, mid, role):
        """recruit가 예비를 직군으로 채용/자기직군 확정할 때 호출 — 메모리(_roster_labels)+디스크(jobs.json) 갱신."""
        self._roster_labels[int(mid)] = role
        self._save_jobs()

    def _register_project(self, channel_id, name, workspace, leader, purpose="",
                          origin_msg="", reuse_ok=None) -> str:
        """프로젝트를 1급 엔티티로 등록 → 식별번호 P-XXX 부여. 같은 채널이나 같은 이름이 이미
        있으면 재사용(중복 방지). 등록 채널에 다시 명령이 오면 '개입'으로 라우팅된다.
        purpose = 프로젝트를 탄생시킨 **사용자 원문**(docs Project.md의 '방향성') — 개입마다
        주입돼, 마지막 미완 Task만 보고 마감하는 시야 협착을 막는다(라이브 관측: '이어서 진행해'가
        아트 Task 하나만 닫고 멀티·배포가 남은 프로젝트를 끝났다고 보고).
        origin_msg = 프로젝트를 탄생시킨 원요청의 메시지 ID — 부팅 복구가 '이미 프로젝트로 졸업한
        원요청'을 새 흐름으로 재발사하지 않고 그 프로젝트 채널 '개입'으로 잇는 연결 고리(라이브:
        동면 복구가 P-009 원요청을 재발사해 진행을 버리고 처음부터 새로 시작 — 사용자 지적)."""
        ch = int(channel_id)
        if ch in self.projects:
            p = self.projects[ch]
            changed = False
            if purpose and not p.get("purpose"):
                p["purpose"] = purpose[:700]
                changed = True
            if origin_msg and not p.get("origin_msg"):
                p["origin_msg"] = str(origin_msg)
                changed = True
            if changed:
                self._save_projects()
            return p["id"]
        # 같은 이름이 이미 있으면 식별번호를 '그대로 유지'하고 채널만 현재 것으로 이동(증가/중복 금지)
        for c, p in list(self.projects.items()):
            if p.get("name") == name:
                # [신규가 기본 — 주소 지정의 이치(사용자 사건 2026-06-12)] 메인 채널의 새 요청이
                # 기존 작품을 이어가는 길은 둘뿐: 그 프로젝트 채널 개입, 또는 원문에 P-번호 명시.
                # 단어가 유사한 '다른 작품'("2인 협동…디펜스")이 유사 안내+같은 이름 작명으로 기존
                # P-009의 신원·작업공간·채널을 통째로 가져가던 사고 차단 — 이름이 같아도 신설(고유화).
                if reuse_ok is not None and p.get("id") not in reuse_ok:
                    name = f"{name}-{self._proj_n + 1}"
                    self._log("project_reuse_denied_new_request", existing=p.get("id"), made=name)
                    break
                # [신원 가드 — 이름은 라벨이지 신원이 아니다] 일반명사 이름("public-data-website")이
                # 우연히 일치하면 다른 작품이 기존 프로젝트의 채널·작업공간·배포 슬롯을 통째로
                # 차지했다(라이브: 지진 사이트가 대기질 P-006을 하이재킹). 재사용은 '진짜 같은
                # 작품'(목표 원문 유사)일 때만 — 다르면 이름을 자동 고유화해 신규 등록한다.
                if purpose and p.get("purpose") and not self._same_purpose(purpose, p["purpose"]):
                    name = f"{name}-{self._proj_n + 1}"   # 라벨 충돌 해소(신원 분리)
                    self._log("project_name_uniquified", asked=p.get("name"), made=name,
                              existing=p.get("id"))
                    break
                # [채널 하이재킹 가드] 미완 Task가 영속된 '진행 중' 프로젝트의 채널은 옮기지 않는다 —
                # 같은 작품을 다른 채널에서 다시 등록하는 흐름(라이브: 동면 복구 재발사가 새 채널을 파고
                # create_project)이 원래 작업 채널에서 신원·토픽·open_task를 떼어가 '기존 채널이 죽고
                # 새 채널에서 처음부터'가 되던 사고 차단. 신원은 돌려주되(같은 작품 인지) 채널·미완
                # Task는 원래 자리를 지킨다 — 이어가기는 그 채널 개입으로.
                if c != ch and p.get("open_task"):
                    self._log("project_channel_move_refused", project=p.get("id"),
                              kept=c, asked=ch)
                    return p["id"]
                # [연장 = 기존 산출물 위에서] 재사용은 작업공간을 새 흐름의 임시 폴더로 덮지 않는다 —
                # 이어가기의 본질은 '그 작품의 폴더'를 계속 쓰는 것(덮으면 산출물 연속성이 끊긴다).
                p["channel"] = ch
                if purpose and not p.get("purpose"):
                    p["purpose"] = purpose[:700]
                if origin_msg and not p.get("origin_msg"):
                    p["origin_msg"] = str(origin_msg)
                self.projects[ch] = p
                if c != ch:
                    del self.projects[c]
                    self._clear_topic(c)   # 옛 채널의 스테일 토픽 제거(부팅 reconcile 때 유령 등록 방지)
                self._save_projects()
                self._sync_topic(ch)
                return p["id"]
        self._proj_n += 1
        pid = f"P-{self._proj_n:03d}"
        workspace = self._idify_workspace(workspace, pid, name)   # 신원=번호: p-00n-슬러그 개명
        self.projects[ch] = {"id": pid, "name": name, "channel": ch,
                             "workspace": workspace, "leader": leader, "summary": "",
                             "purpose": purpose[:700], "origin_msg": str(origin_msg or "")}
        self._save_projects()
        self._sync_topic(ch)
        return pid

    # --- 레지스트리의 Discord 영속(채널 토픽) — logs/는 리클레임으로 사라지므로, 직군을 Discord '역할'에
    # 영속하듯 등록 정보(식별번호·리더·워크스페이스·이름)를 그 프로젝트 '채널 토픽'에 영속한다.
    # 우선순위: 런타임 디스크 > 채널 토픽 > 커밋 시드. ---

    _TOPIC_RE = re.compile(r"^\[ORGANT:(P-\d+)\]\s+leader=(\d+)\s+\|\s+ws=(.*?)\s+\|\s+name=(.*)$", re.S)

    @staticmethod
    def _topic_for(p) -> str:
        return (f"[ORGANT:{p['id']}] leader={int(p.get('leader') or 0)} "
                f"| ws={p.get('workspace') or ''} | name={p.get('name') or ''}")[:1024]

    @classmethod
    def parse_project_topic(cls, topic) -> Optional[dict]:
        m = cls._TOPIC_RE.match((topic or "").strip())
        if not m:
            return None
        return {"id": m.group(1), "leader": int(m.group(2)),
                "workspace": m.group(3).strip() or None, "name": m.group(4).strip()}

    def _spawn_topic_write(self, channel_id, topic: str):
        if not hasattr(self.guide, "set_channel_topic"):
            return

        async def _write():
            try:
                r = await self.guide.set_channel_topic(int(channel_id), topic)
            except Exception:
                return
            if r is None:   # 404 — 채널 죽음. 프로젝트 *기록은 유지*하되 'channel_dead' 표시만 →
                # 다음 부팅부터 reconcile이 이 채널 토픽쓰기를 건너뛴다(죽은 채널 churn 제거, 부팅 stall 차단).
                p = self.projects.get(int(channel_id))
                if p is not None and not p.get("channel_dead"):
                    p["channel_dead"] = True
                    self._save_projects()
                    self._log("channel_marked_dead", channel=int(channel_id), project=p.get("id"))
        try:
            asyncio.get_running_loop().create_task(_write())
        except RuntimeError:    # 이벤트 루프 밖(동기 테스트 등) — best-effort라 건너뜀
            pass

    def _sync_topic(self, channel_id):
        """등록/리더 재지정 때 레지스트리 요지를 채널 토픽에 기록(best-effort, 비동기)."""
        p = self.projects.get(int(channel_id))
        if p:
            self._spawn_topic_write(channel_id, self._topic_for(p))

    def _clear_topic(self, channel_id):
        self._spawn_topic_write(channel_id, "")

    async def reconcile_projects_from_discord(self):
        """부팅 시 Discord 채널 토픽으로 레지스트리를 보강한다(리클레임 내구성의 마지막 조각).
        - 레지스트리에 없는 토픽 프로젝트(시드 이후 생겼거나 시드에도 없던 것): 토픽에서 등록 복원.
        - 시드로 복원된 항목(seeded 마커): 토픽이 더 최신이므로 leader/workspace/name을 토픽으로 갱신
          (리더 재지정이 시드로 원복되던 한계 해소). 런타임 디스크 항목은 그대로(디스크가 진실원).
        끝나면 마커를 지우고, 토픽이 없거나 깨진 등록 채널엔 토픽을 다시 채워 자가치유한다."""
        if not hasattr(self.guide, "get_channel_topics") or not self.guild_id:
            return
        try:
            topics = await self.guide.get_channel_topics(self.guild_id) or {}
        except Exception:
            topics = {}
        changed = False
        for ch, topic in topics.items():
            info = self.parse_project_topic(topic)
            if not info:
                continue
            ch, cur = int(ch), self.projects.get(int(ch))
            if cur is None:
                # 같은 식별번호가 다른 채널에 이미 살아 있으면(채널 이동 후 남은 스테일 토픽) 유령 등록 금지
                if any(p.get("id") == info["id"] for p in self.projects.values()):
                    continue
                self.projects[ch] = {"id": info["id"], "name": info["name"], "channel": ch,
                                     "workspace": info["workspace"], "leader": info["leader"],
                                     "summary": ""}
                changed = True
                self._log("project_restored_from_topic", project=info["id"], channel=ch)
            elif cur.pop("seeded", None):
                changed = True
                if (cur.get("leader") != info["leader"] or cur.get("name") != info["name"]
                        or (info["workspace"] and cur.get("workspace") != info["workspace"])):
                    cur["leader"], cur["name"] = info["leader"], info["name"]
                    if info["workspace"]:
                        cur["workspace"] = info["workspace"]
                    self._log("project_updated_from_topic", project=cur["id"], channel=ch)
            try:
                self._proj_n = max(self._proj_n, int(info["id"].split("-")[1]))
            except (IndexError, ValueError):
                pass
        for ch, p in self.projects.items():
            if p.pop("seeded", None):    # 토픽이 없던 시드 항목 — 시드 값이 최선, 마커만 제거
                changed = True
            if not p.get("channel_dead") and self.parse_project_topic(topics.get(int(ch), "")) is None:
                self._sync_topic(ch)     # 자가치유: 등록돼 있는데 토픽이 없으면/깨졌으면 다시 기록(죽은 채널은 스킵)
        if changed:
            self._save_projects()

    def _stage_inbound(self, flow) -> None:
        """[파일 전송 — 인바운드] 사용자가 첨부한 파일을 작업공간 inbox/로 옮긴다(워크스페이스가 준비됐을 때 1회)
        — 봇이 Read/run으로 사용하게. create_project가 워크스페이스를 만든 직후 + 매 턴 시작에 호출(멱등)."""
        atts = getattr(flow, "inbound_attachments", None)
        ws = getattr(flow, "workspace", None)
        if not atts or not ws:
            return
        inbox = os.path.join(str(ws), "inbox")
        try:
            os.makedirs(inbox, exist_ok=True)
            names = []
            for item in atts:
                try:
                    name, data = item
                    safe = os.path.basename(str(name)) or "file"
                    with open(os.path.join(inbox, safe), "wb") as fh:
                        fh.write(data)
                    names.append(safe)
                except Exception:
                    continue
            flow.inbound_files = list(getattr(flow, "inbound_files", []) or []) + names
            flow.inbound_attachments = []   # 1회만(중복 staging 방지)
            self._log("inbound_files_staged", files=names)
        except Exception as e:
            self._log("inbound_stage_error", err=str(e)[:100])

    def _task_snapshot(self, flow, ref) -> dict:
        """미완 Task를 다음 개입에서 '되살릴' 수 있도록 최소 스냅샷으로 직렬화한다(상태블록·스레드·담당자·
        팀·목표). 검증 누계(verified/run_count/owner_delivered 등)는 저장하지 않는다 — 되살릴 때 0에서
        다시 시작해 '완료'엔 새 run 증거를 다시 요구하기 위함(되살린 직후 허위완료 방지)."""
        return {
            "task_id": ref.task_id,
            "thread_id": ref.thread_id,
            "block_id": ref.block_id,
            "purpose": ref.status.purpose or "",
            "goal": ref.status.goal or "",
            "owner": int(ref.owner or 0),
            "owner_name": ref.status.owner or "",
            "team": [int(x) for x in ref.team],
            "result_so_far": (ref.status.result or "")[:500],
            "collab_notes": getattr(ref, "collab_notes", ""),   # 회의·표결 합의 — 재개 위임에도 동봉
            "acceptance": getattr(ref, "acceptance", ""),        # 수용 계약 — 동면·재개 너머 영속(없으면 마감 게이트가 매번 재정의 요구)
            "standard": getattr(ref, "standard", ""),            # [최대화] 최대 품질 표준(도메인 누적) — 동면 너머 영속(없으면 바가 증발, 라이브 확인)
            "interfaces": getattr(ref, "interfaces", ""),        # [협업] 도메인 간 인터페이스 계약 — 재개 마감 L2 검증이 같은 계약으로
            # [협의 명단 영속] 이게 없으면 재개마다 set_goal 게이트가 전원 재협의를 강제 —
            # 라이브: 동면 5회 흐름에서 리더가 같은 협의 질문을 5회 반복(시간·토큰 낭비의 주범).
            # 협의는 '사실'이라 영속이 옳다(검증 누계와 다름 — 그건 의도적으로 0에서 재시작).
            "participated": sorted(int(x) for x in getattr(ref, "participated", []) or []),
            "last_work_body": getattr(ref, "last_work_body", ""),  # [정밀 복구] owner 위임 원문 — 복구가 재작문 대신 replay
            # [정밀 복구 — 전체 체인] 열린 베턴 프레임 전부(원문 포함)를 영속한다. 끊김 시 owner(레벨1)만이 아니라
            # *가장 깊은 활성 워커*(체인 끝)를 그 원문으로 재개하기 위함 — 깊은 전문가 협업이 리더로 튀지 않게.
            "active_chain": [
                {"from": int(f.from_id), "to": int(f.to_id), "kind": str(getattr(f, "kind", "work")),
                 "body": (getattr(f, "body", "") or "")[:1500]}
                for f in flow.comm.open_requests
                if int(f.to_id) != int(flow.comm.origin)
            ],
        }

    def _status_text(self, flow, t0, final=None) -> str:
        """[Rule/Status — 상태 가시화] 흐름 상태 메시지 본문. 묻기 전에 보이는 계기판:
        무엇이(요청 요약), 얼마나(시작 시각), 지금 누가(베턴 보유자), 살아 있는가(마지막 활동).
        시각은 Discord **동적 타임스탬프**(<t:유닉스:R>)로 박는다 — 상대시간을 클라이언트가
        계속 다시 그리므로, 컨테이너가 멈춰 수정(edit)이 끊겨도 표시는 '1초 전→2시간 전'으로
        늙는다. 수정 시점에 계산한 'N초 전' 고정 문자열은 박제되면 **거짓 생존 신호**가 되던
        결함(사용자 관측: 동면 중에도 '마지막 활동 1초 전')의 구조적 수정 — 죽으면 죽어 보인다.
        final이 오면 종결 확정 표기('✅ 완료'/'⏸ 중단')로 닫는다."""
        req = (getattr(flow, "status_req", "") or "")[:60]
        if final is not None:
            return f"{final} {time.strftime('%H:%M')} — “{req}”"
        now_m, now_w = time.monotonic(), time.time()
        start_ts = int(now_w - max(0, now_m - t0))
        alive = flow.comm.alive
        who = flow._info(alive) or ("담당자" if alive == flow.leader else f"<@{alive}>")
        done = sum(1 for h in flow.comm.history if h[0] == "respond")
        last_ts = int(now_w - max(0, now_m - (flow.last_activity or t0)))
        return (f"● 작업 중(시작 <t:{start_ts}:R>) — “{req}”\n"
                f"지금: {who} · 위임 {done}건 완주 · 세그먼트 {max(1, flow.leader_segment)}\n"
                f"마지막 활동: <t:{last_ts}:R>")

    @staticmethod
    def _idify_workspace(workspace, pid, name) -> str:
        """[신원=번호 — 사용자 제안] 흐름의 임시 폴더(new-…)를 'p-00n-슬러그'로 개명한다 — 작업
        공간의 정체성을 리더의 작명이 아니라 식별번호가 보증해, 일반명사 이름 충돌이 폴더·배포
        수준에서 무해해진다. 흐름 임시 폴더(new-*)일 때만 작동(직접 등록·시드 경로는 전달값 유지)."""
        try:
            ws = str(workspace or "").rstrip("/")
            parent, cur = os.path.dirname(ws), os.path.basename(ws)
            if not (cur.startswith("new-") and os.path.isdir(ws)):
                return str(workspace)
            slug = re.sub(r"[^0-9a-z가-힣-]+", "-", str(name or "").lower()).strip("-")[:32]
            pidl = pid.lower()
            if slug == pidl or slug.startswith(pidl + "-"):   # 이름에 식별번호가 새도 'p-021-p-021' 중복 접두 방지
                slug = slug[len(pidl):].strip("-")
            tgt = os.path.join(parent, f"{pid.lower()}{('-' + slug) if slug else ''}")
            if tgt != ws and not os.path.exists(tgt):
                os.replace(ws, tgt)
                return tgt
        except OSError:
            pass
        return str(workspace)

    @staticmethod
    def _same_purpose(a, b) -> bool:
        """두 목표 원문이 '같은 작품'을 가리키는지 — 토큰 겹침 50% 이상(짧은 쪽 기준).
        이름 일치 재사용의 신원 검증용: 라벨이 같아도 작품이 다르면 차지(하이재킹) 금지."""
        ta = {t for t in re.split(r"[^0-9A-Za-z가-힣]+", str(a or "")) if len(t) >= 2}
        tb = {t for t in re.split(r"[^0-9A-Za-z가-힣]+", str(b or "")) if len(t) >= 2}
        if not ta or not tb:
            return True   # 비교 불능이면 종전 동작(이름 신뢰) 유지
        return len(ta & tb) >= max(1, int(min(len(ta), len(tb)) * 0.5))

    def _similar_projects(self, text) -> str:
        """새 요청과 기존 프로젝트(이름+목표 원문)의 토큰 겹침으로 유사 후보를 찾는다 — 임계는
        '겹친 토큰 3개 이상 또는 요청 토큰의 30%'. 정답을 정하지 않는다(신설/재사용은 리더 판단),
        리더가 몰라서 중복 신설하는 일만 막는다."""
        toks = {t for t in re.split(r"[^0-9A-Za-z가-힣]+", str(text or "")) if len(t) >= 2}
        if not toks:
            return ""
        out = []
        for p in self.projects.values():
            base = f"{p.get('name', '')} {p.get('purpose', '')}"
            ptoks = {t for t in re.split(r"[^0-9A-Za-z가-힣]+", base) if len(t) >= 2}
            inter = toks & ptoks
            if len(inter) >= max(3, int(len(toks) * 0.3)):
                out.append(f"{p['id']} '{p.get('name', '')}'")
        return " / ".join(out[:3])

    def _checkpoint_open_task(self, flow) -> None:
        """[크래시-세이프 Task 스냅샷] 흐름 '도중' Task 전이마다 미완 Task를 레지스트리에 영속한다 —
        종전엔 흐름 '종료' 시에만 써서, 동면(컨테이너 정지)·강제종료처럼 마감 코드가 못 도는 죽음이면
        진행 중 Task의 정체가 유실돼 복구가 '같은 Task 이어가기'가 아니라 '새 Task'로 시작했다
        (라이브 관측: 093740-1 동결 → 복구가 122245-1 신설, 옛 블록은 '진행' 박제 — 사용자 지적).
        guide의 전이 지점(create_task/set_goal/owner 확정/complete_task)이 flow.checkpoint_task로 호출."""
        ch = flow.project_channel
        if not ch or int(ch) not in self.projects:
            return
        p = self.projects[int(ch)]
        p["open_task"] = (self._task_snapshot(flow, flow.current)
                          if flow.current is not None else None)
        self._save_projects()

    async def _restore_open_task(self, flow, proj) -> Optional[dict]:
        """프로젝트에 저장된 미완 Task가 있으면 이번 흐름에 그대로 되살린다 — 같은 상태블록·스레드·담당자
        (owner)·팀을 재부착해 '이어가기'가 사용자가 Task명을 부르지 않아도 그 Task를 잇게 한다(담당자가
        판단해 이어감). 검증 누계는 0에서 시작(verified=False 등) → 완료 전 run 재검증을 강제. 되살린
        스냅샷을 반환(없으면 None)."""
        snap = proj.get("open_task")
        if not snap:
            return None
        team = [int(x) for x in snap.get("team", []) if int(x) in flow.pool]
        if flow.leader not in team:
            team = [flow.leader] + team
        group = [(f"<@{i}>", flow._info(i)) for i in team]
        status = TaskStatus(task_id=snap["task_id"], purpose=snap.get("purpose", ""),
                            status="진행", goal=snap.get("goal", ""),
                            owner=snap.get("owner_name", ""), group=group)
        ref = TaskRef(task_id=snap["task_id"], thread_id=snap["thread_id"],
                      block_id=snap["block_id"], status=status, team=team,
                      owner=int(snap.get("owner") or 0))
        if snap.get("collab_notes"):
            ref.collab_notes = snap["collab_notes"]   # 합의 기록 복원 — 재개 후 위임에도 동봉(스펙 증발 방지)
        if snap.get("acceptance"):
            ref.acceptance = snap["acceptance"]        # 수용 계약 복원 — 재개 마감이 같은 기준으로 검증(증발 방지)
        if snap.get("standard"):
            ref.standard = snap["standard"]            # [최대화] 최대 표준 복원 — 동면 재개에도 바가 유지(증발 방지)
        if snap.get("interfaces"):
            ref.interfaces = snap["interfaces"]        # [협업] 인터페이스 계약 복원 — 재개 L2 검증 일관
        ref.participated = {int(x) for x in snap.get("participated", [])}   # 협의 명단 복원(재협의 루프 차단)
        if snap.get("last_work_body"):
            ref.last_work_body = snap["last_work_body"]   # [정밀 복구] owner 위임 원문 복원 → SYS 이어가기가 replay
        # [정밀 복구 — 완료잠금(구조)] 담당(owner)이 있던 미완 Task를 되살리면, owner가 '이어가기'로 재인도하기
        # 전엔 complete를 *구조로* 막는다(종전엔 resume_continue_body 프롬프트 의존 → 모델이 잊으면 조기완료 사고:
        # 라이브 054013-1 조기완료→074010-1 신설). owner_incomplete=True가 (1) complete_task 게이트로 마감을 막고
        # (2) SYS 자동 이어가기(_auto_continue_owner)가 last_work_body 원문으로 owner를 직접 재개(리더 재작문·드리프트 차단).
        if int(snap.get("owner") or 0):
            ref.owner_incomplete = True
        # [정밀 복구 — 가장 깊은 워커 재개(#7)] 전체 체인(active_chain)이 있으면, 재개 owner를 *가장 깊은 활성
        # 워커*로 덮어쓴다 — 레벨1 owner가 아니라 끊긴 그 깊이(예: 8단 체인 끝의 디자이너)에서 재개해 깊은
        # 전문가 작업이 리더로 튀지 않게. last_work_body에 그 깊이 원문 + 체인 경로를 실어, #3의 _auto_continue_
        # owner가 그 워커를 정확히 재개하게 한다(상류 이미 끝난 부분은 작업공간 보존 → 리더가 통합).
        chain = snap.get("active_chain") or []
        if chain:
            deepest = chain[-1]
            wk = int(deepest.get("to") or 0)
            # 가장 깊은 프레임이 *진짜 더 깊은 워커*일 때만 덮어쓴다 — 리더/origin 프레임이거나 원문이 비면
            # (동기 완주로 깊은 위임이 이미 닫힘) 레벨1 owner 로직(#3)을 그대로 둔다(오발동 방지).
            if (wk and wk in flow.pool and wk != flow.leader and (deepest.get("body") or "").strip()):
                ref.owner = wk
                ref.status.owner = flow._info(wk) or f"<@{wk}>"
                path = " → ".join(f"{flow._info(c.get('from'))}→{flow._info(c.get('to'))}" for c in chain)
                ref.last_work_body = (
                    f"[끊긴 깊은 전문가 체인: {path}]\n[가장 깊은 이 작업을 당신({flow._info(wk)})이 받아 진행 중 "
                    f"끊겼습니다 — 작업공간에 이미 된 부분은 보존됨. 처음부터 다시 하지 말고 이어서 완성하세요]\n"
                    f"{(deepest.get('body') or '')[:1200]}")
                ref.owner_incomplete = True
                if wk not in ref.team:
                    ref.team.append(wk)
                self._log("deep_chain_restored", depth=len(chain), deepest=wk, task=ref.task_id)
        flow.tasks.append(ref)
        flow.current = ref
        # 되살린 Task 멤버를 프로젝트 팀에 **합친다(union)** — 덮어쓰면 그 Task에 낀 일부 멤버로
        # project_team이 축소돼, 같은 프로젝트에서 일하던 팀원이 이후 '이 프로젝트 팀이 아님'으로
        # 거부되던 라이브 버그(복원이 팀을 좁힘 — 사용자 관측). 좁히지 않고 넓히기만 한다(리더 포함).
        for x in [flow.leader] + team:
            if x not in flow.project_team:
                flow.project_team.append(x)
        flow.comm.reset_task_tracking()
        try:
            await flow.refresh(ref)   # 상태블록을 '진행'으로 재활성(블록이 남아 있으면)
        except Exception:
            pass
        self._log("open_task_restored", project=proj.get("id"), task=snap["task_id"],
                  owner=int(snap.get("owner") or 0))
        return snap

    def _load_profiles(self):
        """디스크(role_profiles.json)에서 직무 기준을 복원한다. 리클레임으로 사라지면 그만 —
        각 직군 전문가가 첫 작업 때 다시 작성한다(자가 재생; 사용자 디스코드를 오염시키지 않음)."""
        if not self.profiles_path or not os.path.exists(self.profiles_path):
            return
        try:
            data = json.load(open(self.profiles_path, encoding="utf-8"))
            self.role_profiles.update({k: v for k, v in (data.get("profiles") or {}).items() if v})
            self.role_experience.update({k: list(v)[-self._EXP_KEEP:]
                                         for k, v in (data.get("experience") or {}).items() if v})
        except Exception:
            pass

    def _save_profiles(self):
        if not self.profiles_path:
            return
        try:
            tmp = f"{self.profiles_path}.tmp-{time.monotonic_ns()}"   # 병렬 흐름 동시 저장 경합 방지
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"profiles": self.role_profiles, "experience": self.role_experience},
                          f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.profiles_path)
        except Exception:
            pass

    def _log(self, event, **f):
        rec = {"event": event, "ts": time.time(), **f}
        self.flow_log.append(rec)
        if self.flow_log_path:   # 메모리만이던 continue_incomplete/flow_done/req_sent를 디스크로 영속(관측)
            try:
                with open(self.flow_log_path, "a", encoding="utf-8") as fp:
                    fp.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            except OSError:
                pass

    def _reset_sessions(self, scope=None):
        """세션 파일 초기화 — scope를 주면 그 스코프만, 없으면 전체(운영 청소용).
        세션이 흐름 스코프별 파일로 분리된 뒤로는 새 요청에 리셋이 필요 없다(고유 스코프로 시작)."""
        if not self.session_dir:
            return
        pat = f"organt_state_{scope}_*.json" if scope else "organt_state_*.json"
        n = 0
        for fp in glob.glob(os.path.join(str(self.session_dir), pat)):
            try:
                os.remove(fp)
                n += 1
            except OSError:
                pass
        self._log("reset_sessions", cleared=n, scope=scope)

    # 모든 Organt 공통 원칙: 추론보다 검증, 소통으로 규약을 맞춘다.
    _PRINCIPLE = (
        "[원칙: 추론보다 검증] 다른 파트(동료의 규격·산출물·의도)에 대해 모르거나 가정이 필요한 "
        "순간, 추측해서 진행하지 마세요. 그 정보를 가진 동료에게 request(kind=Info)로 물어 "
        "확인하세요. 받은 답이 모호하거나 부족하면 다시 물어도 됩니다(재질문). 단, 진행에 "
        "꼭 필요한 것만 물으세요(불필요한 질문·정보 적재 금지).\n"
        "[규약은 합의로] 필드명·데이터 형태·API 경로·디자인 토큰 같은 인터페이스는 혼자 임의로 "
        "정하지 말고, 그걸 함께 쓰는 동료와 request(Info)로 합의해 정하세요. 동료 산출물은 "
        "Read/Glob로 직접 확인해 검증하세요.\n"
        "[요청은 하나씩 — 보내면 이 턴을 마치세요] 한 턴에 request는 하나만 보내세요. 보내면 '[위임됨]'이 "
        "즉시 돌아옵니다 — 그 즉시 이 턴을 마치세요('처리 중' 같은 말이나 추가 도구 호출·재위임·폴링 금지). "
        "SYS가 그 동료를 **끝까지 완주**시키고 그 결과와 함께 당신을 자동으로 다시 깨웁니다 — 그때 검증·통합하고 "
        "다음 요청을 보냅니다. 동료의 응답을 같은 턴 안에서 기다리려 하지 마세요(기다림은 SYS의 몫).\n"
        "[동료는 비동기로 일하지 않음 — 단일활성] 동료는 **한 번에 한 명만** 일합니다(SYS가 베턴으로 보장) — "
        "백그라운드 작업이나 '시차를 두고 도착하는 파일', 여러 동료 동시 진행은 존재하지 않습니다. 결과를 아직 "
        "못 받았다고 ls/run으로 기다리며 폴링하지 마세요(아무것도 진행되지 않고, SYS가 결과를 가져다줍니다). "
        "받은 산출물이 미완(응답에 '⚠ 턴 한도 도달'·남은 파일)이면 그 owner에게 request(Work)로 '이어서'를 "
        "보내야만 작업이 계속됩니다.\n"
        "[재현으로 진단] 버그·요청은 스펙에서 유추 말고 **run으로 실제 산출물을 돌려 증상을 재현**한 뒤 원인 코드를 "
        "고치세요. **동작/규칙/계산 문제와 표현/배치 문제는 보통 다른 코드 층**이니 증상에 맞는 층을 보세요(표면만 보고 "
        "엉뚱한 층 고치기 금지). 스펙 문서는 참고일 뿐 할 일 목록이 아닙니다.\n"
        "[실행으로 검증] '구동되나'가 아니라 **의도한 동작(사용자가 받는 결과)이 일어나나**를 run으로 끝까지 재현하세요 "
        "— 즉시실패·빈결과·오작동을 보고, goal 성공조건이 진짜 충족되는지가 기준('실행됨'에서 멈추지 말 것). **엣지케이스·"
        "내부 일관성**도 사용자 입장에서 확인('결과가 말이 되나'). 응답에 '⚠ 턴 한도 도달'이면 미완 — 보완 요청.\n"
        "[완성도 기준] 산출물은 '동작하는' 수준이 아니라 '그 종류 결과물로서 완성·정돈된' 수준이어야 "
        "합니다 — 같은 요청을 숙련자가 받았다면 당연히 갖췄을 요소·손맛·디자인을 갖추세요(요청의 함의에 맞는 "
        "깊이, 골격/최소판 금지). '무엇이 완성인지'는 그 artifact 종류의 상식에서 끌어내세요(하드코딩 아님). "
        "검증·리뷰도 '되나'만이 아니라 '완성도·경험의 질'까지 봅니다.\n"
        "[실 사용성 — 기능 완비 ≠ 잘 쓰임] 기능을 다 넣는 것과 *진짜 사용자가 핵심 목표를 쉽게 달성*하는 건 다릅니다. "
        "**주 사용 경로의 마찰을 없애세요**(맥락 기반이면 자동감지·기본값·원탭 — 다단계 수동 폼 금지). '사용자가 열면 "
        "*제일 먼저* 뭘 하고 싶을까'를 그 종류 최고 앱처럼 처리하세요(접근성도 실사용성). 손으로 다 설정시키는 설계 금지.\n"
        "[자원 동원 — 닿는 건 다 끌어다 최대 품질] 닿는 모든 자원을 **스스로** 동원하세요 — 실제 재료가 필요한데 "
        "코드로 placeholder 대체하게 되면 최종으로 내지 말고, WebSearch로 무료(CC0/오픈) 자원을 찾아 받아 통합하거나 "
        "닿는 무료 수단으로 확보하세요. 모르는 기법·라이브러리는 WebSearch로 익혀 적용('상상'으로 구현 말 것). 사용자/"
        "위임자에게 '키·자원 달라' 요구 금지(스스로 무키로 해결). **특히 '실제·공공 데이터로 학습/분석'이 핵심이면 "
        "데이터를 지어내지 마세요**(합성·랜덤 생성=placeholder 아니라 요구 위반, 지표가 순환논리) — 무키 경로(벌크 CSV·"
        "미러)를 찾아 실데이터를 쓰고, 정말 안 닿으면 합성으로 '완성'인 척 말고 보고하세요. 구한 소스·기법은 [경험]에 남기세요.\n"
        "[안 닿으면 최선의 차선책 — 포기·placeholder 금지] 이상적 도구·자원·데이터가 이 환경에서 안 닿으면(큰 다운로드·"
        "차단·GPU 필요 등) 실패나 placeholder가 아니라 **닿는 것 중 최선의 대안으로 갈아타 최고 품질로 완성**하는 게 "
        "목표입니다. 착수 전 **test-fetch/test-run으로 도달을 확인**하고 안 닿으면 즉시 피벗(막다른 길 반복 금지). "
        "'안 닿는다'는 보고는 어떤 차선책으로도 품질이 안 나올 때만의 최후수단.\n"
        "[되묻기 규칙] 당신에게 일을 맡긴 '직속 위임자'에겐 request(Info)로 되물을 수 있습니다(이 턴은 짧게 마치고 반환 "
        "— 위임자가 답한 뒤 다시 맡깁니다). 더 위·다른 멈춘 동료에겐 못 되물으니 그 산출물을 Read 하거나 안 멈춘 동료에게.\n"
        "[작업공간 레이아웃] 모든 산출물은 작업공간 '루트' 기준 하나의 일관된 구조로 만드세요(중첩 프로젝트 "
        "폴더 만들지 말 것). 산출물 종류의 관례를 따르되 — **웹 앱이면** 서버는 루트(server.js 또는 app.py), "
        "정적 프론트는 public/(index.html·style.css·app.js)로 두면 그대로 배포됩니다. 같은 산출물을 두 위치에 "
        "만들지 말고, 동료에게 위임할 땐 정확한 경로를 주세요.\n"
        "[보고] 결과는 간결한 일반 텍스트로 반환하세요 — 그 반환값이 곧 요청자에게 가는 Response. "
        "'---' 구분선/'✅ 완성' 배너/표/긴 머리말 같은 장식은 쓰지 말고, 보고하려고 request 쓰지 마세요."
    )

    def _craft_note(self, me) -> str:
        """이 봇 직군(겸직 포함)의 '직무 기준'을 프롬프트에 주입한다 — 없으면 첫 작업 때 스스로
        작성하게 한 번만 요청한다. 기준의 내용은 시스템이 정하지 않는다(그 직군의 전문가가 정의,
        Discord sys-roles에 영속, 사람이 편집 가능) — QA·백엔드·프론트·런타임 직군 전부 같은
        메커니즘으로 '각자의 일'이 고도화된다."""
        jobs = [j.strip() for j in str(self.bot_info.get(me, "")).split("·")
                if j.strip() and not j.strip().startswith("예비")]
        if not jobs:
            return ""
        notes, missing = [], []
        for j in jobs:
            p = self.role_profiles.get(j)
            if p:
                notes.append(f"[당신의 직무 기준 — {j} 전문가의 자기검수 기준. 이 기준을 충족한 산출물만 인도하세요]\n{p}")
            else:
                missing.append(j)
            exp = self.role_experience.get(j)
            if exp:
                notes.append(f"[당신의 최근 경험 — {j} 직군이 실제 작업에서 얻은 교훈. 같은 함정을 반복하지 마세요]\n"
                             + "\n".join(f"- {e}" for e in exp[-6:]))
        if jobs:
            # [의무형 — 데이터 근거] 선택형("없으면 생략")은 라이브에서 0% 산출, 의무형([직무기준]
            # 요청)은 7/7 직군 100% 산출. 학습 플라이휠(기준→경험→증류→개선)의 원료가 여기서만 나오므로
            # 고정 섹션으로 강제하되, '없음' 탈출구로 억지 채움(노이즈)을 막는다('없음'은 흡수 때 버려짐).
            notes.append(
                f"[경험 — 보고의 고정 섹션(생략 금지)] 작업 보고 끝에 반드시 아래 블록을 포함하세요. "
                f"이번 작업에서 얻은 **직군 차원의 일반화 가치가 있는** 교훈(함정·효과적이었던 방법) "
                f"1~2줄만 — 다음 작업에 주입되고 수면 중 직무 기준으로 증류됩니다. 새 교훈이 진짜 "
                f"없으면 본문에 '없음'이라고 쓰세요(억지로 채우는 것보다 '없음'이 낫습니다 — 일회성 "
                f"디테일·당연한 일반론은 노이즈입니다):\n[경험] {jobs[0]}\n(교훈 또는 '없음')\n[/경험]")
        if missing:
            notes.append(
                f"[직무 기준 작성 — 이번 한 번만] 당신 직군 '{missing[0]}'의 직무 기준이 아직 없습니다. "
                f"이번 보고 **맨 끝에** 아래 형식으로 이 직군의 '훌륭한 산출물·검증 기준' 5~8줄을 작성해 "
                f"포함하세요. 이후 모든 작업에서 당신의 자기검수 기준으로 영속·주입되고, **마감 검증의 "
                f"루브릭**으로도 쓰입니다 — 당신이 이 직군의 전문가로서 정의하는 것입니다(일반론 말고 이 "
                f"직군 특유의 품질·검증 기준으로). [RFC-008] 품질은 추상적 규칙보다 **예시로 더 잘 전수**되니"
                f"(암묵지), 기준 끝에 '좋은 예 / 흔한 나쁜 예'를 각 1줄 덧붙이면 검증자가 'good'을 구체로 "
                f"잡습니다:\n[직무기준] {missing[0]}\n(기준 줄들)\n좋은 예: …\n나쁜 예(흔한 미달): …\n[/직무기준]")
        return ("\n\n".join(notes) + "\n\n") if notes else ""

    def _portfolio_note(self) -> str:
        """회사가 지금까지 만든 것(기존 프로젝트 목록)을 담당자에게 사실로 보여준다.

        봇은 프로젝트 역사를 볼 수 없어 같은 도메인을 반복 선택하곤 했다(라이브: '안 쓰던 분야의
        공공데이터'를 요청받고도 이미 여러 번 쓴 대기질을 또 고름 — 담당자가 어떤 분야를 썼는지 몰라
        환각으로 판단). per-job 플라이휠(role_profiles·role_experience)이 '직군의 일'을 누적하듯,
        이건 '회사가 무엇을 만들어왔나'를 의사결정자(담당자)에게 누적해 신규성 판단·중복 회피·기존
        작품 이어가기의 사실 근거를 준다. 담당자 프롬프트에만 주입한다(도메인 선택은 담당자의 몫이고,
        팀원 프롬프트엔 노이즈)."""
        rows = []
        for p in self.projects.values():
            pid = str(p.get("id") or "?")
            name = (p.get("name") or "").strip()
            gist = (p.get("summary") or p.get("purpose") or "").strip().replace("\n", " ")
            if len(gist) > 70:
                gist = gist[:70].rstrip() + "…"
            # 이름을 앞에, 식별번호는 괄호로 뒤에 — 'P-NNN 이름' 표기를 봇이 새 프로젝트 이름으로
            # 흉내 내(번호를 이름에 박아) 채널·폴더에 번호가 중복되던 것 방지(번호는 시스템 몫).
            label = f"{name} ({pid})" if name else pid
            rows.append((pid, f"- {label}" + (f" — {gist}" if gist else "")))
        if not rows:
            return ""                              # 아직 만든 게 없으면 주입 안 함(하위호환·노이즈 0)
        rows.sort(key=lambda t: t[0])              # P-001, P-002 … 안정 정렬
        shown = [ln for _, ln in rows[-16:]]       # 길어지면 최근 것 위주(프롬프트 비대 방지)
        return (
            "[회사가 지금까지 만든 것 — 사실 목록(추측·환각 금지)] 아래는 우리 회사가 실제로 진행/배포한 "
            "프로젝트입니다(괄호 안 P-번호는 **시스템이 자동 부여하는 식별자** — 새 프로젝트 이름엔 번호를 "
            "넣지 마세요). 요청이 '안 쓰던 분야로/새롭게/지금까지와 다른' 같은 신규성을 요구하면 "
            "**이 목록에 없는 도메인**이라야 신규입니다(데이터 출처만 바꿔 같은 분야를 반복하는 건 신규가 "
            "아닙니다). **단, 어떤 도메인·데이터를 쓸지(주제)를 당신이 혼자 정하지 마세요** — 사용자가 "
            "도메인을 명시하지 않은 열린 요청이면, 이 목록을 '중복 회피' 근거로 팀과 공유하고 **회의에서 "
            "전문가들에게 도메인·데이터 후보를 제안받아 수렴해 정합니다('나는 X로 한다'고 통보 금지 — 당신은 "
            "퍼실리테이터). 주제 선정 자체가 회의의 산물입니다.** 반대로 기존 작품을 발전·수정하라는 요청이면 "
            "그 P-번호 채널에서 이어가세요. 목록에 없는 걸 '이미 했다'고 짐작하지 마세요 — 여기 적힌 것만이 사실입니다:\n"
            + "\n".join(shown) + "\n\n")

    def _env_note(self) -> str:
        """[이 환경의 능력·경계 — 담당자가 '닿는 범위에서 최고 품질' 경로로 팀을 이끌게 하는 사실]
        손코딩에 갇히지 말고 실제 툴·에셋을 끌어 쓰되, 막다른 길(Godot 1GB·온디바이스 AI생성 등)을
        처음부터 피하게 사실만 준다(해법은 팀이 정함 — 하드코딩 아님). 담당자에게만 주입(워커는 _PRINCIPLE
        의 차선책 원칙 + run 타임아웃 메시지로 충분 — 프롬프트 비대 방지)."""
        return (
            "[이 환경의 능력·경계(사실) — 닿는 범위에서 최고 품질로 팀을 이끄세요]\n"
            "- run은 **root Bash**(npm·pip·apt·curl 다 됨) → 실제 툴·라이브러리·에셋을 설치/다운로드해 품질을 "
            "올리세요. 단 run 한 번 ~1분이라 **큰 단일 다운로드(수백MB+)는 안 됨** → 닿는 경량 대안으로.\n"
            "- **GPU 없음** → 온디바이스 AI 생성(이미지·영상) 불가. 그래픽은 CC0 에셋 + 절차적 생성(Canvas·SVG·Pillow).\n"
            "- 배포는 **Render Node-웹 전용** → 최종물은 웹 서빙 가능해야(게임=웹엔진 Three.js·Phaser, AI=빌드타임 "
            "학습→예측 JSON을 Node가 서빙; 런타임 Python 서버는 배포 게이트가 막음).\n"
            "- 외부 소스는 다 닿지 않음 → 데이터·에셋 소스는 **착수 전 test-fetch로 도달 확인** 후 진행.\n\n"
        )

    def _prompt(self, body, kind, role, me, leader_id=None, flow=None):
        # '담당자'는 고정 직책이 아니라 이번 흐름의 To 수신자(=leader)다. 동료 목록엔 직군만 적고, 담당자에게만
        # '(담당자)' 표식을 단다(다른 흐름에선 같은 봇이 한 직원으로 참여).
        def _peer(i):
            lbl = self.bot_info.get(i, "?")
            return f"{lbl}(담당자)" if i == leader_id else lbl
        peers = ", ".join(f"{i}({_peer(i)})" for i in self.bot_info if i != me)
        domain = self.bot_info.get(me, "")
        # 탈중앙(퍼실리테이터): 모두가 '담당자의 요약'이 아니라 '사용자 원문'을 직접 본다 → 한 명의 해석을
        # 거치며 의도가 왜곡되는 걸 막는다. 받은 지시가 원문과 어긋나면 원문 의도를 우선·되물음.
        # [흐름별 원문 우선] 흐름에 박제된 원문을 먼저 본다 — 전역 self._origin_request는 다음 개입이
        # 덮어쓰므로 동시 흐름에서 교차 오염된다(웹 흐름이 게임 원문을 받던 라이브 버그). flow가 없을
        # 때만(도구 형식용 빈 흐름 등) 전역으로 폴백.
        orig = ((getattr(flow, "origin_request", "") if flow is not None else "")
                or getattr(self, "_origin_request", "") or "").strip()
        origin_note = (f"[사용자 원문 요청 — 진짜 의도(누구의 요약·해석도 아닌, 사용자가 실제로 한 말)]: {orig}\n"
                       f"이 원문이 기준입니다. 받은 지시·질문이 원문과 어긋나 보이면 원문 의도를 우선하고, 모호하면 되물으세요.\n\n"
                       if orig else "")
        # [파일 전송 — 인바운드] 사용자가 첨부한 파일은 작업공간 inbox/에 staging됨 → 봇이 Read로 확인해 쓰게 안내.
        _inb = (getattr(flow, "inbound_files", None) if flow is not None else None) or []
        inbound_note = ((f"[사용자가 첨부한 파일 — 작업공간 inbox/에 있습니다] "
                         f"{', '.join('inbox/' + n for n in _inb)}\n이 파일들을 Read로 확인해 요청에 반영하세요"
                         f"(사용자가 자료로 함께 보낸 것 — 추측 말고 실제 내용 확인). owner에게 위임 시 이 경로를 알려주세요.\n\n")
                        if _inb else "")
        if role == "leader":
            my_role = f"{domain}(담당자)" if domain else "담당자"
            # 담당자가 '예비'(직군 미배정)로 호명된 경우: 자길 예비로 방치하지 말고 먼저 자기 직군부터 채용해
            # 한 직원으로 참여한 뒤 팀을 꾸린다(사용자: '자기 자신도 프로젝트의 일원으로 참여해야지').
            is_spare_leader = str(domain).startswith("예비")
            spare_lead_note = (
                f"[당신은 '예비'로 호명됨 — 가장 먼저, 무엇보다 자기 직군부터] 당신(id {me})은 아직 직군 미배정 "
                f"'예비'인데 이번 흐름의 담당자로 호명됐습니다. **create_project·create_task·request 그 무엇보다 먼저, "
                f"맨 첫 행동으로 recruit(member={me}, role='당신이 맡을 직군')를 호출해 자기 직군부터 확정**하세요 — "
                f"이건 Task가 없어도 됩니다(Task 전에 호출 가능). 이 순서를 안 지키고 '예비'인 채로 프로젝트/Task를 "
                f"열면 화면(상태블록·동료 프롬프트)에 담당자가 '예비'로 박힙니다. 자기 직군을 정한 뒤엔 한 직원으로 "
                f"직접 참여(자기 도메인 구현에 기여)하고, 일에 필요한 다른 직군 동료를 골라 팀을 꾸리며 부족한 직군은 "
                f"recruit로 채우세요.\n"
                if is_spare_leader else "")
            # 팀은 고정이 아니라 담당자가 일에 맞게 동적으로 짠다(직군 고정 해결).
            team_note = (
                f"[팀은 당신이 동적으로 짠다 — 자동 전원 아님] 직군 구성은 미리 고정돼 있지 않습니다. 이 일에 **필요한 "
                f"직군을 당신이 직접 고르세요** — create_project(team='필요한 직군/동료들')로 팀을 정하고, 모자란 직군은 "
                f"recruit(role='직군명')로 더하세요(예비 인력이 그 직군으로 채용됨). 자동으로 전원이 소집되지 않습니다"
                f"(놀던 인력까지 무조건 부르지 말 것). set_goal은 '당신이 고른 그 팀 전원'의 협의로 통과합니다.\n")
            portfolio_note = self._portfolio_note()   # 회사가 만들어온 것 — 신규성 판단·중복 회피의 사실 근거(담당자에게만)
            return (
                f"당신은 이번 요청의 To로 지정돼 흐름을 여는 '담당자'입니다 — 고정 직책이 아니라 To를 받아 "
                f"이번 흐름의 담당이 된 것이며(다른 흐름에선 한 직원으로 참여), 특별한 권력자가 아닙니다. "
                f"당신의 역할: {my_role}\n"
                f"{origin_note}"
                f"{inbound_note}"
                f"{portfolio_note}"
                f"{self._env_note()}"
                f"받은 형태: {body}\n동료: {peers}\n\n"
                f"{self._craft_note(me)}"
                f"{spare_lead_note}{team_note}\n"
                f"{self._PRINCIPLE}\n\n"
                f"[구현은 위임 — 독식 금지] (시스템 강제) 자문만 받고 파일을 혼자 다 만들면 차단됩니다 — 구현은 각 "
                f"도메인 owner에게 Work로 맡기고, 당신은 조율·통합·검증·배포 + 자기 도메인만.\n"
                f"[퍼실리테이터 — 진행자] 당신은 해석자가 아니라 진행자입니다. 사용자 원문을 당신 식으로 바꾸지 말고 "
                f"**그대로 인용**해 묻고, set_goal의 Purpose·Goal은 **전문가 제안을 종합**해 적으세요(혼자 저작 금지). "
                f"채용은 기획에서 드러난 도메인 공백 기준(연속 무응답엔 재채용 말 것 — 인프라 문제).\n"
                f"[전문 능력은 흡수 말고 투입] (시스템 강제) 당신 직군 밖 전문능력(AI·디자인 등)은 떠안지 말고 "
                f"recruit로 전문가를 투입 — 전문가가 없으면 set_goal이 막힙니다(리더는 자기 직군만).\n"
                f"[당신의 위치 — 진행자이자 한 직원] 당신도 Write·run을 가진 직원입니다. **혼자 다 만들지도, 다 위임하고 "
                f"구경만 하지도 마세요**(둘 다 잘못) — 당신 도메인 하나는 직접 맡고, 다른 전문가 도메인은 그 owner에게 "
                f"Work로 위임해 끝까지 책임지게. 위임은 '구현 스펙' 말고 '측정가능한 목표'로(어떻게는 owner가 정함).\n"
                f"[재요청은 Redo로] (시스템 강제) owner 산출물은 먼저 검증(run·증거) → 충족이면 complete, 구체적 결함이 "
                f"드러날 때만 보완 요청. 완료된 걸 같은 동료에게 또 맡기면 Redo(한도 有)로만 처리됩니다.\n"
                f"[최종 인수 + 수평 수렴] (검증 게이트) 부분검증은 도메인 동료가, **전체 최종 인수(사용자 여정 처음~끝)는 "
                f"만든 사람 아닌 QA/독립 동료**가 — 저자편향 차단(자가검증으론 마감 안 됨). 통합 후 owner들+검증자를 meet로 "
                f"한 번 모아 '합쳐놓고 좋은가' 교차비평하고, 비평은 **그 owner에게 직접** 넘겨 1회 끌어올리게(당신 경유 금지). "
                f"왕복 2회+면 공정 종결.\n"
                f"[팀 구성] 작업 무게를 보고 팀 규모를 정해 create_project(team=…) — 무겁거나 중요한 도메인엔 여러 명, "
                f"풀 여유 인력도 활용(놀리지 말 것). 필요 직군이 없으면 recruit(role='직군')로 '예비'를 채용하세요 — "
                f"**말로 '너 X 담당'은 불가(직군 부여가 먼저, 시스템이 강제), 1봇 1직업, 같은 직군 있으면 재사용.**\n"
                f"[처리 갈래] 요청 성격을 보고:\n"
                f"- 단순 질문(혼자 답 가능) → 답만 간결히 반환.\n"
                f"- 팀 논의/토론 → 진행자로서 한 쪽 주장을 다른 쪽에 전달해 실제 반박/수용이 오가게 하고, 합의되면 채택·"
                f"안 되면(왕복 길면) 당신이 공정하게 단일 결론(자기편·무승부 금지).\n"
                f"- 실작업 Project → create_project로 팀을 모아 **기획 회의부터**: 주제·도메인·데이터·분해·담당을 통보 말고 "
                f"전문가 제안을 수렴(주제 미정 열린 요청이면 주제 선정이 첫 안건 — 접근 가능한 데이터인지 확인; 분해·담당도 "
                f"각 전문가가 자기 도메인을 정의). 이후 **산출물 단위 Task 하나씩**(complete로 마감해야 다음): "
                f"create_task(빈 껍데기, members) → 멤버 전원에게 request(Info)로 'Purpose·도메인 목표·성공기준'을 물어 수렴 → "
                f"set_goal(**측정가능한 결과만**, 구현 방법·파일은 적지 말 것 — 그건 owner 몫) → 맡을 동료에게 "
                f"request(Work)(받는 사람이 owner) → 검증 → complete → deploy. **set_goal은 팀 전원 협의 전엔 막힙니다(시스템 강제).**\n"
                f"[협업 인터페이스] (시스템 강제) 맞물리는 계약(필드·반환형·포맷)은 당신이 정하거나 중계하지 말고 "
                f"**owner끼리 request(Info)로 직접** 합의하게(리더 허브 금지 — 마감 게이트가 직접합의를 봅니다).\n"
                f"[무응답 시 독점 금지] 동료가 무응답이면 떠안지 말고 같은 직군 재배정/recruit로 충원(무응답은 대개 "
                f"인프라 문제 — 충원 남발 말고 최후수단; 직접 구현은 당신 도메인에 한함).\n"
                f"[owner가 일하는 중엔 완료·대리구현 금지] (시스템 강제) owner가 'run 검증한 산출물'을 낼 때까지 "
                f"complete·그 도메인 대리 Write 거부(허위완료 차단) — 같은 owner에게 다시 맡겨 받으세요.\n"
                f"[검증·배포] (시스템 강제) '완료'는 run 증거로만 — run 없이 complete 거부, 충족되면 **deploy 필수**. "
                f"전체 최종검증은 만든 사람 아닌 QA/독립 동료가(자가 run으론 교차검증 미충족). 결함은 그 owner에게 "
                f"맡기고 당신이 붙잡고 반복 디버깅 말 것."
            )
        my_role = domain or "팀원"
        return (
            f"당신은 자율적으로 일하는 팀원입니다(당신도 필요하면 동료에게 먼저 묻습니다). "
            f"당신의 역할: {my_role}\n{origin_note}{inbound_note}받은 요청({getattr(kind, 'value', kind)}): {body}\n동료: {peers}\n\n"
            f"{self._craft_note(me)}"
            f"{self._PRINCIPLE}\n\n"
            f"**기획 단계에서 '당신 도메인의 할 일·담당'을 물으면** 당신 전문 영역의 할 일을 스스로 정의해 제안하고 맡을 "
            f"것을 밝히세요(그 분야 전문가는 당신). **단 협의(Info) 중엔 제안·합의만 — 파일 Write는 구조적으로 차단됩니다"
            f"(구현은 Work로 위임받은 뒤).**\n"
            f"**당신이 owner면** 받은 목표를 직접 구현·검증까지 끝까지 책임지세요(리더에게 되넘기지 말 것). 턴 예산은 "
            f"넉넉하니 큰 산출물도 파일을 나눠 끝까지 만들고, 정말 못 끝냈을 때만 '어디까지 했고 뭐가 남았는지' 정확히 "
            f"적어 반환하세요(SYS가 그 지점부터 '이어서' 잇습니다 — '진행 중·마저 하겠다'로 멈추지 말 것). 산출물은 최소 "
            f"동작판이 아니라 그 종류가 당연히 갖출 완성도로(깊이 비평엔 변명 말고 끌어올리기). 역할 밖 산출물 금지.\n"
            f"당신 산출물이 다른 동료 것과 **맞물리면**(공유 인터페이스) 일방 결정 말고 그 owner에게 request(Info)로 먼저 "
            f"합의하세요(상대가 정한 게 있으면 Read·질의로 확인). 일손이 더 필요하면 recruit로 합류.\n"
            f"**토론 입장/대표가 주어졌다면** 그 입장에서 논거를 펴고, 전달된 상대 주장엔 맹목 동의 말고 구체적으로 반박/"
            f"일부 수용(근거와 함께). 파일은 작업공간 상대경로로. 끝나면 결과를 간결히 반환하세요."
        )

    async def _await_with_idle_watchdog(self, task, flow):
        """task(리더 실행)를 기다리되, flow.last_activity가 idle_timeout 동안 안 바뀌면(=흐름 전체 무진행=행)
        task를 취소한다(→ CancelledError). 요청·파일작성·실행 등 진행이 일어나는 한 아무리 길어도 안 끊는다
        — 고정 타임아웃이 아니라 '무진행' 기준이라, 오래 걸리는 정상 빌드는 보호하고 멈춘 것만 해소한다.
        (리더 턴엔 turn_timeout이 없어 생기던 '리더 행' 구멍을 메운다.)"""
        poll = max(1, min(20, self.idle_timeout))

        async def _wd():
            while not task.done():
                await asyncio.sleep(poll)
                idle = time.monotonic() - getattr(flow, "last_activity", time.monotonic())
                if idle > self.idle_timeout and not task.done():
                    self._log("flow_idle_abort", idle=int(idle), timeout=self.idle_timeout)
                    task.cancel()
                    return

        wd = asyncio.create_task(_wd())
        try:
            return await task
        finally:
            wd.cancel()

    _PROFILE_RE = re.compile(r"\[직무기준\]\s*(?P<job>[^\n]+)\n(?P<body>.*?)\n?\[/직무기준\]", re.S)
    _EXP_RE = re.compile(r"\[경험\]\s*(?P<job>[^\n]+)\n(?P<body>.*?)\n?\[/경험\]", re.S)
    _EXP_KEEP = 12   # 직군별 최근 경험 보존 줄 수(누적 상한 — 압축은 '기억 증류' 고도화의 몫)

    async def _absorb_role_profiles(self, text: str) -> str:
        """보고 속 [직무기준] 블록을 흡수한다 — 메모리·Discord(sys-roles)에 영속하고 본문에서 제거.
        직군 전문가가 자기 기준을 한 번 쓰면 이후 모든 작업에 주입되는 '직무 기억'의 수집 지점."""
        if not text or ("[직무기준]" not in text and "[경험]" not in text):
            return text
        absorbed, learned = [], []

        def _take(m):
            job = (m.group("job") or "").strip()
            body = (m.group("body") or "").strip()
            if len(body) > 1500:
                # 하드캡은 '줄 단위'로 — 문장 중간 절단은 양질 데이터를 지키려던 장치가 데이터를
                # 훼손하는 역설(절단된 반쪽 원칙이 매 턴 주입됨). 마지막 완전한 줄까지만 남긴다.
                cut = body[:1500]
                body = cut[:cut.rfind("\n")] if "\n" in cut else cut
            if job and body:
                self.role_profiles[job] = body
                absorbed.append((job, body))
            return ""

        def _learn(m):
            job = (m.group("job") or "").strip()
            body = (m.group("body") or "").strip()[:600]
            if job and body:
                # '없음' 류는 버린다 — 의무 섹션의 탈출구이지 경험이 아니다(저장하면 다음 프롬프트
                # 주입과 증류 원료가 노이즈로 오염된다). 괄호 안내문 재복창도 같은 이유로 컷.
                lines = [ln.strip() for ln in body.splitlines()
                         if ln.strip() and ln.strip().rstrip(".") not in
                         ("없음", "없다", "-", "특이사항 없음", "(교훈 또는 '없음')")]
                if not lines:
                    return ""
                cur = self.role_experience.setdefault(job, [])
                cur.extend(lines)
                del cur[:-self._EXP_KEEP]   # 최근 N줄만(압축은 기억 증류의 몫)
                learned.append((job, len(lines)))
            return ""

        out = self._PROFILE_RE.sub(_take, text)
        out = self._EXP_RE.sub(_learn, out).strip()
        if absorbed or learned:
            self._save_profiles()   # 디스크 영속(사용자 디스코드를 시스템 데이터로 오염시키지 않음)
            for job, body in absorbed:
                self._log("role_profile_saved", job=job, size=len(body))
            for job, n in learned:
                self._log("role_experience_saved", job=job, lines=n)
        return out or "(직무 기준/경험이 기록되었습니다.)"

    _DISTILL_MIN = int(os.environ.get("ORGANT_DISTILL_MIN", "5"))   # 증류 발동 최소 경험 줄 수

    def pick_distill_job(self):
        """증류가 필요한 직군 하나를 고른다 — 경험이 가장 많이 쌓인 직군부터(없으면 None)."""
        jobs = self.pick_distill_jobs()
        return jobs[0] if jobs else None

    # [위생 증류 발동선] 기준이 이 길이를 넘으면 새 경험이 없어도 '정리 전용' 수면 대상 — 기준은
    # 매 턴 주입되므로 비대=주의 분산이고, 하드캡 절단 사고 전에 전문가 스스로 통합·다이어트하게 한다.
    _HYGIENE_AT = int(os.environ.get("ORGANT_HYGIENE_AT", "1100"))

    def pick_distill_jobs(self):
        """증류 후보 직군들 — ① 경험이 쌓인 직군(많은 순) ② 기준이 비대해진 직군(위생 증류,
        경험 0이어도). [병렬] 일부 전문가가 흐름에 묶여 있어도 가용한 다음 후보가 자기계발한다."""
        cands = sorted(((len(v), k) for k, v in self.role_experience.items()
                        if len(v) >= self._DISTILL_MIN), reverse=True)
        jobs = [k for _, k in cands]
        for job, prof in self.role_profiles.items():
            if job not in jobs and len(prof or "") > self._HYGIENE_AT:
                jobs.append(job)               # 정리 전용 수면 — 쌓기가 아니라 솎아내기
        return jobs

    def _bot_of_job(self, job):
        """그 직군을 보유한 봇(겸직 포함)을 찾는다 — 증류는 그 직군의 전문가 본인이 한다."""
        for mid, label in self.bot_info.items():
            if any(j.strip() == job for j in str(label or "").split("·")):
                return mid
        return None

    async def distill_role(self, job) -> bool:
        """[수면 — 기억 증류] 직군의 '최근 경험'을 그 전문가 봇이 직무 기준으로 압축한다.
        시스템은 내용을 정하지 않는다(전문가 자기정의 원칙) — 일반화 가치가 있는 교훈만 기준에
        흡수시키고, 증류된 경험 로그는 비운다. 증류 대화는 별도 세션(state_tag)이라 작업 기억을
        오염시키지 않는다. [병렬] '시스템 전체 유휴'가 아니라 **그 전문가 봇이 유휴**일 때 증류한다
        (회사가 일하는 중에도 한가한 직원은 자기계발 — 전역 점유 장부로 흐름과의 겹침을 차단)."""
        mid = self._bot_of_job(job)
        exp = self.role_experience.get(job) or []
        hygiene = len(self.role_profiles.get(job) or "") > self._HYGIENE_AT   # 정리 전용 수면 자격
        if mid is None or (len(exp) < self._DISTILL_MIN and not hygiene):
            return False
        if self.engaged.holder(mid) is not None:
            return False                                  # 그 전문가가 흐름 참여 중 → 이번 주기 스킵
        self.engaged.engage(mid, "__distill__")           # 증류 중 흐름이 이 봇을 집어가지 않게 점유
        try:
            return await self._distill_role_inner(job, mid, exp)
        finally:
            self.engaged.release(mid, "__distill__")
            # 증류 점유 때문에 큐로 밀린 요청이 있으면 이어서 처리(흐름 종료 드레인과 같은 판정).
            item = self._pop_runnable_queued()
            if item is not None:
                asyncio.ensure_future(self.handle_user_input(*item))

    async def _distill_role_inner(self, job, mid, exp) -> bool:
        cur = self.role_profiles.get(job, "(아직 없음)")
        flow = Flow(self.guide, 0, self.guild_id, mid, self.bot_info)   # 도구 형식용 빈 흐름(깨우기 없음)
        server = build_guide_server(flow, mid, "member")
        try:
            organt = self.organt_builder(mid, server, "member", flow, state_tag=f"distill_{mid}")
        except TypeError:
            organt = self.organt_builder(mid, server, "member", flow)   # 구형 빌더 호환(테스트 등)
        # [수면의 본질 = 정리(인간 수면의 기억 통합·솎아냄)] 더 많이 아는 게 아니라 더 선명하게.
        # LLM 특성: 기준은 매 턴 프롬프트에 주입되므로 길이=주의 분산 — 양질 소수 원칙이 효력의 조건.
        # 구조가 예산(원칙 수·길이)을 강제하고, 무엇을 남길지는 전문가가 정한다(자기정의 보존).
        raw = ("\n".join(f"- {e}" for e in exp) if exp
               else "(이번 수면은 새 경험 없음 — **정리 전용**: 기존 기준의 중복을 합치고 군더더기를 빼 더 선명하게)")
        prompt = (
            f"[자기계발 시간 — 직무 기준 증류] 당신은 '{job}' 전문가입니다. 도구를 쓰지 말고 텍스트로만 답하세요.\n\n"
            f"현재 직무 기준:\n{cur}\n\n"
            f"최근 실작업에서 쌓인 경험(원석):\n{raw}\n\n"
            f"수면의 본질은 '쌓기'가 아니라 '정리'입니다 — 전문가의 힘은 긴 규칙집이 아니라 소수의 깊은 "
            f"원칙입니다. 일반화 가치가 있는 교훈만 골라 기준에 녹이되:\n"
            f"- 새 교훈이 기존 원칙과 겹치면 **별도 추가가 아니라 기존 원칙에 합쳐** 더 일반적인 한 원칙으로.\n"
            f"- **예산: 원칙 최대 8개, 각 2줄 이내, 전체 1,000자 이내** — 넘치면 가장 덜 일반적인 원칙을 버리세요.\n"
            f"- 일회성 디테일·특정 프로젝트 한정 사항은 버리세요.\n"
            f"반드시 아래 형식만으로 답하세요:\n[직무기준] {job}\n(개선된 기준 줄들)\n[/직무기준]"
        )
        try:
            out = await organt.handle(prompt)
        except Exception as e:
            self._log("role_distill_failed", job=job, err=str(e)[:80])
            return False
        before = self.role_profiles.get(job)
        await self._absorb_role_profiles(out)            # [직무기준] 블록 흡수(영속 포함)
        ok = self.role_profiles.get(job) not in (None, before) or (before and "[직무기준]" in (out or ""))
        if self.role_profiles.get(job) and self.role_profiles.get(job) != cur:
            self.role_experience[job] = []               # 증류 완료 — 원석 비움
            self._save_profiles()
            self._log("role_distilled", job=job, used=len(exp))
            # 증류 세션은 일회성 — 다음 증류가 깨끗하게 시작하도록 제거
            if self.session_dir:
                try:
                    os.remove(os.path.join(str(self.session_dir), f"organt_state_distill_{mid}.json"))
                except OSError:
                    pass
            return True
        self._log("role_distill_noop", job=job)
        return False

    async def _drain_inflight(self, flow) -> str:
        """완주 중인 위임(detach 포함)이 있으면 끝까지 기다리고, 도착한 위임 결과를 이어가기 리더에게
        전달할 본문으로 돌려준다(없으면 ''). CLI가 도구 호출을 포기해도 deliver 태스크는 계속 돌므로
        — 일하는 owner를 자르지 않고 결과를 회수하는 게 단일활성·작업 보존의 핵심이다."""
        tasks = [t for t in getattr(flow, "inflight_tasks", ()) if not t.done()]
        if tasks:
            self._log("await_inflight_delegation", n=len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)
        res = getattr(flow, "detached_results", None)
        if res:
            out = ("\n\n[도착한 결과 — 직전에 진행하던 작업(동료 위임·배포·표결 등)이 끝나 결과가 도착했습니다] "
                   "백그라운드로 계속 돌지 않습니다(결과와 함께 멈춤). 결과가 완성이면 검증 후 진행/보고하고, "
                   "**위임 산출물이 미완(남은 파일·'⚠ 턴 한도')이면 기다리지 말고 즉시 같은 owner에게 "
                   "request(Work)로 '이어서'**를 보내세요(그래야만 작업이 계속됩니다). 배포 결과면 라이브 URL을 "
                   "확인해 보고하세요.\n"
                   + "\n".join(res[-3:]))
            del res[:]
            return out
        return ""

    async def _auto_continue_owner(self, flow, lead, limit=None) -> str:
        """[구조적 이어가기] 현재 Task의 위임이 '구조적으로 미완'(owner_incomplete — 턴한도·무활동
        타임아웃으로 끊김)이면, 리더(LLM)의 판단·기억에 맡기지 않고 **SYS가 직접** 같은 owner에게
        '이어서'를 보낸다 — 미완 이어가기는 판단이 아니라 기계적 행동이므로 구조가 보장한다(리더가
        '비동기 작업 중' 오인으로 폴링하며 이어가기 예산을 태우던 결함의 구조적 차단). 호출은 리더
        명의의 표준 request 파이프라인(베턴·게이트·기록·Discord 게시 동일)을 그대로 쓴다. 완성되면
        (owner_incomplete 해제) 결과 요약을 돌려줘 리더가 '판정'(검증·마감)만 하게 한다."""
        out = []
        # [활동 기반 — 진행 중인 긴 작업은 안 자름] n은 폭주 절대 안전망일 뿐(종전 24는 *진행 중인 대형
        # 작업*을 잘랐음 — P-010류, 목표=최대 품질과 모순). 무진행이면 아래 break가 즉시 잡으므로, 이 수는
        # '진행 중인 정당한 사슬'을 자르지 않는 넉넉한 한도로 둔다.
        n = int(os.environ.get("ORGANT_AUTO_CONTINUE", "100")) if limit is None else limit
        _orig = (getattr(flow.current, "last_work_body", "") or "").strip() if flow.current else ""
        if _orig:
            # [정밀 복구 — 드리프트 차단] 리더가 재작문한 위임이 아니라 *원래 보냈던 위임 원문* 그대로 이어 보낸다
            # (부팅 복구 5:13≠5:47 드리프트 차단). owner는 원래 받았던 그 지시로 정확히 재개한다.
            body = ("[SYS 자동 이어가기 — 처음부터 다시 하지 말 것] 직전에 이 작업으로 위임받았습니다(원문 그대로):\n"
                    f"{_orig}\n\n[이어가기] 작업공간에서 이미 된 부분은 그대로 두고 남은 부분만 마저 끝내 완성하세요.")
        else:
            body = ("[SYS 자동 이어가기 — 처음부터 다시 하지 말 것] 직전 작업이 도중에 끊겼습니다. "
                    "작업공간을 확인해 이미 된 부분은 그대로 두고, 남은 부분만 마저 끝내 완성하세요.")
        while n > 0:
            ref = flow.current
            if (ref is None or not getattr(ref, "owner", 0) or not getattr(ref, "owner_incomplete", False)
                    or flow.comm.alive != lead or flow.comm.done):
                break
            n -= 1
            acts_before = flow.act_count
            self._log("sys_auto_continue", task=ref.task_id, owner=ref.owner, left=n)
            tools = {t.name: t for t in make_guide_tools(flow, lead, "leader")}
            try:
                res = await tools["request"].handler(
                    {"to_id": str(ref.owner), "kind": "Work", "body": body})
                txt = (res.get("content") or [{}])[0].get("text", "")
                # [핸드오프 — SYS 내부 호출은 결과까지 동기 회수] 프로덕션 request는 즉시 '[위임됨]'을 반환하고
                # 동료 작업을 인플라이트로 등록한다. SYS 내부 호출은 75초 도구호출이 아니라 블록 가능하므로,
                # 여기서 그 인플라이트를 완주시켜 *실제 결과*를 받고 베턴을 리더로 복귀시킨다(동기처럼). 안 그러면
                # 리더 run_turn이 owner 인플라이트와 동시에 돌아 이중 활성이 되고, 진행 판정도 빈 '[위임됨]'을 본다.
                if "[위임됨" in (txt or ""):
                    _d = await self._drain_inflight(flow)
                    if _d:
                        txt = _d
            except Exception as e:
                txt = f"(자동 이어가기 처리 오류: {e})"
                out.append(txt)
                break
            from .guide_tools import _speech_clip as _sc
            out.append(_sc(txt, 4000))   # 침묵 한계 금지 — 이어가기 결과도 내용이 곧 산출물
            # 진행이 전혀 없는데 여전히 미완이면(크래시 반복 등) 같은 호출을 더 박지 않는다 — 환경 문제.
            if flow.current is not None and flow.current.owner_incomplete and flow.act_count == acts_before:
                break
        if out:
            return ("\n\n[SYS 자동 이어가기 — 미완이던 위임을 시스템이 같은 담당자에게 이어 보내 받은 결과]\n"
                    + "\n".join(out))
        return ""

    async def _auto_delegate_owner(self, flow, lead) -> str:
        """[헛돎 발생 차단 — 구조적 위임(2026-06-15)] 리더가 현재 Task의 designated owner(스냅샷 복원
        등으로 지정됨)에게 **위임을 0건** 하고 솔로 run만 반복해 독식 차단(leader_runs>3)에 막힌 정체면,
        SYS가 직접 그 owner에게 '첫 위임'을 발사해 일이 굴러가게 한다. _auto_continue_owner는 '이미 위임된
        뒤 미완'만 잡으므로 '위임 0건'인 정체는 구조적 빈틈이었다(라이브: 신예준 P-014 — 거부 11·위임 0·
        헛돎). 헛돎을 '한도 N회 종결'로 사후 차단하지 않고 **발생 자체에서** 막는다(한도는 backstop). 위임이
        한 번 나가면 work_delegated>0이라 재발사 안 됨(1회). 리더는 완성본을 받아 판정만."""
        ref = flow.current
        if (ref is None or not getattr(ref, "owner", 0)
                or getattr(flow, "leader_runs", 0) <= 3
                or sum(getattr(t, "work_delegated", 0) for t in getattr(flow, "tasks", [])) != 0
                or flow.comm.alive != lead or flow.comm.done):
            return ""
        self._log("sys_auto_delegate", task=ref.task_id, owner=int(ref.owner),
                  leader_runs=int(getattr(flow, "leader_runs", 0)))
        tools = {t.name: t for t in make_guide_tools(flow, lead, "leader")}
        body = ("[SYS 자동 위임 — 리더가 위임 없이 헛돌아 시스템이 담당 owner에게 직접 맡김] 이 Task의 "
                "담당입니다. 작업공간에서 이미 된 부분은 두고 남은 부분을 직접 구현하고 run으로 검증해 보고하세요.")
        try:
            res = await tools["request"].handler({"to_id": str(ref.owner), "kind": "Work", "body": body})
            txt = (res.get("content") or [{}])[0].get("text", "")
            # [핸드오프 — SYS 내부 호출은 결과까지 동기 회수] 즉시 '[위임됨]'이면 인플라이트를 완주시켜 실제
            # 결과를 받고 베턴을 리더로 복귀(이중 활성·빈 결과 차단). _auto_continue_owner와 동일 이유.
            if "[위임됨" in (txt or ""):
                _d = await self._drain_inflight(flow)
                if _d:
                    txt = _d
        except Exception as e:
            return f"\n(SYS 자동 위임 처리 오류: {e})"
        from .guide_tools import _speech_clip as _sc
        return "\n\n[SYS 자동 위임 — 리더 헛돎 차단, 담당자에게 직접 발사한 결과]\n" + _sc(txt, 4000)

    async def _run_until_silent(self, coro_factory, flow) -> str:
        """coro를 실행하되, '도구 활동(flow.last_activity)이 turn_timeout 동안 한 번도 갱신되지 않은'
        경우(=진짜 행)에만 취소하고 TimeoutError를 낸다. 도구가 하나라도 돌면 시계가 갱신되어 무한정
        허용된다 → '퀄리티 있게 오래 일하는 owner'는 안 자르고 '완전히 멈춘 것'만 끊는다(벽시계 고정
        타임아웃이 일하는 워커를 잘라 좀비·미완을 만들던 결함의 근본 교정)."""
        flow.last_activity = time.monotonic()
        task = asyncio.ensure_future(coro_factory())
        poll = max(1, min(15, self.turn_timeout))
        timed_out = False

        async def _wd():
            nonlocal timed_out
            while not task.done():
                await asyncio.sleep(poll)
                idle = time.monotonic() - getattr(flow, "last_activity", time.monotonic())
                if idle > self.turn_timeout and not task.done():
                    timed_out = True
                    task.cancel()
                    return

        wd = asyncio.ensure_future(_wd())
        try:
            return await task
        except asyncio.CancelledError:
            if timed_out:
                raise asyncio.TimeoutError   # 무활동(행)으로 우리가 끊은 것
            raise                            # 외부(상위 흐름)에서 취소 — 그대로 전파
        finally:
            wd.cancel()
            if not task.done():              # 외부 취소·타임아웃 어느 쪽이든 내부 task 누수 방지
                task.cancel()

    async def run_turn(self, flow: Flow, organt_id, body, kind, role) -> str:
        # 에이전트가 죽으면(SDK 메시지리더 크래시·서브프로세스 SIGTERM 등) 같은 세션으로 되살려 재시도.
        # State는 organt_id별 파일에 영속되므로 새 인스턴스가 세션을 이어간다(전체 워크플로우 보호).
        flow.last_activity = time.monotonic()   # 진행 신호(턴 시작) — 무진행 워치독 갱신
        self._stage_inbound(flow)               # [파일 전송] 사용자 첨부를 작업공간 inbox/로(워크스페이스 준비됐으면, 멱등)
        # [일로 직업 획득 — Discord 역할 비동기 부여] 첫 실작업으로 '획득'된 직군을 Discord 역할로 영속한다
        # (jobs.json은 권한 훅이 이미 동기로 박음; Discord는 리클레임 복원용 — 비동기라 여기 턴 경계에서 드레인).
        _q = getattr(flow, "role_earned_queue", None)
        if _q:
            _fn = getattr(self.guide, "assign_job_role", None)
            while _q:
                _mid, _lbl = _q.pop(0)
                if _fn and getattr(flow, "guild_id", None):
                    try:
                        await _fn(flow.guild_id, _mid, _lbl)
                        self._log("role_earned", member=int(_mid), role=_lbl)
                    except Exception:
                        pass
        last = ""
        for attempt in range(3):
            server = build_guide_server(flow, organt_id, role)
            organt = self.organt_builder(organt_id, server, role, flow)
            try:
                # '…입력 중' 표시: 깨어난 Organt가 응답·작업을 작성하는 동안 현재 Task 스레드
                # (없으면 유저 채널)에 가시화. guide에 typing 없으면(테스트 등) 그냥 건너뜀.
                ch = (flow.current.thread_id if flow.current else None) or flow.user_channel
                tcm = getattr(self.guide, "typing", None)

                async def _do():
                    if tcm is not None:
                        async with tcm(ch, organt_id):
                            return await organt.handle(self._prompt(body, kind, role, organt_id, flow.leader, flow))
                    return await organt.handle(self._prompt(body, kind, role, organt_id, flow.leader, flow))

                # 리더 턴은 '흐름 전체'(중첩 워커 포함)를 품으므로 여기선 타임아웃 안 건다 — 상위 무진행
                # 워치독이 흐름 전체를 본다. 워커(비-리더) 턴은 '도구 활동이 turn_timeout 동안 완전히 멈춘'
                # 경우(진짜 행)에만 끊는다 — 일하는 동안은 무한정 허용(하트비트). 끊기면 '인프라 실패'로 반환.
                if role == "leader":
                    return await self._absorb_role_profiles(await _do())
                return await self._absorb_role_profiles(await self._run_until_silent(_do, flow))
            except asyncio.TimeoutError:
                self._log("agent_timeout", organt=organt_id, role=role, sec=self.turn_timeout)
                return (f"API Error: timeout — 동료({organt_id}) 서브프로세스가 {self.turn_timeout}s 동안 "
                        f"도구 활동이 전혀 없어(행) 끊겼습니다. 단일흐름이라 인프라 문제로 간주(크래시와 동일) — "
                        f"대체 채용 말고, 진행하던 일이 있으면 같은 담당자에게 '이어서' 재요청하거나 보고하세요.")
            except Exception as e:
                last = f"(에이전트 {organt_id} 처리 실패: {e})"
                self._log("agent_revive", organt=organt_id, attempt=attempt + 1, err=str(e)[:100])
                await asyncio.sleep(2 * (attempt + 1))
        return last

    async def _ensure_deploy(self, flow, lead, result):
        """배포 가능한 산출물(package.json)인데 deploy가 안 불렸고 자격증명·배포 슬롯(등록 프로젝트)이
        있으면, 리더에게 의존하지 않고 **SYS가 직접 deploy_sync로 배포**한다(리더가 빼먹는 누락 구멍 차단).
        미등록 흐름은 슬롯이 없어("") 자연 스킵 — 배포 신원은 프로젝트가 보증한다(사용자 설계).
        deploy_sync가 라이브 URL 실제 응답까지 확인하므로, 거짓 성공이 아니라 진짜 배포가 보장된다."""
        ws = str(flow.workspace) if flow.workspace else ""
        # 품질 게이트: 흐름이 미완으로 끝나거나(중단될 Task가 남음) 이 흐름에서 '완료'된 Task가 하나도
        # 없으면 강제 배포하지 않는다 — 미완·실패 산출물이 흐름 종료마다 자동으로 라이브를 덮던 것 차단.
        completed = any(getattr(getattr(t, "status", None), "status", "") == "완료"
                        for t in getattr(flow, "tasks", []))
        if flow.current is not None or not completed:
            return result
        deployable = bool(ws) and os.path.exists(os.path.join(ws, "package.json"))
        gh, ghu = os.environ.get("GH_PAT"), os.environ.get("GH_USER")
        rk, owner = os.environ.get("RENDER_KEY"), os.environ.get("RENDER_OWNER")
        from .guide_tools import deploy_service_name
        name = deploy_service_name(flow)   # [멀티 프로젝트] 프로젝트별 결정적 서비스명(env 고정 제거)
        if flow.deployed or not (deployable and name and gh and ghu and rk and owner):
            return result
        try:
            import anyio
            from .deploy import deploy_sync
            dep = await anyio.to_thread.run_sync(deploy_sync, ws, name, gh, ghu, rk, owner)
            flow.deployed = dep
            self._log("ensure_deploy", forced=True)
            return f"{result}\n\n[배포(SYS 강제)] {dep}"
        except Exception as e:
            return f"{result}\n\n(SYS 배포 강제 중 오류: {e})"

    def _close_flow(self, flow, leader_id, result):
        """베턴을 origin까지 닫는다. 정상이면 리더가 alive→clean close, 비정상(중간 미응답)이면
        열린 프레임을 위로 강제 정리(escalate)해 교착 없이 종료한다."""
        comm = flow.comm
        if not comm.done and comm.alive == leader_id and len(comm.open_requests) == 1:
            comm.respond(leader_id, "accept", result)        # 정상 종료
            return
        guard = 0
        while not comm.done and guard < 64:                   # 비정상: 강제 드레인
            guard += 1
            try:
                comm.escalate("흐름 종료 강제 정리(중간 미응답)")
            except CommError:
                break

    def record_user_feedback(self, channel_id, text):
        """사용자가 프로젝트 채널에 남긴 말을 그 프로젝트에 누적한다(RFC-011 M3 — 취향 축적).

        상용 품질의 천장은 LLM 취향(인간 상관 ~0.5)이라 유일한 신뢰 앵커는 사용자다. 사용자가
        이 프로젝트에서 반복해 지적·요구한 것(되풀이되는 불만)을 쌓아 두면 set_goal·검증에서 그걸
        '이 작품의 품질 기준'으로 되돌릴 수 있다 — 직군·도메인 키워드 하드코딩 없이(사용자 자신의
        말), 배포→플레이→비평이 돌수록 기준이 스스로 올라가는 학습 고리. projects.json에 영속해
        동면·재시작 후에도 누적이 유지된다(신규 채널은 아직 미등록이라 자동 skip — 원문은 purpose로 보존)."""
        text = (text or "").strip()
        if not text:
            return
        p = self.projects.get(int(channel_id))
        if p is None:
            return
        # [좀비 부활 재무장] 사용자가 이 프로젝트로 돌아왔다 → '자동 1회 재개됨' 표시를 해제해 다음
        # 부팅에서 다시 자동 재개 대상이 되게 한다(능동 반복 작업은 계속 이어가고, 버려진 채로만 멈춤).
        p.pop("recovery_attempted", None)
        fb = p.setdefault("feedback", [])
        if fb and fb[-1].get("text") == text:   # 복구 재발사·중복 전송 가드(연속 동일 무시)
            return
        fb.append({"ts": int(time.time()), "text": text[:600]})
        del fb[:-50]   # 저장 위생: 최근 50개만(품질 게이트 아님 — 용량 바운드)
        self._save_projects()

    def _aggregate_feedback(self, proj):
        """[크로스-프로젝트 취향 — '사용자=유일 불만족 엔진' 영속화(2026-06-20)] 이 프로젝트 피드백(전부) +
        과거 프로젝트들의 피드백(중복 제거, 최근순 8개)을 합쳐 '이 사용자가 *작품을 가로질러* 반복 요구하는
        표준'으로 반환한다. 종전엔 이 프로젝트 것만 봐서 한 작품서 고친 걸 다음 작품서 또 틀렸다 — 게이트를
        불만마다 새로 다는 대신(끝없음), 인간 신호가 표준으로 누적돼 스스로 개선되게."""
        own = (proj.get("feedback") if isinstance(proj, dict) else None) or []
        seen = {(f.get("text") or "").strip() for f in own}
        cross = []
        for pp in self.projects.values():
            if pp is proj or not isinstance(pp, dict):
                continue
            for fb in (pp.get("feedback") or []):
                t = (fb.get("text") or "").strip()
                if t and t not in seen:
                    seen.add(t); cross.append(fb)
        cross.sort(key=lambda f: f.get("ts", 0), reverse=True)
        return own + cross[:8]   # 이 프로젝트(전부) + 과거 작업 최근 취향 8개

    def _valid_leader(self, proj):
        """[프로젝트↔봇 결합 해제, 2026-06-15] 프로젝트 리더가 현재 로스터(연결된 봇)에 없으면 — 봇이
        해고·예비환원·미연결된 경우 — 가용 봇으로 자동 재배정해 반환한다. 프로젝트가 특정 봇ID에 종속돼
        깨지지 않게(봇은 자유롭게 넣고 뺄 수 있고, 기존 프로젝트는 유지). 우선순위: 옛 리더와 같은 직군 >
        아무 가용 봇(특정 직군 선호 하드코딩 없음 — 도메인 중립). 재배정은 영속(projects.json). 멀티봇 협업
        구조엔 영향 없음 — 리더 1명만 정하고 팀은 흐름이 현재 로스터에서 다시 꾸린다(복잡한 일=협업 그대로)."""
        if not proj:
            return None
        lead = proj.get("leader")
        if lead and lead in self.bot_info:
            return lead   # 유효(연결돼 있음) — 그대로
        # 무효(해고/예비환원/미연결) → 재배정해 프로젝트를 살린다
        old_role = str(self.bot_info.get(lead, "") or "") if lead else ""
        avail = [b for b in self.bot_info if not str(self.bot_info.get(b, "")).startswith("예비")]
        pick = next((b for b in avail if old_role and self.bot_info.get(b) == old_role), None)
        if pick is None:
            pick = avail[0] if avail else lead   # 같은 직군 없으면 아무 가용 봇(특정 직군 선호 하드코딩 제거)
        if pick and pick != lead:
            self._log("project_leader_reassigned", project=proj.get("id"), old=lead, new=pick,
                      reason="리더 봇 부재(해고/미연결) — 프로젝트 유지 위해 재배정")
            proj["leader"] = pick
            try:
                self._save_projects()
            except Exception:
                pass
        return pick or lead

    async def handle_user_input(self, channel_id, leader_id, user_text, root_id=None, attachments=None) -> dict:
        proj = self.projects.get(int(channel_id))   # 이 채널이 등록된 프로젝트면 '개입'(이어지는 작업)
        # [신규×신규 병렬 완화] 신규 요청도 고유 스코프로 동시 진행한다 — 과거 'main' 직렬은 등록
        # 경합 방지용이었으나 전역 점유·스코프 선점·원자 등록 이후 근거가 소멸(라이브: 서로 다른
        # 리더에게 보낸 두 신규가 직렬돼 병렬 의도가 좌절). 같은 리더면 전역 점유가 자연 직렬화한다.
        scope_key = proj["id"] if proj else f"new-{int(time.time() * 1000)}"
        live = {k: f for k, f in self.active_flows.items() if not f.done}
        # 이 흐름을 이끌 봇(전망치): 명시 To(리더 재지정 포함)가 로스터에 있으면 그 봇, 아니면 등록 리더.
        # 게이트에서 미리 계산해야 '리더가 타 흐름 참여 중'을 흐름을 띄우기 전에 거를 수 있다.
        prospective_lead = (leader_id if (leader_id and leader_id in self.bot_info)
                            else (self._valid_leader(proj) if proj else leader_id))
        # [병렬] 큐로 보내는 세 조건(버리지 않음 — 흐름 내 규약은 불변): ① 같은 스코프 진행 중(직렬)
        # ② 운영 노브 상한(설정 시에만) ③ 리더가 타 흐름 점유 중(한 직원은 한 번에 한 흐름 — 같은
        # 리더의 프로젝트들은 자연 직렬이 되고, 이것이 임의 흐름 수 상한을 대체하는 구조적 안전이다).
        if (scope_key in live
                or (self.max_flows > 0 and len(live) >= self.max_flows)
                or self.engaged.busy_elsewhere(prospective_lead, scope_key)):
            self.queue.append((channel_id, leader_id, user_text, root_id))
            self._log("queued", text=user_text[:80], depth=len(self.queue), scope=scope_key,
                      lead_busy=bool(self.engaged.busy_elsewhere(prospective_lead, scope_key)))
            # [Rule/Status — 침묵하는 큐 금지] 접수 사실을 즉시 보이게 한다(라이브: 큐에 든 요청이
            # 아무 표시 없이 조용해 사용자가 '못 들은 것'으로 체감). 묻기 전에 보여야 한다.
            try:
                await self.guide.post(
                    channel_id, 0,
                    f"⏸ 접수됨 — 대기열 {len(self.queue)}번째. 담당 동료가 진행 중인 작업을 마치면 "
                    f"자동으로 시작합니다(따로 다시 보내지 않으셔도 됩니다).",
                    reply_to=root_id)
            except Exception:
                pass
            return {"mode": "queued", "queued": len(self.queue)}
        # 세션 초기화는 '새 최상위 요청'에만 한다 — 기존 프로젝트 '개입(이어서/수정)'에선 건너뛴다.
        # [근본] 개입은 진행 중이던 팀·위임·owner를 '이어가야' 하는데, 세션을 지우면 리더와 동료가 그 기억을
        # 통째로 잃고(resume할 session_id가 사라짐) 처음부터 다시 계획한다 — 이게 사용자가 본 '리더가 직전
        # 위임(예: 장도현→김민준)을 무시하고, 팀을 일부만 다시 부르고, 혼자 검토·마무리하던' 행동의 근본 원인이다.
        # 개입 본문엔 새 요청/증상이 명시되므로 '이미 했다' 앵커링도 생기지 않는다(앵커링 방지 목적은 새 요청에만
        # 유효). 컨테이너 리클레임으로 세션 파일이 이미 사라졌으면 어차피 새로 시작하니 무해하다(그건 별개 유실).
        # [세션 스코프] 봇 세션 파일을 흐름 스코프별로 분리한다(organt_state_<scope>_<bot>.json) —
        # 프로젝트 간 기억 교차 오염이 '구조적으로' 불가능(병렬 동시 흐름에서도 안전). 새 요청은
        # 고유 스코프로 시작하므로 '이미 했다' 앵커링도 구조적으로 차단(리셋 불필요). 같은 프로젝트
        # 개입은 그 프로젝트 스코프 파일을 resume — 기억이 이어진다.
        session_scope = proj["id"] if proj else scope_key   # 신규는 흐름 스코프=세션 스코프(단일 정체성)
        if proj:
            self._log("intervention_keep_sessions", project=proj["id"])
        # 이전 흐름의 런타임 채용(예비→직군) 라벨 원복 — dict는 그대로 두고 내용만 갱신(빌더 클로저가 참조 중).
        self.bot_info.clear()
        self.bot_info.update(self._roster_labels)
        self._origin_request = (user_text or "").strip()   # 원문 보존 — 담당자가 요약·해석하기 전 '사용자가 실제로 한 말'
        # 리더 재지정(사용자 요청): 개입 시 [Request] To로 현 리더와 '다른' 봇을 명시하면 그 봇을 이 프로젝트의
        # 새 담당자로 갱신한다 — 게임 프로젝트인데 '백엔드'가 담당자로 고정되던 문제 해소(기획자 등으로 담당 이양
        # 가능). 평문 개입은 main이 to_id를 현 리더로 채우므로, leader_id != proj.leader면 '명시적 지정'으로 본다.
        if proj and leader_id and leader_id != proj.get("leader") and leader_id in self.bot_info:
            self._log("leader_reassigned", project=proj["id"], old=proj.get("leader"), new=leader_id)
            proj["leader"] = leader_id
            self._save_projects()
            self._sync_topic(channel_id)   # 토픽(서버 영속)에도 반영 — 리클레임 후 시드로 원복되지 않게
        lead = self._valid_leader(proj) if proj else leader_id
        flow = Flow(self.guide, channel_id, self.guild_id, lead, self.bot_info)
        flow._handoff = True   # [논블로킹 핸드오프] 프로덕션은 위임을 즉시-반환 핸드오프로(75초 detach·비동기 churn
                               #   차단). 동료 작업은 SYS가 호출 밖에서 직렬 완주시켜 결과로 잇는다. (테스트는 기본 동기.)
        flow.inbound_attachments = list(attachments or [])   # [파일 전송] 사용자 첨부 — 워크스페이스 준비 시 inbox/로 staging
        flow.stage_inbound = lambda: self._stage_inbound(flow)  # create_project가 워크스페이스 만든 직후 즉시 staging(turn1 가용)
        flow.session_scope = session_scope
        # [교차오염 차단 — 흐름별 원문 스냅샷] 사용자 원문을 흐름 객체에 '박제'한다. self._origin_request는
        # 다음 개입이 오면 덮어쓰이는 전역 단일 필드라, 동시 흐름이 있으면 먼저 돌던 흐름의 봇들이 _prompt에서
        # '나중 개입의 원문'을 진짜 의도로 받아 엉뚱한 작업으로 새 버린다(라이브 관측: P-016 웹 흐름이 진행 중일
        # 때 P-015 게임 개입이 도착→웹 리더가 '게임성을 강화해'를 자기 원문으로 받아 게임을 짓기 시작). 여기서
        # 박제하면(이후 await로 다른 개입이 끼어들어도) 이 흐름의 모든 프롬프트는 자기 원문만 본다.
        flow.origin_request = self._origin_request
        # [RFC-011 M3] 이 프로젝트에 누적된 사용자 취향(반복된 비평·요구)을 흐름에 부착 — set_goal·검증이
        # '상용 수준'의 외부 앵커로 되돌린다(사용자 자신의 말이라 하드코딩 0, 회차가 쌓일수록 기준 상승).
        # [크로스-프로젝트 취향 누적 — '사용자=유일 불만족 엔진'을 영속화(2026-06-20)] 종전엔 *이 프로젝트*
        # 피드백만 봐서, 한 작품서 고친 걸(자동위치·URL거짓·깊이 등) 다음 작품서 *또 틀렸다*. 사용자 교정은
        # 작품을 가로질러 유효하므로, 과거 프로젝트들의 피드백도 끌어와 '이 사용자가 반복 요구하는 기준'으로
        # 함께 주입한다 — 게이트를 불만마다 새로 다는 대신(끝없음), 인간 신호가 표준으로 쌓여 스스로 개선.
        flow.user_feedback = self._aggregate_feedback(proj)   # 이 프로젝트 + 과거 작업의 취향(크로스-프로젝트 표준)
        # [선점 — 레이스 봉쇄] 게이트 통과 직후·첫 await 이전에 스코프를 점유한다. 등록이 늦으면
        # (개입 복원 등 await 사이) 같은 채널의 연속 메시지가 둘 다 게이트를 통과해 '같은 프로젝트에
        # 흐름 2개'가 생길 수 있다(작업공간·베턴 이중화). 병렬 도입 전부터 있던 창을 함께 봉쇄.
        self.active_flows[scope_key] = flow
        # [전역 점유 — 리더 선점] 같은 sync 블록에서 리더를 장부에 등록 + 흐름의 comm을 장부에 연결.
        # 다른 프로젝트의 동시 시작이 같은 리더를 집어가는 레이스가 구조적으로 불가능해진다
        # (asyncio 단일 스레드 — 게이트 검사~여기까지 await 없음). start_root의 재등록은 멱등.
        self.engaged.engage(lead, scope_key)
        flow.comm.attach_engagement(self.engaged, scope_key)
        def _reg(ch, name):
            # [신원 재사용 권한] 개입(proj)은 자기 프로젝트 연장이 자명 → 무제한(None).
            # 메인 채널 신규 흐름은 사용자 원문에 명시된 P-번호만 재사용 가능(주소 지정의 이치).
            # 흐름에 박제된 원문 사용(전역 self._origin_request는 동시 개입에 덮어쓰여 — 이 closure는
            # 흐름 도중 실행되므로 전역을 읽으면 '남의 프로젝트 원문'으로 등록될 수 있다).
            _orig = (getattr(flow, "origin_request", "") or self._origin_request or "")
            reuse_ok = None if proj is not None else {
                f"P-{m}" for m in re.findall(r"[Pp]-?(\d{3})", _orig)}
            pid = self._register_project(ch, name, flow.workspace, flow.leader,
                                         purpose=_orig,  # 존재 이유 = 사용자 원문
                                         origin_msg=root_id or "",      # 원요청 링크(부팅 복구의 개입 라우팅 근거)
                                         reuse_ok=reuse_ok)
            p0 = self.projects.get(int(ch))
            if p0 is not None and status_mid and not p0.get("origin_status"):
                # [시초 계기판 영속] 원요청의 상태 메시지(채널·id·시작 시각)를 프로젝트에 기록 —
                # 졸업 재개가 새 계기판을 달지 않고 이 시초를 되살린다(사용자 설계).
                p0["origin_status"] = {"channel": status_ch, "id": str(status_mid),
                                       "started": int(time.time() - (time.monotonic() - status_t0))}
                self._save_projects()
            p = self.projects.get(int(ch))
            if p and p.get("workspace"):
                flow.workspace = p["workspace"]   # id-개명(p-00n-슬러그)/재사용(기존 산출물) 결과 채택
                self._stage_inbound(flow)         # [파일 전송] 워크스페이스 생긴 즉시 사용자 첨부를 inbox/로(turn1 가용)
            return pid
        flow.register_project = _reg
        # '기억'(직업 고정): 예비가 recruit로 직군을 받으면 그 직업을 다음 흐름에도 유지하도록 로스터 라벨에 반영
        # — 흐름 시작 때 _roster_labels로 원복되므로, 여기에 기록해야 채용한 직업이 지속된다(1봇 1직업의 연속성).
        flow.persist_role = self._persist_job   # 채용한 직군을 메모리+디스크(jobs.json)에 영속(재시작에도 유지)
        flow.craft_of = lambda job: (self.role_profiles.get(str(job).strip(), "") or "")   # [RFC-008 P0] 직군 직무기준 → 검증 루브릭 조회
        flow.checkpoint_task = lambda: self._checkpoint_open_task(flow)   # Task 전이마다 크래시-세이프 영속
        body = user_text
        if proj:                                     # 기존 프로젝트 개입 — 맥락 유지(재생성 X)
            flow.project_channel = int(channel_id)   # 기존 채널 재사용 → create_project는 no-op
            flow.workspace = proj["workspace"]
            flow.project_id, flow.intervention = proj["id"], proj
            flow.project_name = proj.get("name")   # 배포 슬롯 유도(프로젝트별 결정적 서비스명)
            # 미완 Task 되살리기: 저장된 '진행 중' Task가 있으면 같은 블록·스레드·owner로 재부착(flow.current).
            # → 사용자가 Task명을 부르지 않아도 담당자가 '그 일'을 이어가게 한다(사용자 요청 반영).
            try:
                resumed = await self._restore_open_task(flow, proj)
            except Exception:
                resumed = None   # 복원 실패는 흐름 자체를 막지 않는다(스코프 유령화 방지)
            resume_note = ""
            if resumed:
                resume_note = (
                    f"[진행 중이던 Task 복원됨 — '더 진행해'의 대상일 가능성이 큼] 이 프로젝트엔 아직 끝나지 않은 "
                    f"Task가 남아 있어 **상태블록·스레드·담당자(owner)를 그대로 되살렸습니다** — 사용자가 Task명을 "
                    f"일일이 부르지 않아도 '진행 중인 그 일'을 가리키는 것이니, 당신이 판단해 이어가세요:\n"
                    f"  · Task {resumed['task_id']} / Owner: {resumed.get('owner_name') or '(미정)'} / "
                    f"팀: {flow._names(flow.current.team) if flow.current else ''}\n"
                    f"  · Purpose: {resumed.get('purpose') or '(미정)'}\n"
                    f"  · Goal: {resumed.get('goal') or '(미정)'}\n"
                    f"  · 지금까지(직전 보고): {(resumed.get('result_so_far') or '(기록 없음)')[:200]}\n"
                    f"→ 사용자의 요청이 이 Task의 연장이면(대개 그렇습니다) **새 Task를 또 열지 말고 이 Task를 이어서** "
                    f"끝내세요: 남은 부분을 owner에게 request(Work)로 맡기고(이미 정해진 팀·owner 존중 — 가로채 혼자 "
                    f"마무리 금지), run으로 검증한 뒤 complete_task로 **이 블록**을 마감하세요. 만약 사용자가 **명백히 "
                    f"다른 새 작업**을 원한 거면, 이 Task를 먼저 적절히 마무리(complete_task)한 뒤 새 Task를 여세요(당신 판단).\n\n")
            # [Project.Context 주입 — docs Project.md "Organts는 Context를 숙지한다"] ① 프로젝트 목표
            # (사용자 원문 — 존재 이유)와 ② 직전 흐름의 마감 요약을 리더에게 준다. 목표가 없으면
            # '마지막 미완 Task 마감 = 프로젝트 끝'으로 시야가 좁아진다(라이브 관측: 아트 Task만 닫고
            # 멀티·배포가 남은 프로젝트를 종료 보고). 기록만 되고 읽는 곳이 없던 단절(감사 발견)의 복원.
            purpose_note = ""
            if (proj.get("purpose") or "").strip():
                purpose_note = (
                    f"[프로젝트 목표 — 사용자 원문(이 프로젝트의 존재 이유)] {proj['purpose'].strip()}\n"
                    f"(이번 개입·복원 Task를 마감해도 **이 목표에 남은 부분이 있으면 새 Task로 이어가거나, "
                    f"남은 일을 보고 끝에 명시**하세요 — Task 하나의 마감이 프로젝트의 끝이 아닙니다)\n\n")
            ctx_note = ""
            if (proj.get("summary") or "").strip():
                ctx_note = (f"[프로젝트 최근 맥락 — 직전 흐름의 마감 보고] {proj['summary'].strip()}\n"
                            f"(핵심 결정·방향성 참고용 — 사용자의 이번 요청이 우선합니다)\n\n")
            body = (
                f"[프로젝트 {proj['id']} 개입 — 기존 산출물 수정] 이미 작업공간·산출물이 있습니다. create_project 다시 만들지 마세요.\n"
                f"사용자가 보고한 요청/증상: {user_text}\n\n"
                f"{purpose_note}"
                f"{ctx_note}"
                f"{resume_note}"
                f"[이어지는 작업 — 처음부터 다시 짜지 말 것(중요)] 당신은 이 프로젝트에서 일한 **이전 세션 맥락을 그대로 "
                f"이어갑니다**. 직전에 진행 중이던 Task·목표·위임(누가 누구에게 무엇을 맡겼는지)·owner·팀 구성이 있었다면 "
                f"**그 상태를 이어받아 계속**하세요 — 팀을 처음부터 다시 짜거나 일부만 다시 부르지 말고(이미 정해진 팀·"
                f"owner를 존중), **이미 누군가에게 위임해 둔 일을 당신이 가로채 혼자 검토·마무리하지 마세요**(그 owner가 "
                f"끝내게 하고, 끝내 무응답이면 사용자에게 보고). 기억이 비어 있을 때(예: 환경 재시작으로 맥락 유실)만 "
                f"작업공간을 Read/run으로 확인해 현재 상태를 복원한 뒤 이어가세요.\n\n"
                f"[개입도 'Task 개입' 구조로 — 혼자 run으로 다 하기 금지(중요)] 사용자가 지적한 핵심 문제는 **리더가 "
                f"Task도 안 열고 혼자 run·Read·Edit로 재현·수정을 다 하려다 아무것도 못 끝낸 것**(독식). 개입도 반드시 "
                f"아래 구조로 가세요(단, 위처럼 이미 진행 중이던 작업을 이어가는 것이면 그 흐름을 잇고, 새 증상·새 요청이면):\n"
                f"① **먼저 create_task(members=고장난 부분의 도메인 담당자들)** 로 Task를 엽니다 — 혼자 run으로 재현부터 "
                f"하지 마세요(개입에선 Task 없이 run하면 구조적으로 막힙니다). ② 그 팀과 request(Info)로 'Purpose(무엇이 "
                f"잘못됐나)·Goal(무엇이 되면 고쳐짐인가, 측정가능)'을 합의해 set_goal로 확정(보고된 그 문제에만 한정 — 임의 "
                f"기능추가 금지). ③ **그 도메인 owner에게 request(Work)로 위임** — 재현·원인진단·수정·run 검증까지 그 owner가 "
                f"직접 합니다. **당신 도메인(예: 백엔드) 밖(VFX·디자인·프론트 등)은 절대 혼자 만들지 말고 그 전문가에게 "
                f"맡기세요.** 위임 없이 혼자 run을 반복하면 막힙니다. ④ owner가 검증된 산출물을 내면 당신이 run으로 최종 확인 "
                f"후 complete_task. 동작·물리·판정 문제는 server.js, 색·레이아웃·그리기 순서만 public/입니다.")
            self._log("intervention", project=proj["id"], text=user_text[:60])
        else:
            # [흐름 격리 — 시작부터 고유 폴더] 신규 흐름이 작업공간 루트에서 시작하면 다른 프로젝트
            # 폴더들이 다 보여 남의 산출물을 뒤지고 이어받는 오염이 생긴다(라이브: 모션 팀이 지진
            # 산출물을 발견·개조). 흐름마다 고유 폴더에서 시작하고, 프로젝트 등록 때 P-번호 이름
            # (p-00n-슬러그)으로 개명한다 — **이름이 아니라 번호가 신원**(사용자 제안).
            try:
                flow.workspace = os.path.join(self.workspace, scope_key)
                os.makedirs(flow.workspace, exist_ok=True)
            except OSError:
                flow.workspace = self.workspace
            # [공급 원칙 — 유사 프로젝트 알림] 같은 요청의 재전송이 리더의 이름 짓기 운(한글/영문)에
            # 따라 '기존 이어가기 vs 신설'로 갈리던 비결정성(라이브: 동일 원문 → P-006 중복 신설).
            # 판단은 리더 몫, 정보는 구조가 — 신설 전에 알아야 할 사실을 결정 지점에 공급한다.
            sim = self._similar_projects(user_text)
            if sim:
                body = (f"[유사 프로젝트 존재 — 참고] {sim}\n"
                        f"단어가 비슷해도 이 요청은 **새 작품으로 등록됩니다**(메인 채널 새 요청 = 신규가 기본). "
                        f"사용자가 위 프로젝트의 연장을 원했다면 원문에 P-번호가 있거나 그 프로젝트 채널에 "
                        f"직접 개입했을 것입니다 — 기존 작품을 임의로 이어받지 마세요(신원·작업공간 하이재킹 금지). "
                        f"겹치는 아이디어는 새 작업공간에서 새로 구현하세요.\n\n") + body
        if root_id is not None:
            flow.start_root(root_id)
        flow.wake = lambda to, b, k: self.run_turn(flow, to, b, k, "member")
        flow.log = self._log                       # 관측: req_sent 등을 flow.jsonl로 영속
        flow.last_activity = time.monotonic()
        # [Rule/Status — 상태 가시화] 흐름 시작과 함께 그 채널에 상태 메시지 1개를 System Bot으로
        # 올리고, 진행 동안 '수정'으로만 조용히 갱신한다(알림 0 — Guide/Discord.md). 시스템이 멈추면
        # 갱신도 멈춰 '마지막 활동'의 정체가 사용자에게 박제 신호가 된다(오늘 동면 관측의 직접 해법).
        # edit 능력이 없는 가이드(테스트 등)에선 통째로 생략 — 갱신 못 하는 거짓 계기판을 안 만든다.
        flow.status_req = (user_text or "").strip()
        status_t0 = time.monotonic()
        status_mid, status_updater = None, None
        status_ch = int(channel_id)
        if getattr(self.guide, "edit_message", None):
            # [시초 계기판 되살리기 — 사용자 설계: "재개는 시초가 살아나게만 하면 된다"] 같은
            # 원요청의 재개(졸업 라우팅: root_id == origin_msg)는 새 상태 메시지를 또 달지 않고
            # **원요청 채널의 시초 상태 메시지를 이어서 갱신**한다. 시작 시각도 시초의 것을
            # 유지 — 재개마다 '작업 중 0분'부터 새로 재고 동면 1회당 계기판이 1개씩 쌓이던
            # 노이즈(라이브 관측) 제거. 시초가 사라졌으면(삭제 등) 새로 단다(폴백).
            # [개입 대시보드 재사용 — 중복 금지, 사용자 설계 "개입은 그대로 남게"] 등록 프로젝트의
            # 흐름(졸업 재개든 프로젝트 채널 평문 개입이든)은 그 프로젝트의 **단일 대시보드**를 잇는다.
            # 종전엔 root_id==origin_msg(졸업 재개)일 때만 재사용해, 새 개입은 매번 새 대시보드를 달았다
            # (라이브 2026-06-13: 동면 후 개입마다 '작업 중 0분' 계기판이 1개씩 누적 — 사용자 지적).
            # 개입(proj 존재)이면 무조건 재사용한다. 신규 흐름(proj None)만 새로 단다.
            o = (proj.get("origin_status") if proj else None) or {}
            if o.get("id"):
                try:
                    t0_resume = time.monotonic() - max(0.0, time.time() - float(o.get("started") or time.time()))
                    await self.guide.edit_message(int(o["channel"]), o["id"],
                                                  self._status_text(flow, t0_resume))
                    status_ch, status_mid, status_t0 = int(o["channel"]), str(o["id"]), t0_resume
                except Exception:
                    status_mid = None
            if status_mid is None:
                try:
                    status_mid = await self.guide.post(channel_id, 0, self._status_text(flow, status_t0))
                    status_ch = int(channel_id)
                    # 개입에 새 대시보드를 달았으면(시초가 없거나 삭제됨) 프로젝트에 기록 → 다음 개입이
                    # 재사용(중복 누적 방지 + 시초 삭제 시 자가 치유). 신규 흐름(proj None)은 _reg가 기록.
                    if proj is not None and status_mid:
                        proj["origin_status"] = {"channel": status_ch, "id": str(status_mid),
                                                 "started": int(time.time() - (time.monotonic() - status_t0))}
                        self._save_projects()
                except Exception:
                    status_mid = None
            if status_mid:
                async def _status_updates():
                    period = int(os.environ.get("ORGANT_STATUS_PERIOD", "60"))
                    while not flow.done:
                        await asyncio.sleep(period)
                        if flow.done:
                            break
                        try:
                            await self.guide.edit_message(status_ch, status_mid,
                                                          self._status_text(flow, status_t0))
                        except Exception:
                            pass               # Discord 순단이 흐름을 건드리지 않게(best-effort)
                status_updater = asyncio.create_task(_status_updates())

        async def _run_leader():
            flow.leader_segment = 1
            acts_seg = flow.act_count          # 세그먼트 실작업 기준점(활동 기반 예산 — 첫 턴 포함)
            result = await self.run_turn(flow, lead, body, Kind.WORK, "leader")
            # 구조적 연속 실행: 턴 한도로 작업이 끊겼으면(진행 중 Task가 남았거나 '턴 한도' 표시)
            # 같은 세션으로 이어서 완료까지 재호출한다 — '턴 한도 = 무조건 中断' 결함 해소.
            cont = 0
            while ((flow.current is not None or "턴 한도 도달" in (result or ""))
                   and cont < self.max_continue):
                # [단일활성 복원] 리더 턴이 끝났는데 위임이 아직 '완주 중'이면(CLI가 도구 호출을 포기해
                # detach됐거나, 턴 한도로 끊겼지만 deliver 태스크는 살아 있음) — 그 위임을 죽이지 않고
                # **끝까지 기다린다**. 일하는 owner를 드레인으로 자르던 것(작업 유실·재위임 churn·'오유진
                # 2회 호출')의 근본 교정. 완주가 프레임을 닫으므로 대개 베턴도 자연 복귀한다.
                drained = await self._drain_inflight(flow)
                # 그래도 베턴이 굳어 있으면(진짜 고아 프레임) 강제 복구(escalate-drain)한 뒤 이어간다.
                if flow.comm.alive != lead and not flow.comm.done:
                    guard = 0
                    # origin 프레임은 남긴다(스택 1장에서 멈춤) — 이어가기 준비 드레인이 흐름
                    # 자체를 종료(comm.done)시켜 이후 요청이 전부 막히는 것 방지.
                    while (flow.comm.alive != lead and not flow.comm.done
                           and len(flow.comm.open_requests) > 1 and guard < 64):
                        try:
                            flow.comm.escalate("continue 전 베턴 복구(위임 고아 정리)")
                        except CommError:
                            break
                        guard += 1
                    self._log("baton_recover_continue", alive=flow.comm.alive, recovered=(flow.comm.alive == lead))
                # [구조적 이어가기] 미완(턴한도·타임아웃) 위임은 리더 판단에 맡기지 않고 SYS가 직접
                # 같은 owner에게 이어 보낸다 — 리더는 완성본을 받아 '판정'(검증·마감)만 한다.
                drained += await self._auto_continue_owner(flow, lead)
                # [헛돎 발생 차단] 리더가 designated owner에게 위임 0건이고 솔로 독식에만 막혀 헛돌면,
                # SYS가 직접 owner에게 첫 위임을 발사한다(위 _auto_continue_owner의 '위임 0건' 빈틈 메움).
                drained += await self._auto_delegate_owner(flow, lead)
                # [활동 기반 예산 — "작업 중이면 얼마가 걸리든 안 끊는다"(확립 원칙)의 세그먼트 적용]
                # 직전 세그먼트에 실작업(act_count 증가)이나 위임 완주 도착(drained)이 있었으면 예산을
                # 소모하지 않는다 — 예산의 목적은 '무진행 루프 차단'이지 '대형 작업 총량 제한'이 아니다.
                # 라이브 P-010: 동면 재개 5회+재협의 루프가 예산 12를 태워 '진행 중인' 작업이 마감 직전
                # 절단(사용자: "왜 작업 도중에 끊겼지"). 무진행 정체는 종전대로 이 예산+워치독이 잡는다.
                progressed = (flow.act_count > acts_seg) or bool(str(drained).strip())
                if not progressed:
                    cont += 1
                else:
                    cont = 0   # [연속 무진행 한도] 진행이 확인되면 리셋 — 예산은 '정체 감지기'다
                               # (사용자: "예산도 너무 작다" — 숫자가 아니라 의미를 교정: 진행하는 한
                               # 무제한, 연속 12회 헛돌 때만 정체로 종결).
                acts_seg = flow.act_count
                flow.leader_segment += 1
                self._log("continue_incomplete",
                          task=(flow.current.task_id if flow.current else None), attempt=cont,
                          seg=flow.leader_segment, progressed=progressed)
                # [기억 구멍 무력화] 이어가기마다 팀·소유의 '시스템 사실'을 재주입한다 — 외부 절단
                # (SIGTERM)으로 직전 턴이 세션에 안 남으면 리더가 자기 팀 구성을 잊고 '참여 중인가요?'
                # 재확인·팀 밖 호출을 반복했다(라이브 관측). 기억은 흔들려도 사실은 SYS가 들고 있다.
                team_note = ""
                if flow.current is not None:
                    try:
                        team_note = (
                            f"[시스템 기록 — 현재 Task {flow.current.task_id}] "
                            f"팀: {flow._names(flow.current.team)} / Owner: {flow.current.status.owner or '미정'} / "
                            f"Goal: {(flow.current.status.goal or '미확정')[:80]}\n"
                            f"[프로젝트 팀 전체] {flow._names(flow.project_team)} — 이 명단 밖 동료는 이 프로젝트 "
                            f"구성원이 아닙니다(필요하면 recruit로 합류부터).\n\n")
                    except Exception:
                        team_note = ""   # 사실 주입은 best-effort — 형식이 다른 Task여도 이어가기는 진행
                result = await self.run_turn(flow, lead, _CONTINUE_BODY + team_note + drained,
                                             Kind.WORK, "leader")
            # 이어가기 한도 소진/마감 후에도 완주 중인 위임이 있으면 그 결과까지 받아 보고에 붙인다
            # (작업 유실 방지 — 마지막 위임이 마감 직전에 끝나는 경우).
            drained = await self._drain_inflight(flow)
            if drained:
                result = (result or "") + drained
            return result

        leader_task = asyncio.create_task(_run_leader())
        try:
            # 무진행(행) 워치독: idle_timeout 동안 진행이 0이면 리더 턴 취소(리더-행 구멍 메움). 진행 중이면 무제한.
            result = await self._await_with_idle_watchdog(leader_task, flow)
        except asyncio.CancelledError:
            for t in list(getattr(flow, "inflight_tasks", ())):   # 흐름 중단 시 완주 태스크도 정리(누수 방지)
                if not t.done():
                    t.cancel()
            result = (f"(흐름 자동 중단: 약 {self.idle_timeout // 60}분간 아무 진행(요청·파일작성·실행)이 없어 '행'으로 "
                      f"판단했습니다 — 리더/동료 서브프로세스가 멈춘 듯합니다(환경 불안정). 지금까지 산출물은 작업공간에 "
                      f"남아 있습니다. 다시 시도하거나 반복되면 잠시 뒤 재요청하세요.)")
            self._log("flow_idle_aborted")
        except Exception as e:                     # 리더가 죽어도 흐름은 닫고 보고한다
            result = f"(리더 처리 중 오류: {e})"
        # 배포 강제: 배포 가능한 산출물인데 deploy를 안 불렀으면 리더에게 '배포만' 한 번 더(누락 방지).
        # 여기부터 마감 꼬리는 어떤 실패에도 끊기면 안 된다 — 끊기면 스코프·전역 점유가 유령으로
        # 남아 그 프로젝트(와 그 리더의 다른 프로젝트)가 영영 큐에 갇힌다(병렬에서 반경 확대).
        try:
            result = await self._ensure_deploy(flow, lead, result)
        except Exception as e:
            self._log("ensure_deploy_failed", err=str(e)[:80])
        # 리더의 반환값 = 사용자에게 가는 Response(=보고). origin 프레임을 닫아 시작점 복귀.
        try:
            await self.guide.post(flow.user_channel, lead, format_response(result),
                                  reply_to=flow.root_id)
        except Exception as e:
            self._log("final_post_failed", err=str(e)[:80])
        self._close_flow(flow, lead, result)
        flow.done, flow.final = True, result
        # [Rule/Status] 종결 확정 — 마지막 수정으로 ✅/⏸를 박고 갱신을 멈춘다(이후 불변).
        if status_updater is not None:
            status_updater.cancel()
        if status_mid is not None:
            try:
                mark = "⏸ 중단(미완 Task 이어가기 가능)" if flow.current is not None else "✅ 완료"
                await self.guide.edit_message(status_ch, status_mid,
                                              self._status_text(flow, status_t0, final=mark))
            except Exception:
                pass
        # 안전망: 리더가 complete_task로 명시적으로 닫지 않은 현재 Task는 '중단'으로 표시한다
        # (허위 완료 금지 — owner가 실제로 안 끝냈을 수 있으므로 '완료'로 둔갑시키지 않음).
        # 동시에, 그 미완 Task를 프로젝트 레지스트리에 스냅샷으로 남겨 '다음 개입'에서 같은 Task로
        # 되살릴 수 있게 한다(사용자가 Task명 안 불러도 '더 진행해'가 그 Task를 잇게 — 근본 구조).
        open_task_snap = None
        if flow.current is not None:
            flow.current.status.status = "중단"
            flow.current.status.result = (result or "")[:500]
            try:
                await flow.refresh(flow.current)   # Discord 실패가 마감 꼬리를 끊지 않게(유령 스코프 방지)
            except Exception:
                pass
            open_task_snap = self._task_snapshot(flow, flow.current)
            flow.current = None
        # 프로젝트 요약 + 미완 Task 영속 갱신(다음 개입 때 맥락·이어가기 대상으로 제공).
        # current가 None(=complete_task로 마감했거나 Task 자체가 없었음)이면 open_task를 비운다(완료 처리).
        if flow.project_channel:
            p = self.projects.get(int(flow.project_channel))
            if p:
                p["summary"] = (result or "")[:600]   # Project.Context — 개입 프롬프트에 주입됨
                p["open_task"] = open_task_snap
                self._save_projects()
        # 신규 흐름이 프로젝트를 등록했으면 세션을 프로젝트 스코프로 '승격'(리네임) — 다음 개입이
        # 이 흐름의 기억을 그대로 잇는다(흐름 도중엔 스코프 고정이라 회의 기억도 안 끊김).
        if flow.project_id and session_scope != flow.project_id and self.session_dir:
            for fp in glob.glob(os.path.join(str(self.session_dir),
                                             f"organt_state_{session_scope}_*.json")):
                try:
                    os.replace(fp, fp.replace(f"_{session_scope}_", f"_{flow.project_id}_"))
                except OSError:
                    pass
        self._log("flow_done", project=flow.project_channel is not None,
                  tasks=len(flow.tasks), comm_done=flow.comm.done)
        self.active_flows.pop(scope_key, None)
        # [전역 점유 해제 안전망] 이 흐름의 모든 점유를 일괄 해제 — 정상 경로는 respond/escalate가
        # 대칭으로 풀지만, 예외·강제 종료로 남은 점유가 있어도 여기서 회사 풀로 돌려보낸다.
        self.engaged.release_scope(scope_key)
        # [임시 폴더 위생] 프로젝트로 승격되지 못한 흐름 폴더(new-…)가 비어 있으면 정리 — 루트에
        # 빈 껍데기가 쌓이지 않게(산출물이 있으면 보존: 사용자가 살펴볼 수 있게 남긴다).
        if not flow.project_id:
            try:
                ws = str(flow.workspace or "")
                if os.path.basename(ws.rstrip("/")).startswith("new-") and not os.listdir(ws):
                    os.rmdir(ws)
            except OSError:
                pass
        # 큐 드레인: 지금 시작 가능한(스코프 비충돌·리더 가용) 첫 명령을 이어서 처리.
        item = self._pop_runnable_queued()
        if item is not None:
            return await self.handle_user_input(*item)
        return {"mode": "flow", "flow": flow}

    def _pop_runnable_queued(self):
        """큐에서 '지금 시작 가능한' 첫 항목을 꺼낸다(없으면 None) — 시작 가능 = 스코프 비충돌 +
        운영 상한 여유 + 그 흐름을 이끌 봇이 타 흐름에 점유돼 있지 않음(게이트와 같은 판정).
        흐름 종료와 수면 증류 종료(점유 해제 시점들)가 공용으로 쓴다."""
        live = {k for k, f in self.active_flows.items() if not f.done}
        for i, item in enumerate(list(self.queue)):
            ch = int(item[0])
            p = self.projects.get(ch)
            k = p["id"] if p else "main"
            lead_q = (item[1] if (item[1] and item[1] in self.bot_info)
                      else (self._valid_leader(p) if p else item[1]))
            if (k not in live
                    and (self.max_flows <= 0 or len(live) < self.max_flows)
                    and not self.engaged.busy_elsewhere(lead_q, k)):
                self.queue.pop(i)
                return item
        return None

    # --- 진짜 입구: 채널의 유저 형식 Request를 읽어 라우팅 ---

    async def read_latest_request(self, channel_id) -> Optional[Request]:
        msgs = await self.guide.read_thread(channel_id, limit=20)
        reqs = [m for m in msgs if isinstance(m, Request)]
        return reqs[-1] if reqs else None

    async def route_channel_request(self, channel_id, request: Request, root_id=None) -> dict:
        if request.to_id is None:
            self._log("ignored", reason="To 없음")
            return {"mode": "ignored"}
        return await self.handle_user_input(channel_id, request.to_id, request.body,
                                            root_id=request.message_id,
                                            attachments=getattr(request, "attachments", None))
