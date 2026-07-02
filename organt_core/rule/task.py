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
