"""[Task Rule] 작업(Task) 완료·인수 검증 규칙 — 원래 설계(REWORK_DESIGN §7 rule/task.py) 복원.
잘못된 구현이 guide_tools에 병합했던 Task 완료 게이트(실제 제작자원·시각 런타임·데이터 출처·QA 검증)를
여기로 되돌린다. 전부 순수 함수(workspace/text/labels → bool/…): Organt이 complete_task로 마감을
선언할 때 SYS가 강제하는 *광역 Task Rule*. guide_tools는 이 모듈을 import해 도구에서 소비한다."""
import os
from dataclasses import dataclass, field
from typing import List

from ..protocol import TaskStatus


# 폰트·영상) 파일 확장자. 지각 비대칭 차원(특히 사운드)을 코드로 합성한 placeholder가 아니라 실제 받아온
# 자원으로 채웠는지의 **도메인 중립** 증거 — 특정 직군·장르 하드코딩이 아니라 '실재물 파일 존재'다.
_ASSET_EXTS = {
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus", ".mid", ".midi",            # 사운드
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".avif", ".tga", ".svg",    # 이미지
    ".glb", ".gltf", ".obj", ".fbx", ".dae",                                              # 3D
    ".ttf", ".otf", ".woff", ".woff2",                                                    # 폰트
    ".mp4", ".webm", ".mov", ".ogv", ".m4v",                                              # 영상
    ".aseprite", ".psd", ".xcf",                                                          # 소스 아트
}


@dataclass
class TaskRef:
    """채널에 누적되는 Task 하나 (상태블록 + 대화 Thread + 배정 팀 + 단일 책임자)."""
    task_id: str
    thread_id: str
    block_id: str
    status: TaskStatus
    team: List[int] = field(default_factory=list)   # 이 Task에 배정된 Organt들
    owner: int = 0                                   # 이 산출물의 단일 책임자(accountable)
    participated: set = field(default_factory=set)   # 이 Task 정의에 '실질 협의'로 참여한 동료(보낸/받은 쪽 모두)
    peer_info_pairs: set = field(default_factory=set)  # [협업] owner↔owner 직접 Info 교환 쌍(리더 중계 아닌 전문가
                                                     #   간 직접 대화) — 인터페이스 계약을 직접 합의했나 마감 게이트가 판정
    owner_incomplete: bool = False                   # owner가 '턴 한도'로 미완 반환 → 완료 차단(이어서 끝내야)
    owner_delivered: bool = False                    # owner가 '검증된 실작업 산출물'을 위임 도중 실제로 내고 응답이 돌아왔나
                                                     #   → 거짓이면 complete_task 거부(owner 미응답·착수전인데 리더가 대신 허위완료 차단)
    last_work_body: str = ""                         # [정밀 복구] owner에게 보낸 마지막 Work 위임의 원문 — 부팅 복구가 리더
                                                     #   재작문(드리프트: 5:13≠5:47)이 아니라 이 원본을 그대로 replay하게(SYS 이어가기에 주입)
    precise_chain_frames: list = field(default_factory=list)  # [정밀 복구(2026-06-23)] 끊긴 전체 위임 체인(active_chain)
                                                     #   — restore_chain으로 comm 스택 내부 복원 + 가장 깊은 워커부터 재개(C→B→A unwind,
                                                     #   각자 범위 보존). 평탄화(리더→C 직접, B 빠짐) 교정. 비면 종전 평탄화로 폴백.
    verified: bool = False                           # run으로 한 번이라도 실행됐나(실행 0회 완료 차단)
    work_delegated: int = 0                          # 리더가 이 Task에서 보낸 Work 위임 수(0이면 '자문만 받고 독식' 의심)
    work_delegated_to: set = field(default_factory=set)  # 이 Task에서 Work를 *실제로 받은* 멤버 집합 — '회의 발언만 하고
                                                     #   실작업 0'인 멤버가 한 번도 위임받지 못한 채 [기여 불필요]로 묵살되는
                                                     #   흡수 패턴을 마감 게이트가 막는다(참여했는데 위임 0 = 도메인 흡수 의심).
    collab_notes: str = ""                           # 회의·표결 합의 기록 — Work 위임에 자동 동봉(스펙이 회의에서 증발하던 결함 방지)
    cross_checks: int = 0                            # owner 인도 후 '다른 멤버'의 검증 참여 수(0이면 complete 1회 보류 — 품질 판정 독점 방지)
    cross_check_offdomain: int = 0                   # 그중 owner와 '다른 도메인' 검증 수(독립 검증 — 같은 직군 검증은 같은 맹점 에코)
    last_verify_writes: int = -1                      # [검증 종료상태(2026-06-23 전수감사)] 마지막 *독립(off-domain)* 검증 시점의 저작수(writes_by_role 합). 독립검증 후 코드 변경 0이면 그 검증자 재요청을 막아 무한 '최종 검증' 루프 차단(고친 뒤·첫 검증·새 검증자는 허용).
    cross_checkers: set = field(default_factory=set)  # [검증 종료상태 — 리뷰F1] 이 산출물을 독립검증한 검증자 id 집합. 재검증 dedup이 '이미 검증한 그 검증자'에게만 적용되게(검증자에게 *새 작업* 시키는 건 안 막게).
    loop_escalated: bool = False                      # [회로차단기 S1] 교차검증 임계 초과 미수렴으로 사용자에게 이미 에스컬레이트했나(1회만 — 영속).
    cc_held: int = 0                                  # 교차검증 게이트가 이 Task에서 보류된 횟수 — 3회+면 '반복 마감(독점·헛돎)' 경보로 에스컬레이트(리더가 혼자 run 반복+재마감하는 스래싱 차단; cross_check 오르면 자연 통과라 교착 없음)
    complete_retry: bool = False                     # (구) 1회 보류 시절 잔재 — 교차 검증 의무 하드화(Rule/Task 6)로 미사용, 호환 위해 유지
    leader_writes: int = 0                           # 리더가 이 Task에서 직접 쓴 파일 수(위임 없이 독식하면 차단)
    contrib_checked: bool = False                    # 팀 기여 의무 게이트(RFC-009) 1회 통과 여부 — 부른 직군이 실작업·검증 0(회의 발언만)이면 1회 보류 후 재호출 통과
    run_count: int = 0                               # 이 Task의 run 실행 횟수(체리픽 노출용)
    evidence: str = ""                               # 시스템이 직접 캡처한 마지막 run 영수증(허위보고 차단)
    acceptance: str = ""                             # [수용 계약] 팀이 합의한 '좋음(상용)'의 구체·검증가능 기준(회의 전문
                                                     #   제안 + 훌륭한 예 대비에서 도출). 마감이 각 항목 충족 증거/의식적 드롭을
                                                     #   요구 — 회의 전문성이 회의록에서 증발 않고 코드에 도달하도록 구속(라이브
                                                     #   P-015: 회의 제안 6개 중 코드 반영 0, 잠수 아닌 직군의 '구체 약속'은 무게이트).
    standard: str = ""                               # [최대화 — PHASE 1.2] 실제 exemplar 기준의 *최대* 품질 표준(외부 앵커).
                                                     #   목적함수가 '요청 문자 최소'가 아니라 '가용 외부자원으로 만들 수 있는
                                                     #   최대'임을 박는다 — 마감 검증(PHASE 3)이 이 최대 대비 갭으로 판정.
    interfaces: str = ""                             # [협업 — PHASE 1.3] 도메인 간 인터페이스 계약(데이터포맷·이벤트 타이밍).
                                                     #   분해된 sub-task를 *독립 사일로가 아니라 계약으로 연결* — 마감 검증이
                                                     #   이 계약이 실제 지켜졌나(통합/L2)를 본다(사일로·"끝에 붙이기" 차단).


def _has_real_asset(workspace) -> bool:
    """작업공간에 코드 아닌 실제 제작 자원 파일(_ASSET_EXTS)이 하나라도 있으면 True — 합성 placeholder가
    아닌 '받아온 실재 자원'의 증거. node_modules·.git은 제외(의존성 번들의 에셋은 우리 제작물 아님)."""
    import os
    if not workspace:
        return False
    try:
        for root, dirs, files in os.walk(str(workspace)):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__", ".cache", "dist", "build")]
            for f in files:
                if os.path.splitext(f)[1].lower() in _ASSET_EXTS:
                    return True
    except Exception:
        return False
    return False


def _has_visual_runtime(workspace) -> bool:
    """작업공간이 사용자가 *화면으로 보는* 웹 UI를 렌더하나 — .html 진입점이 있으면 True. 시각 산출물은
    presence·로직 검증만으론 부족하다(실제 렌더가 헤드리스 WebGL/GPU에서 검게/빈 화면으로 나올 수 있음 —
    라이브 P-003). 특정 장르·직군 하드코딩 아님(웹 UI = 사용자가 봄 = 시각 차원)."""
    import os
    if not workspace:
        return False
    try:
        for root, dirs, files in os.walk(str(workspace)):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__", ".cache", "dist", "build")]
            if any(f.lower().endswith(".html") for f in files):
                return True
    except Exception:
        return False
    return False


# [지각-필수(오디오) 차원 탐지 — percept '탐지→강제' 강화(2026-06-19, 항목 75 후속)] percept 게이트는
# 'LLM 검증자가 지각 못 하는 차원'(특히 *들어야* 아는 소리·음악 — 시각은 스크린샷으로 검증 가능)에 실제
# 에셋을 요구한다. 그런데 탈출구 `[지각차원 없음]`이 *무조건* 통과라, 사운드 직군을 둔 게임조차 '없음'으로
# 거짓 선언해 코드 합성 placeholder를 통과시킬 수 있었다(detect-not-enforce — 라이브 P-010 재발 경로).
# 그래서 '이 작품에 *오디오* 지각차원이 정말 있는가'를 *팀 자신의 신호*로 탐지한다 — ① 이 Task 팀에
# 사운드/음악 전문가가 배치됐거나(리더가 그 차원이 중요하다고 직접 채용) ② 팀이 합의한 기준(goal·
# acceptance·standard)이나 사용자 원문이 소리·음악 품질을 명시. 탐지되면 빈 '없음' 선언을 막고(실제 음원
# 통합 또는 *사유 있는* 명시 요구), 아니면 종전대로 가벼운 탈출. 데이터출처 게이트(_wants_real_data)와 같은
# '요구가 그 차원을 부를 때만 강제' 패턴 — 도메인('games') 하드코딩이 아니라 팀 자신의 직군·기준으로 판정.
_PERCEPT_AUDIO_HINTS = ("사운드", "음악", "음향", "효과음", "배경음", "오디오",
                        "sound", "audio", "music", "bgm", "sfx")
def _perceptual_essential(labels, texts) -> bool:
    """이 작품에 'LLM이 지각 못 하는 오디오 차원'이 정말 있는가 — 팀이 그 직군을 두었거나(라벨) 합의
    기준·원문이 소리·음악을 명시(텍스트)하면 True. 도메인 중립(팀 자신의 신호로 판정)."""
    hay = " ".join([str(x).lower() for x in (labels or [])] +
                   [str(t).lower() for t in (texts or [])])
    return any(k in hay for k in _PERCEPT_AUDIO_HINTS)


# [데이터 출처 검증 — percept 게이트의 '데이터' 평행판(2026-06-18, 라이브 P-021)] '공공/실데이터로
# AI를 학습'하라는 요청에서, 데이터를 받지 않고 *지어낸*(합성·하드코딩) 분포로 학습시키고 완성으로
# 내는 것을 막는다. percept가 '합성 placeholder 에셋'을 막듯, 이건 '합성 placeholder 데이터'를 막는다
# (P-021: 국토부 실거래가를 안 받고 가격을 공식+노이즈로 합성 → 'MAE 성공'이 순환논리 = 요구 위반).
# 도메인 중립(요청이 real/public 데이터를 요구할 때만 발동), 의식적 명시 탈출구 상시.
_DATA_FILE_EXTS = {".csv", ".tsv", ".parquet", ".xlsx", ".xls", ".feather", ".arrow", ".jsonl"}
_SYNTH_MARKERS = ("합성 데이터", "합성데이터", "synthetic data", "synthetic_data", "더미 데이터",
                  "dummy data", "가짜 데이터", "fake data", "임의 생성", "랜덤 생성", "데이터 합성",
                  "generate_price", "generate_data", "make_synthetic", "fabricat", "mock data")


def _wants_real_data(text) -> bool:
    """요청/목표가 '실제·공공 데이터로 학습'을 요구하는가 — 데이터 출처 게이트의 발동 조건(도메인 중립)."""
    t = str(text or "").lower()
    real = any(k in t for k in ("공공데이터", "공공 데이터", "공개 데이터", "공개데이터", "오픈 데이터",
                                "오픈데이터", "open data", "실데이터", "실 데이터", "실거래", "real data",
                                "real-world", "공공 api", "data.go.kr", "공공 데이타", "공공데이타"))
    learn = any(k in t for k in ("학습", "train", " ai", "모델", "model", "예측", "predict", "데이터셋", "dataset"))
    return real and learn


def _has_real_dataset(workspace) -> bool:
    """작업공간에 '받아온 실제 데이터 파일'(csv/parquet 등, 빈 스텁 아님)이 있는가 — 합성이 아닌 증거."""
    import os
    if not workspace:
        return False
    try:
        for root, dirs, files in os.walk(str(workspace)):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__", ".cache", "dist", "build")]
            for f in files:
                if os.path.splitext(f)[1].lower() in _DATA_FILE_EXTS:
                    try:
                        if os.path.getsize(os.path.join(root, f)) > 2048:   # 빈/스텁 파일은 증거 아님
                            return True
                    except OSError:
                        return True
    except Exception:
        return False
    return False


def _synthesizes_data(workspace):
    """작업공간 코드(.py/.js/.ts)가 학습 데이터셋을 '지어내는'(합성·더미) 흔적 — (파일명, 표식) 또는 None."""
    import os
    if not workspace:
        return None
    try:
        for root, dirs, files in os.walk(str(workspace)):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__", ".cache", "dist", "build")]
            for f in files:
                if os.path.splitext(f)[1].lower() not in (".py", ".js", ".ts", ".mjs"):
                    continue
                try:
                    txt = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read().lower()
                except OSError:
                    continue
                for m in _SYNTH_MARKERS:
                    if m in txt:
                        return (f, m)
    except Exception:
        return None
    return None


# [검증 역할(QA) 식별 — 기능 기반, 타이틀 하드코딩 아님(2026-06-19, 사용자 설계: 'QA=최종 검증 역할')]
# 시스템은 직군을 우대하지 않지만(도메인 중립), '검증/품질'은 *도메인*이 아니라 *기능*이다 — 산출물 전체를
# 사용자 관점에서 처음부터 끝까지 써보는 '최종 인수'는 만든 사람의 저자편향 밖에 있어야 하고, 그 기능에
# 특화된 역할(QA)이 자연히 담당한다. 그 역할을 능력 키워드로 식별해 *홀리스틱 최종 검증을 우선 라우팅*한다
# (부분·기술 검증은 도메인 동료도 가능 — QA는 전체·최종에 우대). 직군 '선택'을 박는 게 아니라 '검증 기능'을 알아본다.
_VERIFIER_HINTS = ("qa", "검증", "품질", "테스트", "테스터", "quality", "tester", "verif", "정합성")

# [회로차단기 임계(2026-06-23 협업재설계 S1)] 교차검증 N회+ 미수렴 = 루프 → 사용자 1회 에스컬레이트(advisory).
# 봇은 '해결 불가'(플랫폼 한계 등)를 스스로 판정 못 해 무한 검증하므로, 시스템이 메타인지를 대신해 사람에게 넘긴다.
_LOOP_ESCALATE_CROSS = int(os.environ.get("ORGANT_LOOP_CROSS", "12"))


def _is_verifier(label) -> bool:
    """역할 라벨이 '검증/품질(QA) 기능'인가 — 전체·사용자관점 최종 인수의 자연 담당."""
    t = str(label or "").lower()
    return any(h in t for h in _VERIFIER_HINTS)


# ── [Task 체크포인트 — guide_tools에서 이관] 흐름의 Task 상태를 크래시-세이프 영속 ──
def _ckpt(flow):
    """[크래시-세이프 Task 체크포인트] Task 전이(생성·목표확정·owner 확정·마감)마다 미완 Task를
    레지스트리에 영속한다 — 종전엔 흐름 '종료'에만 써서, 동면·강제종료처럼 마감 코드가 못 도는
    죽음이면 진행 중 Task의 정체(블록·스레드·owner·Goal)가 유실돼 복구가 '같은 Task 이어가기'가
    아니라 '새 Task'로 시작했다(라이브 관측 — 사용자 지적). 콜백은 SYS가 주입(미주입이면 무해)."""
    fn = getattr(flow, "checkpoint_task", None)
    if fn:
        try:
            fn()
        except Exception:
            pass


async def create_task(flow, args):
    """[Task Rule 로직] create_task — 빈 Task 껍데기를 열고 팀 배정. @tool 래퍼가 _ok로 감쌈(평문 반환).
    flow는 duck-typed(guide·current·project_*·pool·leader·tasks·comm·next_task_id 등)."""
    from .communication import _resolve_members, _uniq, _is_spare, _group_of, _add_members
    g = flow.guide
    if flow.current is not None and flow.current.status.status != "완료":
        return (f"현재 Task({flow.current.task_id}: {(flow.current.status.purpose or '미정')[:24]})가 아직 "
                   f"'진행'입니다 — 단일흐름은 한 번에 Task 하나만. complete_task로 먼저 마감한 뒤 "
                   f"다음 Task를 여세요(여러 산출물도 하나씩 순차로).")
    ch = flow.project_channel or flow.user_channel
    tid = flow.next_task_id()
    pool = flow.project_team or flow.pool
    picked = _resolve_members(args.get("members", ""), flow, pool)
    # 팀은 담당자(리더)가 '일에 맞게' 동적으로 고른다 — 자동 전원 소집 아님. members=로 필요한 직군만
    # 지정하면 그들로. 비우면 기본 팀은 **직군당 1명**(실행 핵심)으로 둔다 — [팀 비대 차단, 라이브
    # 2026-06-14: 역할 드리프트(과거 recruit가 Discord 역할로 영속)로 백엔드 5명 등이 기본 팀에 다
    # 들어와, set_goal '전원 협의' × 비대 = meet 4회·6 잠수·override 노이즈·136분 미수렴]. 같은 직군
    # 중복은 협의·게이트 비용만 키우므로(Brooks: 소통비용~인원²) 기본에서 빼고, 정말 병렬 일손이
    # 필요하면 recruit/members=로 더한다(리더 자율). 매직넘버 아님 — '한 도메인 한 책임자'는 이미
    # 시스템의 단일-owner 보편 이치. set_goal은 '이 (슬림한) 팀 전원' 협의로 통과.
    if picked:
        base = picked
    else:
        base, _seen = [], set()
        for m in flow.project_team:
            if m == flow.leader or _is_spare(flow, m):
                continue
            r = (flow._info(m) or "").strip()
            if r and r in _seen:
                continue        # 같은 직군 중복은 기본 팀에서 제외(recruit로 추가 가능)
            _seen.add(r)
            base.append(m)
    team = _uniq([flow.leader] + base)
    # 'PM 혼자 Task' 차단(구조): 프로젝트에 직군 동료가 있는데 리더 혼자만 멤버로 여는 건 팀을 버리고
    # 단독작업·독식하는 패턴(사용자가 본 'PM 혼자 있는 Task'). 동료가 무응답이라고 새 솔로 Task로 도망가지
    # 말 것 — 그건 '환경 불안정'이니 사용자에게 보고하고 멈춰야 한다. 진짜 1인 프로젝트(동료 없음)·개입은 허용.
    others = [m for m in flow.pool if m != flow.leader and not _is_spare(flow, m)]
    if team == [flow.leader] and others and not getattr(flow, "intervention", None):
        return (f"단독 Task 거부: 이 프로젝트엔 동료({flow._names(others)})가 있는데 당신 혼자만 멤버인 "
                   f"Task는 열 수 없습니다(팀 버리고 단독작업·독식 금지 — 사용자가 지적한 'PM 혼자 Task'). "
                   f"일에 맞는 동료를 members로 넣어 함께 하세요. 동료가 모두 응답 불능이면 새 솔로 Task로 "
                   f"넘어가지 말고 '환경(인프라) 불안정으로 일시 중단'을 사용자에게 보고하고 멈추세요.")
    # Purpose·Goal·Owner 모두 비워둔다 — 빈 껍데기. Purpose·Goal은 배정된 팀이 모여 set_goal로 정하고,
    # Owner는 Work-request 수신으로 떠오른다(리더가 할 일·목표·담당을 미리 박던 중앙집권 제거).
    status = TaskStatus(task_id=tid, purpose="", status="진행",
                        goal="", owner="", group=_group_of(flow, team))
    block_id, thread_id = await g.open_task(ch, status)
    await _add_members(g, thread_id, [m for m in team if m != flow.leader])  # 멤버십=팀
    flow.project_channel = ch
    ref = TaskRef(task_id=tid, thread_id=thread_id, block_id=block_id,
                  status=status, team=team, owner=0)   # participated는 빈 set에서 시작(Task별 협의 추적)
    flow.tasks.append(ref)
    flow.current = ref
    flow.comm.reset_task_tracking()   # 새 산출물 단위 → '완료/Redo' 추적 초기화(Redo는 같은 Task 안에서만)
    _ckpt(flow)                       # 크래시-세이프: 열린 즉시 영속(동면·강제종료에도 같은 Task로 복구)
    # [공급 원칙 — RFC-005 / 매직넘버 제거(사용자 원칙 2026-06-13)] '소통 비용은 인원²'은
    # 보편 이치(Brooks)지만 '6명+'라는 트리거는 임의값(4항목과 같은 부류). 크기 임계를 빼고
    # '실행 핵심만 + 나머지는 검증·자문' 분리를 크기 무관 질적 조언으로 — 판단은 리더.
    size_note = ("\n[정보 — 판단은 당신 몫] 실행(직접 구현)에 꼭 필요한 핵심만 owner로 두고, 나머지 "
                 "전문가는 검증·자문(request Info)으로 두는 편이 좋습니다 — 소통·조율 비용은 실행 "
                 "인원이 늘수록 가파르게 커집니다(필요 이상 큰 실행 팀은 비효율). 회의·검증엔 전원, "
                 "실행엔 핵심만.")
    return (f"task={tid} (빈 껍데기·담당자가 팀 선정) thread={thread_id} 팀={flow._names(team)}{size_note} — 이 팀은 "
               f"당신이 고른 구성입니다(직군이 부족하면 recruit(role=)로 더하세요). 배정된 팀과 **meet(회의)로 "
               f"'Purpose(풀 문제)·Goal(성공기준)·각자 도메인 할 일'을 함께 정한 뒤** set_goal로 확정하세요 — "
               f"meet은 독립의견을 동시에 모으고(앵커링 방지) 토론·회의록(합의)까지 남깁니다(1:1 request(Info)를 "
               f"여러 번 도는 것보다 합의가 또렷하고 빠름 — 개별 후속 확인만 Info로). 전원 협의 전엔 set_goal "
               f"거부됨. 그 다음 일을 맡길 동료에게 Work로 위임.")


async def set_goal(flow, me_id, role, args):
    """[Rule 로직] set_goal — guide_tools에서 이관(평문 반환, @tool이 _ok 래핑)."""
    from .._util import _ok, _speech_clip
    from .communication import _capability_gaps, _fork_collect, _jobs_of, _needed_caps_coverage, _norm_job
    import re
    g = flow.guide
    if flow.current is None:
        return _ok("오류: 진행 중인 Task가 없습니다. create_task로 먼저 여세요.")
    goal = (args.get("goal") or "").strip()
    purpose = (args.get("purpose") or "").strip()
    if not goal:
        return _ok("오류: goal이 비었습니다.")
    # Purpose·Goal은 '담당 팀이 함께' 정한다(docs: Task.Team이 Goal을 정한다). 이 Task 멤버 전원이
    # '실질 협의(participated)'에 참여했는지 검사 — 리더가 물었든 peer끼리 물었든 인정(허브 완화),
    # 단 빈 핑('응답 가능?')은 불인정(실질 강제). → 매 Task를 팀이 모여 정하는 분산 구조를 구조적으로 보장.
    # [유화적 전원협의 — 무한 루프 차단] '전원'을 글자 그대로 강제하면, 한 멤버가 그 시간 내내 타 흐름에
    # 점유(1봇=1흐름 배타 — 병렬 안전 기둥)되어 도달 불가할 때 set_goal이 영영 거부돼 교착한다(라이브
    # P-002 114305-1: 프론트 4명 중 1명이 내내 P-013을 '리드'(set_goal까지) 중 → 협의 33회 거부 →
    # 200분 미수렴·스킬 코드 0줄). 교정: '지금 가용(reachable)'한 미참여 멤버에게만 협의를 요구(최대한
    # 다 받기 — 가용한 사람은 전원)하고, '타 흐름 점유로 도달 불가'한 멤버는 면제하고 진행한다 —
    # _fork_collect의 '부분 조인'(일부 점유 멤버 때문에 수집 전체가 막히지 않음)과 같은 정신. 면제는
    # '포기'가 아니라 '실제 부재 인정'이며, 면제 멤버의 도메인 공백은 리더에게 정보로 돌려준다(필요하면
    # 같은 직군 recruit — 판단은 리더, 공급 원칙).
    # [합의 = 직군 커버리지 (동질 모델 원리) + 유화적 면제 — 무한 루프 차단]
    # 같은 Claude·같은 직군 봇 둘은 0 다양성(에코)이라 합의엔 *직군당 1명*이면 그 도메인 관점이 곧 전부다.
    # 라이브: meet 57%가 같은 직군 중복(백엔드×3) → 에코·합의 편향(한 시각 N배 가중)·과대소집. 그래서
    # '전원 참여'가 아니라 '도메인 커버리지'를 요구한다 — 같은 직군 잉여는 합의 면제(그들은 *병렬 실행*용).
    # 타 흐름 점유로 도달 불가한 직군도 면제(교착 차단). 둘 다 글자 그대로의 '전원'이 만든 에코·교착을
    # 푼다. 직군 못 읽는 라벨(예비 등)은 각자 고유 도메인으로 보아 보수적으로 참여를 요구(은근 면제 방지).
    members = [x for x in flow.current.team if x != me_id]
    excused_note = ""
    if members:
        eng, scope = flow.comm.engagement, flow.comm.scope
        def _busy_now(m):
            return bool(eng is not None and scope is not None and eng.busy_elsewhere(m, scope))
        def _doms(m):
            return ({_norm_job(j) for j in _jobs_of(flow._info(m) or "")} - {""}) or {f"·{m}"}
        dom_members = {}                       # 직군 → 그 직군 팀 멤버들
        for m in members:
            for d in _doms(m):
                dom_members.setdefault(d, []).append(m)
        uncov_reach = {}                       # 미커버 직군 → 가용 멤버(합의 필요·가능)
        uncov_busy = []                        # 미커버 직군 — 대표가 지금 타 흐름 점유(도달 불가)
        redundant = []                         # 같은 직군 잉여(이미 커버) — 에코, 면제
        for d, ms in dom_members.items():
            if any(x in flow.current.participated for x in ms):
                redundant += [x for x in ms if x not in flow.current.participated]
                continue
            reach = [x for x in ms if not _busy_now(x)]
            if reach:
                uncov_reach[d] = reach
            else:
                uncov_busy.append(d)   # 이 도메인 대표가 전부 타 흐름 점유 — 아래 '점유 도메인'에서 처리
        if uncov_reach:
            one_each = [r[0] for r in uncov_reach.values()]   # 직군당 1명 예시
            tail = (f"\n(점유 도메인 {', '.join(sorted(uncov_busy))}은 아래 '점유 도메인' 안내를 따르세요)"
                    if uncov_busy else "")
            return _ok(f"확정 거부: 이 Task의 Purpose·Goal은 담당 팀이 함께 정합니다(리더 독단·선지정 금지). "
                       f"아직 합의에 참여 안 한 **도메인**: {', '.join(sorted(uncov_reach))} — 각 도메인 "
                       f"**1명만** meet(회의)로 '풀 문제·목표·성공기준'을 정하면 됩니다(같은 직군을 더 부르는 건 "
                       f"*에코*라 불필요 — 잉여는 병렬 실행용). 예: {flow._names(one_each)}. 파일·엔드포인트 "
                       f"같은 구현 스펙 말고 '측정가능한 결과'로.{tail}")
        # [점유 도메인 — 대기 우선; 무시(묵살)·대체 증원 금지] 어떤 도메인의 대표가 타 흐름에서 일하는
        # 중이면, *침묵 면제(의견 묵살)*도 *대체 인력 증원(기억 없는 복제)*도 답이 아니다 — 그 도메인의
        # 기억을 가진 본인이 제일 낫다(1봇1직업1기억). 점유는 좀비 수정으로 *일시적·유한*이라 곧 풀린다.
        # 1회 보류로 둘 중 하나를 의식적으로 택하게 한다: ①(권장) 그가 풀리면 합류시켜 마무리(대기),
        # ② 결론이 그 도메인 없이도 *명확히* 닫혔으면 재호출해 확정하되 그는 실행에서 자기 도메인을
        # 직접 만든다(타 직군 가짜 대체 금지). 모호하면 ①. 사용자 설계: '정확하면 반출, 모호하면 대기'.
        if uncov_busy and not getattr(flow, "busy_consensus_held", False):
            flow.busy_consensus_held = True
            if flow.log:
                flow.log("set_goal_busy_consensus_hold", task=flow.current.task_id,
                         domains=sorted(uncov_busy))
            return _ok(f"확정 보류(점유 도메인 — 1회): 도메인 **{', '.join(sorted(uncov_busy))}**의 대표가 "
                       f"지금 타 흐름에서 일하는 중입니다. 임시로 바쁘다고 그 도메인을 *무시하거나*, "
                       f"*대체 인력을 새로 만들지* 마세요 — 그 도메인의 기억을 가진 본인이 제일 낫습니다. "
                       f"둘 중 하나를 의식적으로 택하세요: **①(권장) 그 전문가가 풀리면 회의에 합류시켜 "
                       f"마무리** — 점유는 일시적이라 곧 풉니다(그때까지 가용 멤버와 회의를 이어가며 대기). "
                       f"**② 회의 결론이 그 도메인 없이도 *명확히* 닫혔으면** 그 점이 goal에 드러나게 적고 "
                       f"재호출해 확정 — 단 그 전문가는 *실행 단계에서 자기 도메인을 직접* 만듭니다(타 직군 "
                       f"가짜 대체 금지). **결론이 모호하면 ①(대기)이 맞습니다.**")
        if redundant or uncov_busy:
            if flow.log:
                flow.log("set_goal_consensus_coverage", task=flow.current.task_id,
                         redundant=[int(x) for x in redundant], uncovered_busy=sorted(uncov_busy))
            bits = []
            if redundant:
                bits.append(f"같은 직군 잉여 {flow._names(redundant)}는 합의 면제(에코 방지) — 이들은 "
                            f"**병렬 실행(parallel_work)**에 쓰세요(같은 직군의 유일한 정당한 가치는 합의 "
                            f"인원수가 아니라 병렬 처리량)")
            if uncov_busy:
                bits.append(f"점유 도메인 {', '.join(sorted(uncov_busy))}은 ②(의식적 진행) — 그 전문가가 "
                            f"*실행에서 자기 도메인을 직접* 만들어야 합니다(가짜 대체 금지)")
            excused_note = "\n[합의 커버리지] " + "; ".join(bits) + "."
    # [P7 — 범주적 완성 점검: recognition→action 강제, RFC-010] 확정 전 1회, 장르 예시 대비 '통째로
    # 없는 범주'를 goal에 '구축 대상'으로 반영(없으면 recruit)하거나 불필요 사유를 명시하게 강제한다 —
    # 라이브: P6 넛지로 사운드를 grep '점검'만 하고 구현 0(인지≠행동). 점검을 '구축'으로 한 칸 올림.
    # 1회 보류 후 재호출 통과(막지 않되 의식적 결정 — override 게이트와 같은 정신). 직군 키워드 하드코딩
    # 없음 — 장르·범주 판단은 리더(비체험형이면 'N/A 불필요'로 재호출). set_goal_gap_check 로그로 가시화.
    # [최대화 — 구조적 강제(2026-06-20 사용자 "프롬프트 의존 제거")] 종전엔 '흐름당 1회 보류→재호출
    # 통과'라 standard 기록 없이도 통과 → '최소 구현 통과'의 출처(품질 바가 *안 박힘*). 이제 *standard
    # (최대 표준)가 실제로 기록될 때까지* 보류한다 — flow.current.standard(per-Task 필드)+이번 인자로
    # 키잉해 새 Task마다 다시 요구(per-flow 플래그 아님 → 다중-Task에서도 매번 발동). 의식적 면제는
    # '[최대화 N/A: 사유]'(사유 필수). gap_checked는 *테스트 우회 플래그*로만 유지(프로덕션은 standard 유무로 판정).
    _has_std = bool((flow.current.standard or "").strip() or (args.get("standard") or "").strip())
    _max_na = bool(re.search(r"\[\s*최대화\s*(?:N\s*/?\s*A|면제|불필요)\s*[:：]\s*\S",
                             goal + "\n" + (args.get("standard") or "")))
    if not getattr(flow, "gap_checked", False) and not _has_std and not _max_na:
        if flow.log:
            flow.log("set_goal_gap_check", task=flow.current.task_id)
        return _ok("확정 보류(최대화 기준 — 최대판 *구성요소 분해* 기록 강제, 재호출만으론 통과 안 됨): 이 "
                   "시스템의 전제는 '요청을 문자 그대로 최소로'가 아니라 '가용 외부자원으로 만들 수 있는 "
                   "**최대** 품질'입니다. **이 작품과 같은 종류의 *실제 훌륭한 예*를 WebSearch로 찾아**(상상 "
                   "말 것 — LLM은 자기 산출을 기준 삼아 '평범=충분'으로 수렴하니 실제 레퍼런스가 외부 기준), "
                   "그 **최대판이 당연히 갖춘 *구성요소를 분해***해 **`standard`에 *체크 가능한 항목 목록*으로 "
                   "적으세요** — 모호한 한 문장이 아니라 *부품 목록*(예: 핵심기능 A·B·C, 상태·엣지 처리, "
                   "손맛·완성도 요소 …). **그리고 *기능 나열*에 그치지 말고 '**주 사용 흐름(실사용성)**'도 "
                   "분해에 넣으세요 — *진짜 사용자가 핵심 목표를 어떻게 달성하나*. 최고 앱은 위치·맥락 기반이면 "
                   "**자동감지·기본값·원탭**으로 주 경로 마찰을 없앱니다(예: 열면 *내 위치 결과 즉시* — 지역을 "
                   "수동으로 하나하나 고르는 다단계 폼이 아니라). **수동으로 일일이 설정하게 만들면 기능이 "
                   "완비돼도 *실사용 실패*입니다**(라이브 관측: 위치기반인데 자동위치 0·수동 select 강제). "
                   "접근성(라벨·키보드)도 실사용성의 일부. 마감은 '좋은가?'(홀리스틱이라 satisfice됨)가 아니라 "
                   "**'최대판 부품이 다 있나·각 부품이 얼마나 깊나·*주 사용 경로가 마찰 없나*'를 *항목별로* "
                   "대조**합니다(구성적 품질·사용흐름은 보지 않아도 "
                   "구성요소로 분석 가능 — taste 아님). *리더 혼자 정하지 말고* 각 도메인이 자기 분야 실제 "
                   "훌륭한 예를 대조해 *자기 도메인 최대 부품*을 meet로 기여(합집합 누적 — 1명 지능에 인질 "
                   "금지). 정말 이 작품엔 최대화할 차원이 없으면 goal이나 standard에 **'[최대화 N/A: "
                   "<사유>]'**를 적어 재호출하세요(빈 재호출은 통과 안 됨 — 사유 필수).")
    # [스태핑 커버리지 강제 — 리더 흡수 차단(2026-06-19, 사용자: '전문가 분배 무조건, 리더는 자기 직군만')]
    # consensus-coverage 게이트처럼 persistent-until-resolved: 목표가 명시적으로 부른 전문 능력을 팀(리더
    # 포함)이 *아무도* 보유 못 했으면 확정 보류 → recruit로 그 전문가를 투입해야 통과(채우면 갭이 사라져
    # 자동 통과). 그러면 그 도메인에 owner가 박혀 기존 #4 게이트가 리더를 자기 직군에 가둔다(언더스태핑
    # 탈출구를 닫는다). 능력 식별은 기능 기반(직군 하드코딩 아님). 정말 리더 역량으로 커버한다고 판단하면
    # goal/acceptance에 '[스태핑 면제: <이유>]'로 의식적 면제(무한 루프 차단 — 탈출구 상시).
    _staff_exempt = (getattr(flow, "staffing_exempt", False)
                     or "[스태핑 면제" in goal or "[스태핑 면제" in (args.get("acceptance") or ""))
    if not _staff_exempt:
        _labels = [flow._info(m) for m in flow.current.team] + [flow._info(flow.leader)]
        _gaps = _capability_gaps(
            " ".join([goal, purpose, str(getattr(flow, "origin_request", "") or "")]), _labels)
        if _gaps:
            if flow.log:
                flow.log("set_goal_staffing_gap", task=flow.current.task_id, gaps=_gaps)
            return _ok(
                f"확정 보류(스태핑 커버리지 — 전문가 분배 강제): 이 목표는 **{', '.join(_gaps)}** 능력을 "
                f"요구하는데 팀(당신 포함)에 그 전문가가 없습니다. **리더(당신)는 자기 직군 일만** 합니다 — "
                f"그 도메인을 흡수해 직접 하지 마세요. **recruit(role='해당 전문 직군')으로 그 전문가를 "
                f"투입**(예비 인력이 그 직군으로 채용됨)한 뒤 그에게 위임하세요. 투입하면 그 도메인에 owner가 "
                f"생겨 분배가 강제되고, set_goal은 자동 통과합니다(갭 사라짐). 정말 *당신 직군 역량으로* 그 "
                f"능력까지 커버한다고 판단하면 goal에 **'[스태핑 면제: <이유>]'**를 적어 재호출하세요(의식적 "
                f"판단 — 그냥 재호출로는 통과 안 됨).")
        # [협업 깊이 — 핵심 능력 복수 검토(2026-06-22 사용자: '중요한 직군은 2명, 상호 같은직군 토론')]
        # staffing 통과(필요 능력 갭 0) 후: 필요 능력이 *전부 1명뿐*이면 그 도메인 품질이 한 사람 지능에
        # 인질이다(P-018식 1인 의존). 가장 중요한 능력 1개는 2명으로 채워 상호 검토(peer review)·병렬·
        # 동일직군 토론이 일어나게 강제. 어느 능력을 깊게 갈지는 리더 판단(시스템이 '핵심'을 지정 안 함) —
        # *한 능력이라도 2명*이 되면 통과(과채용을 +1봇으로 한정, '백엔드 6명' 재발 차단). 의식적 면제는
        # '[심도 단독: <능력> — <사유>]'. 능력표 밖 도메인(게임 등)엔 _cov가 비어 발동 안 함(과발동 차단).
        _depth_na = bool(re.search(r"\[\s*심도\s*단독[^\]]*[:：]\s*\S{2,}",
                                   goal + "\n" + (args.get("acceptance") or "")))
        if not _depth_na:
            _cov = _needed_caps_coverage(
                " ".join([goal, purpose, str(getattr(flow, "origin_request", "") or "")]), _labels)
            if _cov and all(n == 1 for n in _cov.values()):
                if flow.log:
                    flow.log("set_goal_depth_gap", task=flow.current.task_id, caps=list(_cov.keys()))
                return _ok(
                    f"확정 보류(협업 깊이 — 핵심 능력 복수 검토): 이 목표의 핵심 능력"
                    f"(**{', '.join(_cov.keys())}**)이 *각 1명뿐*입니다 — 같은 한 사람의 지능에 그 도메인 "
                    f"품질이 인질이 됩니다(상용 품질의 천장). **가장 중요한 능력 1개**는 recruit로 **2명**으로 "
                    f"채워 *상호 검토(peer review)·병렬·동일직군 토론*이 일어나게 하세요(어느 걸 깊게 갈지는 "
                    f"당신 판단 — 한 능력이라도 2명이 되면 통과). 정말 1명으로 충분하면 goal이나 acceptance에 "
                    f"**'[심도 단독: <능력> — <사유>]'**를 적어 재호출하세요(의식적 — 빈 태그·그냥 재호출은 "
                    f"통과 안 됨).")
    # [병렬 강제 제거 — 단일흐름 안정성(2026-06-22 사용자 결정)] 병렬 fork 경로가 작업공간 cwd·게이트#9·
    # 쓰기리스로 Write를 잃어 산출물 0 churn을 유발했다(P-029 규명). '흐름을 최대한 하나로 유지하며
    # 안정성'이 전제이므로 병렬 강제 게이트를 걷어낸다 — 협업은 직렬(request)+meet로. parallel_work도 비활성화.
    # [단일-Task 깊은 수렴 — 분해 강제 제거(2026-06-18)] 종전엔 다도메인 목표를 '도메인별 Task로
    # 쪼개라'고 밀었으나(분해 점검 게이트), 라이브 규명: P-002 114305-1의 '한 owner가 5도메인'은
    # *구조* 문제가 아니라 전문가 8명 idle(참여 문제)이었고, P-016(216 대화·단일 Task)이 보여주듯
    # 단일 Task가 *이미 다중 검증*(자기검증·QA·교차검증)을 지원한다. 분해는 그 깊은 수렴을 조각냈다
    # (P-019: AI가 백엔드 서브태스크로 떨어져 가짜로 격하). 그래서 분해 강제를 걷어낸다 — 다도메인은
    # 한 Task에서 깊게 수렴하고, 검증 깊이는 acceptance·percept·교차검증(아래 마감 게이트)이 책임진다.
    if purpose:
        flow.current.status.purpose = purpose
    flow.current.status.goal = goal
    acceptance = (args.get("acceptance") or "").strip()
    if acceptance:
        # [수용 계약 포착] 회의 전문 제안을 '구속력 있는 구체 기준'으로 박제 — 마감 게이트가 항목별 충족을
        # 요구한다. 누적(개입 때마다 덮어쓰지 않고 이어붙임)해 회차가 쌓일수록 기준이 두꺼워진다.
        prev = (flow.current.acceptance or "").strip()
        flow.current.acceptance = (prev + "\n" + acceptance).strip() if prev and acceptance not in prev else (acceptance or prev)
    standard_in = (args.get("standard") or "").strip()
    if standard_in:
        # [최대화 + 분산 — 핵심] 실제 exemplar의 *최대* 표준을 박되, *리더 단독*이 아니라 각 도메인이 자기
        # 분야 최대 기준을 기여한 *합집합*으로 누적한다(acceptance와 같은 누적 — 회차마다 이어붙임). 한
        # 오케스트레이터의 지능이 전체 품질 바를 혼자 정하면 그게 곧 '품질이 1명에 인질'(사용자 명제).
        prev = (flow.current.standard or "").strip()
        flow.current.standard = (prev + "\n" + standard_in).strip() if prev and standard_in not in prev else (standard_in or prev)
        if flow.log:
            flow.log("set_goal_standard_set", task=flow.current.task_id, chars=len(flow.current.standard))
    interfaces_in = (args.get("interfaces") or "").strip()
    if interfaces_in:
        # [협업 — PHASE 1.3] 도메인 간 인터페이스 계약 박기(사일로 방지). 마감 검증(L2)이 이 계약 준수를 본다.
        flow.current.interfaces = interfaces_in
    await flow.refresh(flow.current)
    _ckpt(flow)                       # 크래시-세이프: 확정된 Purpose·Goal 영속
    # [공급 원칙 — 정보는 구조가, 판단은 리더가. 매직넘버 제거(사용자 지적 2026-06-13)]
    # 종전엔 'goal 4항목+'(항목 수)로 분해를 트리거했으나, 항목 '수'는 표면 프록시다 — RFC-008이
    # 경고한 측정의 함정(Goodhart)의 재발이었다. 분해 필요성의 본질은 개수가 아니라 '독립적으로
    # 구현·검증할 단위로 나뉘는가'(응집도/검증 단위). 숫자 게이트 없이 질적 기준만 공급하고 판단은
    # 리더(LLM)에게 — 검증·마감 게이트가 부분마다 생겨 결함이 일괄 통과되지 않는다(P-009 교훈).
    tip = ("\n[정보 — 판단은 당신 몫] 이 goal이 **서로 독립적으로 구현·검증할 수 있는 여러 부분**을 "
           "담고 있으면(개수가 아니라 '독립성'이 기준), 부분마다 Task로 나눠(create_task→위임→검증→"
           "complete_task 반복) 각각 마감하는 것을 고려하세요 — 한 Task로 가면 검증·마감이 1회뿐이라 "
           "부분 결함이 묻힙니다(라이브 P-009). 깊게 얽혀 한 덩어리면 굳이 나누지 마세요.")
    # [RFC-008 P0 — 품질/기능 분리] 측정 가능한 기능만 goal에 담으면 측정 어려운 품질이 빠진다
    # (Holmström-Milgrom 다중작업: 측정가능한 것만 보상 → 품질 이탈이 최적). 기능 체크리스트와
    # 별도로 '이 도메인의 훌륭함'(완성도·UX·재미)을 품질 차원으로 의식하게 — 정의 불가한 품질도
    # 부분 operationalize는 가능(Graham). 강제 아닌 공급(암묵지라 다 못 적음 — Polanyi).
    team_roles = [r for r in flow._names([m for m in flow.current.team if m != me_id]) if r]
    qbar = ("\n[품질 차원 — '되는가'≠'배포할 만한가'] 측정가능한 기능만 goal에 담으면 측정 어려운 품질"
            "(완성도·UX·재미·연출)이 빠집니다(라이브: 작동하나 폴리시 0인 게임). 지금 두 가지를 의식하세요: "
            f"① **이 팀 구성에서 품질 축을 유도** — 팀의 각 직군({', '.join(team_roles) or '동료'})이 "
            "*자기 도메인에서 '훌륭함'으로 치는 기준*이 곧 '완성'의 축입니다(무엇이 훌륭함인지는 그 직군이 "
            "정의 — 시스템이 특정 항목을 박지 않음). 이 품질 기대치를 **acceptance(수용 계약)에 검증가능한 "
            "항목으로** 담으세요(각 직군이 회의에서 제안한 구체 조건) — **마감이 각 항목의 실제 코드 도달을 "
            "검증**하므로, 회의에서 한 약속이 회의록에서 증발하지 않습니다(라이브 P-015: 제안 6개 중 코드 반영 0). "
            "② **폴리시(기능을 넘는 품질) 직군이 팀에 있는가** — 이 작품이 그런 사용 경험을 "
            "요구하는 종류면(판단은 당신) 그 전문가가 팀에 있는지 보고 없으면 recruit하세요 — 안 부르면 그 "
            "품질은 아무도 책임지지 않습니다(라이브: 폴리시 직군 미채용/잠수로 '최소 기능'만 배포됨).")
    # [RFC-010 P3·P5 — 발산→수렴 + '완성' 재정의] LLM은 정렬(RLHF)으로 '전형적·자명한 1개 완성'으로
    # 수렴한다(mode collapse) → "언급한 것만" 구현(라이브 사용자 지적). 창의/체험형은 ① 복수 접근안을
    # 내고 골라야 뻔함을 깬다(발산→수렴, CreativeDC) ② '작동=완성'이 아니라 '경험돼서 좋음'이 완성이다
    # (Pirsig·Craftsmanship). 강제 아닌 환기 — 취향의 천장은 LLM이라 최종 판단은 리더/사용자(RFC-010 §3).
    creative = ("\n[창의·완성 기준 — 자명한 1개로 수렴 금지] 이 작품이 경험·재미·디자인이 중요한 "
                "종류라면: ① **'언급된 것'만 하지 말고** 더 좋게 만들 접근을 2~3개 떠올려 비교한 뒤 "
                "고르세요(LLM은 시키면 가장 뻔한 1개로 수렴 — 의식적 발산→수렴). ② '완성'의 기준은 "
                "'작동한다'가 아니라 **'사용자로서 써보니 좋다'**입니다 — 마감 전 누군가 실제로 써보고"
                "(플레이) '재밌나·뭐가 아쉽나'를 비평하고 최소 1회 개선하세요(작동≠좋음).")
    # [RFC-010 P6 — 장르 예시 대비 '범주적 부재' 점검(라이브: 게임에 사운드 0인데 아무도 인지·채용
    # 안 함). LLM은 '있는 것 개선'엔 강하나 '통째로 없는 범주'를 못 본다(mode collapse는 확장만 한다).
    # 처방: 같은 '종류의 훌륭한 예'와 비교해 그쪽엔 있는데 우리엔 없는 범주를 찾고, 그건 '개선'이
    # 아니라 '신규 구축'이며 필요 직군이 없으면 recruit한다(Graham '최고를 알아야 목표가 보인다' +
    # exemplar anchoring). 직군 키워드 하드코딩 없음 — 장르 판단·예시는 LLM 지식, 채용은 리더.
    gapcheck = ("\n[범주적 부재 점검 — '있는 것 개선'에만 머물지 말 것] 이 작품과 **같은 종류의 훌륭한 "
                "예**를 하나 **WebSearch로 실제로 찾아보고**(상상 말 것 — 자기 산출 기준으론 '평범=충분'으로 "
                "수렴), 그것이 *당연히 갖춘* 요소 중 우리에겐 **통째로 없는 범주**가 있는지 "
                "보세요. 무엇이 그런 범주인지는 **작품 종류를 아는 당신이 판단**합니다(시스템이 특정 범주를 "
                "지정하지 않음 — 직군·키워드 하드코딩 안 함). 있으면 그건 '개선'이 아니라 **신규 구축**이고, "
                "담당 직군이 팀에 없으면 **recruit**하세요. 라이브 교훈: 기존 것만 깊게 파고 *통째로 빠진 "
                "범주*는 아무도 본 적이 없었음 — 훌륭한 예라면 당연히 있을 범주를 먼저 점검(다듬기 전에).")
    # [RFC-011 M3 — 누적 사용자 취향을 '진짜 품질 기준'으로] 상용 품질의 천장은 LLM 취향이라
    # (인간 상관 ~0.5) 유일한 신뢰 앵커는 사용자다. 이 프로젝트에서 사용자가 반복해 지적·요구한
    # 말을 그대로 되돌린다 — '언급된 것'만 고치지 말고 *되풀이되는 불만의 범주*를 goal의 품질 축으로
    # (직군·키워드 하드코딩 0 — 사용자 자신의 말). 배포→플레이→비평이 돌수록 기준이 스스로 올라간다.
    fb_texts = [f.get("text", "") for f in (getattr(flow, "user_feedback", None) or []) if f.get("text")]
    taste = ""
    if fb_texts:
        bullets = "\n".join(f"  · “{_speech_clip(t, 160)}”" for t in fb_texts[:10])
        taste = ("\n[누적 사용자 취향 — 이 사용자의 진짜 품질 기준(크로스-프로젝트)] 사용자가 *이 작품 + "
                 "과거 작업들*에서 해 온 말들입니다. **되풀이되는 불만·요구가 곧 '상용 수준'의 기준**입니다"
                 "(LLM 취향엔 천장이 있어 사용자가 유일한 앵커 — 한 작품서 고친 걸 다음서 또 틀리지 말 것). "
                 "이번에 '언급된 것'만 처리하지 말고, 아래에서 **반복되는 "
                 "범주**(어떤 측면이 계속 '부족·구리다'고 지적되는지)를 찾아 goal의 품질 축으로 박으세요 "
                 "— 그 범주가 통째로 부실하면 신규 구축·recruit로 끌어올리세요:\n" + bullets)
    return _ok(f"task={flow.current.task_id} 정의 확정 — Purpose: {purpose[:50] or '(유지)'} / Goal: {goal[:80]}{tip}{qbar}{creative}{gapcheck}{taste}{excused_note}")
