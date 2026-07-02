"""Organt(LLM) 본체.

claude-agent-sdk의 ClaudeSDKClient로 Organt를 구동한다.
작업공간(cwd) 안에서 내장 파일 툴(Read/Write/Edit)로 파일을 다룬다.

기능4 범위: Organt 본체 구성 + 파일시스템 접근.
- 인격(CLAUDE.md)·세션 보존(resume)은 Step2,
- Discord 소통 툴은 기능5, audit 훅은 기능6에서 옵션 override로 붙인다.
"""
import asyncio
import dataclasses
import json
import os
from pathlib import Path
from typing import Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from .config import ROOT, Config

# Organt 기본 인격(폴백). CLAUDE.md가 없거나 비면 이걸 system_prompt로 쓴다.
ORGANT_PERSONA = (
    "당신은 Discord 위에서 일하는 AI 직원 'Organt'입니다. 동료와 함께 한 회사처럼 일합니다. "
    "현재 작업 디렉터리 안에서 '상대경로'로만 파일을 만들고 수정합니다(작업공간 밖 접근 금지). "
    "짧은 요청이라도 목표는 '문자 그대로 최소'가 아니라 가용 자원으로 낼 수 있는 '최대' 품질입니다 — "
    "명시 안 됐어도 그 종류 산출물이 당연히 갖출 것까지 채우고(최소판 금지), 모르면 상상 말고 WebSearch로 "
    "조사하며, 추측보다 run·소통으로 검증하세요. 실작업은 request로 그 도메인 전문가 동료에게 맡기고, "
    "run으로 검증하고, deploy로 배포합니다."
)

# Organt 인격 파일(CLAUDE.md) 경로. 인격·기억·Rule/Guide 목록을 담는다.
PERSONA_PATH = ROOT / "organt" / "CLAUDE.md"


def load_persona(path=None) -> str:
    """Organt 인격(CLAUDE.md)을 읽어 system_prompt로 쓴다. 없거나 비면 기본 인격."""
    p = Path(path) if path is not None else PERSONA_PATH
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return ORGANT_PERSONA
    return text or ORGANT_PERSONA


_MAX_API_RETRY = 3   # 일시적 API 오류(과부하 등) 재시도 횟수


def _is_stale_session_error(text: str) -> bool:
    """resume 대상 세션이 CLI 저장소에 없다(cwd 불일치·저장소 유실) — 일시 오류와 달리 재시도가
    무의미하며, 세션을 버리고 새로 시작하는 것이 유일한 전진이다."""
    return "no conversation found" in (text or "").lower()


def pinned_cwd(state_path) -> Optional[str]:
    """이 상태 파일의 세션이 '시작된' cwd(있고, 디렉터리가 살아 있으면). CLI 세션 저장소는 cwd
    기준이라 같은 세션을 resume하는 빌드는 이 cwd를 그대로 써야 찾는다 — 흐름 도중 작업공간이
    바뀌어도(create_project 카빙) 세션 연속성이 깨지지 않게 하는 고정점."""
    try:
        d = json.loads(Path(state_path).read_text(encoding="utf-8"))
        c = str(d.get("cwd") or "")
        if d.get("session_id") and c and os.path.isdir(c):
            return c
    except (OSError, ValueError):
        pass
    return None


def _is_transient_api_error(text: str) -> bool:
    """응답이 일시적 오류(429/5xx/529 과부하·rate limit, 또는 SDK 서브프로세스 사망=SIGTERM/143·
    파이프 끊김·메시지리더 크래시·**제어 스트림 닫힘(Stream closed)**)로 보이는지 — resume 재시도 대상.
    빈 응답('')도 '서브프로세스가 발화 없이 조용히 종료'한 신호라 재시도 대상으로 본다(호출부에서 처리)."""
    t = (text or "").strip().lower()
    if not t.startswith("api error"):
        return False
    # [과민 재시도 교정(2026-06-23 전수감사)] bare 토큰 'cancel/abort/disconnect/stream/closed'는 watchdog의
    # *의도적* 취소나 무관한 "...closed" 텍스트까지 transient로 오인해 죽은 서브프로세스에 3회 재시도 churn을
    # 냈다. 특정 구문('stream closed' 등)·서브프로세스 사망 신호(sigterm/143/process exited=resume 의도)는
    # 유지하되 bare 토큰은 제거한다.
    return any(s in t for s in ("429", "500", "502", "503", "529", "overload", "rate", "timeout",
                                "command failed", "exit code", "sigterm", "143", "137",
                                "broken pipe", "message reader", "connection", "disconnected",
                                "stream closed", "stream is closed", "process exited"))


def _strip_decoration(text: str) -> str:
    """보고에서 장식용 수평선('---' 등)만 제거한다(내용은 보존)."""
    lines = [ln for ln in (text or "").splitlines()
             if ln.strip() not in ("---", "***", "___", "—", "──────")]
    return "\n".join(lines).strip()


def build_options(config: Config, **overrides) -> ClaudeAgentOptions:
    """Organt용 ClaudeAgentOptions를 만든다.

    기능5·6에서 mcp_servers/hooks/allowed_tools 등을 override로 주입한다.
    """
    opts = dict(
        model=config.model,                       # None이면 SDK 기본 모델
        system_prompt=load_persona(),             # CLAUDE.md 인격 로딩(없으면 기본)
        cwd=str(config.workspace_dir),            # 작업공간 안에서만 파일 작업
        allowed_tools=["Read", "Write", "Edit", "Bash"],  # 내장 파일/셸 툴(Step1 범위)
        permission_mode="acceptEdits",            # 파일 편집 자동 승인(권한 훅은 Step2)
        max_turns=16,                             # 작업당 턴 상한(폭주 방지)
    )
    # [워커 CLI 버전 교정(2026-06-24) — CCR 프록시 호환] SDK 번들 CLI(2.1.170)는 CCR-v2 egress 프록시를
    # 통과할 때 모델 API 응답 스트림을 끝없이 버퍼링→RSS 10GB로 폭주해 OOM(라이브: 모든 워커가 어떤 모델
    # 이든 시동 직후 죽음, 소켓 read 4.2GB 관측). 시스템에 설치된 새 CLI(2.1.187 — 메인 세션이 쓰는 그
    # 버전)는 같은 프록시로 정상 작동(즉시 응답·폭주 없음). README의 "upgrade it" 처방대로, 프록시를 끄지
    # 않고(금지) 워커가 호환 CLI를 쓰게 cli_path를 명시한다. 그 바이너리가 없으면 번들로 안전 폴백.
    _wcli = os.environ.get("ORGANT_WORKER_CLI") or "/opt/node22/bin/claude"
    if os.path.exists(_wcli):
        opts["cli_path"] = _wcli
    opts.update(overrides)
    return ClaudeAgentOptions(**opts)


class Organt:
    """파일시스템에 접근하고, 세션 resume로 State를 보존하는 Organt(LLM) 본체."""

    def __init__(self, config: Config, options: Optional[ClaudeAgentOptions] = None,
                 state_path=None, narrate=None, on_activity=None):
        self.config = config
        self.options = options or build_options(config)
        self.narrate = narrate   # (text)->None: 매 발화(추론) 기록 콜백(관측). 없으면 미기록.
        self.on_activity = on_activity   # ()->None: 메시지 수신마다 호출 — 침묵 워치독 하트비트.
        # State(작업 맥락)는 세션 ID로 보존한다. 재시작(새 인스턴스) 시 파일에서 복원.
        self.state_path = (Path(state_path) if state_path is not None
                           else config.audit_log_path.parent / "organt_state.json")
        self.session_id = self._load_session_id()

    def _load_session_id(self) -> Optional[str]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")).get("session_id")
        except (OSError, ValueError):
            return None

    def _save_session_id(self, sid: str) -> None:
        self.session_id = sid
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            # cwd를 함께 영속 — CLI 세션 저장소는 cwd 기준이라, 같은 세션을 잇는 다음 빌드가
            # '세션이 시작된 그 cwd'를 그대로 쓰게 한다(pinned_cwd). 흐름 도중 작업공간이 바뀌어도
            # (create_project의 폴더 카빙) resume가 깨지지 않는 구조적 근거.
            self.state_path.write_text(
                json.dumps({"session_id": sid, "cwd": str(self.options.cwd or "")}),
                encoding="utf-8")
        except OSError:
            pass

    def _reset_session(self) -> None:
        """스테일 세션 폐기 — resume 대상이 저장소에 없을 때(cwd 불일치·유실) 새 출발.
        상태 파일째 지워 cwd 고정도 함께 푼다(새 세션은 현재의 올바른 작업공간에서 시작)."""
        self.session_id = None
        try:
            self.state_path.unlink()
        except OSError:
            pass

    def _session_in_store(self) -> bool:
        """resume 대상 세션이 '현재 cwd의' CLI 세션 저장소에 실재하는가 — 저장소는 cwd 기준 슬러그
        (~/.claude/projects/<cwd의 '/'→'-'>/<sid>.jsonl). 에러 텍스트에 기대지 않는 결정론 판정
        (라이브 관측: SDK 예외에는 CLI의 'No conversation found'가 안 실려 마커 감지가 불발했다).
        레이아웃이 바뀌어 오판해도 결과는 '새 세션 시작'(안전한 저하)일 뿐 — 영구 헛돌이는 없다."""
        if not self.session_id:
            return False
        cwd = str(self.options.cwd or os.getcwd())
        p = (Path.home() / ".claude" / "projects" / cwd.replace("/", "-")
             / f"{self.session_id}.jsonl")
        try:
            return p.exists()
        except OSError:
            return False

    def _options_for_call(self) -> ClaudeAgentOptions:
        """직전 세션이 있으면 resume를 붙인 옵션을 만든다."""
        if self.session_id:
            return dataclasses.replace(self.options, resume=self.session_id)
        return self.options

    async def _run_once(self, prompt: str):
        """ClaudeSDKClient 한 번 실행 → (최종 발화, session_id).

        SYS의 무진행 취소(CancelledError)가 나도 `async with`의 정상 종료(__aexit__)가 SDK 자원을
        정리한다. 취소는 '도구 활동이 완전히 멈춘'(진짜 행) 경우에만 일어나므로 — 일하는 워커는 자르지
        않으므로 — 정상 종료가 깔끔히 이뤄진다(바쁜 워커를 끊다 자원이 남던 과거 문제의 근본 회피)."""
        final_text = ""
        captured_sid: Optional[str] = None
        truncated = False
        # stderr 수집: CLI의 실패 사유(예: 'No conversation found')는 stderr로만 나와 SDK 예외
        # 텍스트에 안 실린다(라이브 관측 — 마커 감지 불발의 원인). 꼬리를 모아 예외에 붙여
        # '왜 죽었는지'가 항상 에러 텍스트에 남게 한다(스테일 마커·일시오류 판별 모두 강화).
        err_tail: list = []

        def _collect_stderr(line: str) -> None:
            if line and len(err_tail) < 20:
                err_tail.append(str(line).strip())
            # stderr 출력 = CLI 서브프로세스가 살아 움직인다(레이트리밋 재시도·진행 로그 등). 표준출력
            # 메시지가 한동안 없어도(첫 토큰까지 침묵) 이 신호로 무진행 워치독의 사각을 메운다 —
            # '살아서 대기 중'을 '행(hang)'으로 오인해 잘 돌아가는 흐름을 끊던 결함 교정.
            if line and self.on_activity:
                try:
                    self.on_activity()
                except Exception:
                    pass

        # [작업공간 앵커 — cwd 오진 차단(2026-06-23, 사용자)] 봇은 자기 작업공간 절대경로를 구조적으로
        # 못 받아 모델 내장 프라이어('/workspace')로 흘러, 빈 /workspace를 보고 '이전 파일 모두 유실'로
        # 오판해 중복 리빌드를 지시하던 라이브 결함(P-031 5925). 경로가 닿는 통로가 '위임자가 본문에 직접
        # 타이핑'뿐이라 봇마다 들쭉날쭉했다. system_prompt는 resume 세션에 재적용이 불확실하므로, *매 턴
        # 메시지 본문*에 cwd 절대경로를 못 박아 모든 봇·모든 턴에 구조적으로 보장한다(단일 chokepoint).
        _cwd = getattr(self.options, "cwd", None)
        if _cwd:
            prompt = (f"[작업공간 — 절대경로] 당신의 모든 파일은 정확히 여기 있습니다: {_cwd}\n"
                      f"이 경로가 당신의 cwd입니다 — `/workspace`가 아닙니다. 파일·디렉터리는 항상 이 절대경로로 "
                      f"확인하고, 무언가 안 보여도 '유실'로 단정하지 말고 먼저 이 경로를 Read/ls 하세요.\n\n") + prompt

        # [in-flight = 워커 *생존* 보호(2026-06, 사용자 검증)] 이 하트비트는 '진행을 조작'하는 게 아니라
        # '워커 서브프로세스가 *실제로 살아서* 이 턴을 도는 중'을 반영한다 — receive_response를 await하는
        # 동안은 서브프로세스가 살아있다(죽으면 SDK가 예외를 던져 루프가 끝난다). 레이트리밋으로 첫 토큰까지
        # 느리거나 긴 독립 검증 중이면 출력이 한동안 없을 수 있는데(라이브 확인: 16분+ 도는 워커 0.5%CPU),
        # 그 침묵만으로 *살아 일하는 워커*를 잘라선 안 된다. 진짜 wedge(살았지만 영영 멈춤)는 러너 max_age로.
        async def _inflight_alive():
            try:
                while True:
                    await asyncio.sleep(20)
                    if self.on_activity:
                        try:
                            self.on_activity()           # '생존' 신호 — 서브프로세스가 살아 도는 한
                        except Exception:
                            pass
            except asyncio.CancelledError:
                pass

        opts = dataclasses.replace(self._options_for_call(), stderr=_collect_stderr)
        _alive = None
        try:
            async with ClaudeSDKClient(options=opts) as client:
                await client.query(prompt)
                _alive = asyncio.ensure_future(_inflight_alive())
                async for msg in client.receive_response():
                    # 메시지 수신도 '활동'이다 — 도구 호출이 없는 긴 모델 생성(거대 파일 하나를 첫 Write로
                    # 만들기 직전의 장문 사고/작성)이 침묵 워치독에 '행'으로 오인되지 않게, 도구 훅(Pre/Post)
                    # 사이의 사각을 메시지 단위 하트비트로 메운다.
                    if self.on_activity:
                        try:
                            self.on_activity()
                        except Exception:
                            pass
                    sid = getattr(msg, "session_id", None)
                    if sid:
                        captured_sid = sid
                    if isinstance(msg, AssistantMessage):
                        t = "".join(b.text for b in msg.content if isinstance(b, TextBlock)).strip()
                        if t:
                            final_text = t   # 마지막 비어있지 않은 발화만 유지
                            if self.narrate:   # 관측: 매 발화(추론)를 기록 — '왜 그 행동을 했나'를 본다
                                try:
                                    self.narrate(t)
                                except Exception:
                                    pass
                    elif isinstance(msg, ResultMessage):   # 턴 한도 등으로 끊겼는지
                        st = (getattr(msg, "subtype", "") or "") + (getattr(msg, "stop_reason", "") or "")
                        if "max_turns" in st.lower():
                            truncated = True
        except asyncio.CancelledError:
            raise                                    # 워치독 취소는 의미 보존(감싸지 않음)
        except Exception as e:
            tail = " | ".join(x for x in err_tail[-3:] if x)
            raise RuntimeError(f"{e}{(' [stderr] ' + tail) if tail else ''}") from e
        finally:
            if _alive is not None:
                _alive.cancel()                          # 턴 종료(서브프로세스 죽음·완료) → 생존 신호 중단
        if truncated and not _is_transient_api_error(final_text):
            final_text = (final_text + "\n(⚠ 턴 한도 도달 — 작업이 미완일 수 있음)").strip()
        return final_text, captured_sid

    async def handle(self, prompt: str) -> str:
        """요청 한 건을 처리하고 **최종 발화**(=보고/응답)만 돌려준다.

        턴마다의 중간 narration은 버리고 마지막 메시지만 반환(Response가 간결). 직전 세션이
        있으면 resume로 이어간다(State 보존). 일시적 API 오류(429/5xx/529 과부하)는 백오프 재시도.
        """
        # [사전 점검 — 스테일 resume 차단] 세션이 '이 cwd의' 저장소에 없으면 스폰 전에 폐기한다.
        # 레거시 상태 파일(cwd 미기록)·cwd 불일치·저장소 유실 전부가 여기서 결정론적으로 걸러져,
        # 'No conversation found' 영구 헛돌이(라이브 12회×2 관측)가 원천 차단된다.
        if self.session_id and not self._session_in_store():
            self._reset_session()
        final_text = ""
        for attempt in range(_MAX_API_RETRY):
            try:
                final_text, captured_sid = await self._run_once(prompt)
            except Exception as e:                       # 전송/스트림 예외도 일시오류로 간주해 재시도
                final_text, captured_sid = f"API Error: {e}", None
            if captured_sid:
                self._save_session_id(captured_sid)
            # 마커 감지(이중 안전망): 사전 점검이 레이아웃 변화로 못 거른 변종이 stderr 꼬리로 잡히면
            # 같은 처리 — 세션을 버리고 즉시 새 세션으로 전진(재시도해 봐야 영원히 같은 실패라서).
            if _is_stale_session_error(final_text) and self.session_id:
                self._reset_session()
                continue
            # 정상 응답(비어있지 않고 일시오류도 아님)이면 종료. **빈 응답('')은 서브프로세스가 발화 없이
            # 조용히 죽은 신호**이므로(이게 동료가 '무응답'으로 보여 리더가 충원·재처리로 churn하던 원인)
            # 일시오류와 똑같이 resume 재시도한다. 끝내 비면 그대로 반환(무한루프 없음 — 최대 _MAX_API_RETRY).
            if final_text.strip() and not _is_transient_api_error(final_text):
                break
            if attempt < _MAX_API_RETRY - 1:
                await asyncio.sleep(2 * (attempt + 1))   # 2s, 4s 백오프
        return _strip_decoration(final_text)
