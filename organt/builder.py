"""[Core] Organt 빌더 — role에 맞는 도구·권한·훅·State를 갖춘 Organt를 만드는 빌더를 돌려준다.

매체-중립(Discord·SNS 무관): Discord 진입(main)과 SNS 러너(run_organt_sns)가 *공유*한다.
종전엔 이 빌더가 Discord 진입 모듈(main.py)에 있어, SNS 러너가 `from src.main import _make_builder`로
가져오며 discord.py를 *transitively* 끌어오던 계층 누수가 있었다 — 빌더를 Core로 옮겨 그 결합을 끊는다.
(2026-07 계층 분리: Core / Discord매체 / SNS매체 대칭화)
"""
import os
import time

from claude_agent_sdk import HookMatcher

from organt_core.audit import AuditLog, make_post_tool_use_hook
from organt_core.config import Config
from organt_core.tool_names import FLOW_TOOLS, LEADER_TOOLS
from .organt import Organt, build_options, load_persona, pinned_cwd
from organt_core.permissions import make_pre_tool_use_hook

# 워커 기본 도구(WebSearch 포함 — RFC-011 M1 자원동원). 매체-중립 Rule 자산.
WORKER_BASE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "ToolSearch", "WebSearch", "WebFetch"]


def _make_builder(cfg: Config, audit: AuditLog, bot_info=None, model_map=None, persona_map=None):
    """role에 맞는 도구·권한·훅·State를 갖춘 Organt를 만드는 빌더를 돌려준다.
    model_map({organt_id: model})이 주어지면 그 봇만 build_options에 model override를 싣는다
    (per-agent 모델 — 매체가 직원별 LLM 지정. 디스코드 경로는 model_map 미전달이라 동작 불변).
    persona_map({organt_id: persona})이 주어지면 그 봇만 기본 인격(CLAUDE.md) 뒤에 자기 개성·지침을
    덧붙인다 — 스튜디오에서 사용자가 지정한 봇별 정체성이 실제로 프롬프트에 실린다(미지정 봇은 불변).
    Config가 frozen이라 전역 cfg.model을 못 바꾸므로, override 인자로 봇별 모델을 통과시킨다."""
    bot_info = bot_info or {}
    model_map = model_map or {}
    persona_map = persona_map or {}
    def organt_builder(organt_id, server, role, flow=None, state_tag=None):
        # 리더도 한 명의 직원 — 구현 도구(Write/Edit)를 그대로 갖는다. 차이는 권한이 아니라
        # 역할: 목표는 팀 합의로 정하고(set_goal), Work 위임 본문은 '스펙'이 아니라
        # '측정가능한 목표'이며, 받은 owner가 구현·검증까지 끝까지 책임진다.
        allowed = [*WORKER_BASE_TOOLS, *FLOW_TOOLS]   # 기본 도구(WebSearch 포함, RFC-011 M1) + 흐름 도구
        # 턴 한도 = 폭주(무한 루프) 브레이크일 뿐, 작업을 자르는 수단이 아니다 — 끊겨도 작업·세션은
        # 보존되고 '이어서' 재위임으로 잇는다. 다만 큰 산출물(대형 클라 본체 등)이 한 위임 안에 끝나도록
        # 워커 예산을 넉넉히 두고, 운영 중 조정은 환경변수로(코드 수정·재배포 불필요).
        # 라이브 정량분석(2026-06-10): 어떤 워커도 한도 근처에 가지 않았다(최대 13회 도구호출/60턴) —
        # 미완의 원인은 한도가 아니라 도구포기·자발 중간보고였다. 한도는 '작업을 자르는 일이 절대
        # 없도록' 크게 두고(브레이크 역할만), 폭주는 활동 워치독·run 증거 게이트가 막는다.
        turns = int(os.environ.get("ORGANT_WORKER_TURNS", "300"))
        if role == "leader":
            allowed = allowed + LEADER_TOOLS
            turns = int(os.environ.get("ORGANT_LEADER_TURNS", "500"))
        # state_tag: 증류(수면) 등 '작업 외 대화'는 별도 세션 파일을 써 작업 기억을 오염시키지 않는다.
        # 흐름이 있으면 세션을 '흐름 스코프'별로 분리 — 프로젝트 간 기억 오염·병렬 흐름 충돌이
        # 구조적으로 불가능(같은 봇이 두 프로젝트에서 동시에 일해도 기억이 섞이지 않음).
        scope = getattr(flow, "session_scope", None) if flow is not None else None
        tag = state_tag or (f"{scope}_{organt_id}" if scope else organt_id)
        state_path = cfg.audit_log_path.parent / f"organt_state_{tag}.json"
        label = bot_info.get(organt_id, role)   # 협업 관찰성: 로그에 '누가' 남기기
        # sdk 서버별 도구호출 타임아웃(ms) — CLI가 env(MCP_TOOL_TIMEOUT)보다 우선 적용하는 명시 설정.
        # request(동료 위임)는 동료의 중첩 작업 동안 수십 분 블록되는 게 정상 설계라 사실상 해제해 둔다.
        server = {**server, "timeout": int(os.environ.get("MCP_TOOL_TIMEOUT", "14400000"))}
        heartbeat = None
        if flow is not None:
            def heartbeat():   # 메시지 수신 단위 하트비트 — 도구 훅 사이 사각(긴 단일 생성)을 메움
                try:
                    flow.last_activity = time.monotonic()
                except Exception:
                    pass
        # organt의 파일 도구(cwd)는 '현재 흐름의 작업공간'을 따른다 — 프로젝트별 폴더 분리와 정합
        # (cwd가 base 고정이면 run은 프로젝트 폴더, Write는 base로 가는 분열이 생긴다).
        cwd = str(getattr(flow, "workspace", None) or cfg.workspace_dir)
        # [세션-cwd 고정] CLI 세션 저장소는 cwd 기준 — 이미 세션이 있는 봇은 '그 세션이 시작된 cwd'로
        # 빌드해야 resume가 찾는다. 흐름 도중 create_project가 작업공간을 하위 폴더로 깎아도(카빙)
        # 세션이 안 깨진다(카빙 폴더는 원 cwd의 하위라 파일 쓰기 범위는 동일, run은 flow.workspace 사용).
        # 라이브 관측: cwd가 바뀐 리더 이어가기가 'No conversation found'로 12회 전부 헛돈 뒤 미완 종료.
        cwd = pinned_cwd(state_path) or cwd
        _bopts = dict(
            cwd=cwd, allowed_tools=allowed, mcp_servers={"guide": server}, max_turns=turns,
            hooks={
                "PreToolUse": [HookMatcher(hooks=[make_pre_tool_use_hook(audit, allowed, actor=organt_id, role=label, flow=flow)])],
                "PostToolUse": [HookMatcher(hooks=[make_post_tool_use_hook(audit, actor=organt_id, role=label, flow=flow)])],
            },
        )
        _m = model_map.get(organt_id)
        if _m:                                   # [per-agent 모델] 지정 봇만 override(없으면 cfg.model=전역)
            _bopts["model"] = _m
        _pp = (persona_map.get(organt_id) or "").strip()
        if _pp:                                  # [per-agent 인격] 기본 인격 뒤에 이 봇만의 개성·지침을 덧붙임
            _bopts["system_prompt"] = (load_persona()
                                       + "\n\n[이 직원만의 개성·지침 — 스튜디오에서 사용자가 지정한 정체성]\n"
                                       + _pp)
        return Organt(cfg, build_options(cfg, **_bopts),
                      state_path=str(state_path), on_activity=heartbeat)
    return organt_builder
