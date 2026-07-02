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


async def complete_task(flow, role, args):
    """[Rule 로직] complete_task — guide_tools에서 이관(평문 반환, @tool이 _ok 래핑)."""
    from .._util import _ok, _react, _speech_clip
    from .communication import _jobs_of, _norm_job
    import re
    g = flow.guide
    if flow.current is None:
        return _ok("오류: 진행 중인 Task가 없습니다.")
    if not flow.current.verified:
        return _ok(f"완료 거부: 이 Task({flow.current.task_id})를 run으로 한 번도 실행하지 않았습니다 "
                   f"— 산출물을 run으로 실제 실행한 뒤 complete_task 하세요(허위 완료 금지).")
    # owner가 '턴 한도'로 미완 반환한 Task는 완료 불가 — 같은 owner에게 request(Work)로 '이어서' 재위임해
    # 마저 끝내야 한다(허위완료→다음 Task churn·유실 차단). 이어가기는 Redo 한도와 무관하게 계속 가능.
    if flow.current.owner_incomplete:
        return _ok(f"완료 거부: 이 Task의 담당자가 '턴 한도'로 작업을 미완 반환했습니다 — 새 Task로 넘어가지 말고, "
                   f"같은 담당자에게 request(Work)로 '이어서 남은 부분을 마저 끝내라'고 재위임해 완성시킨 뒤 "
                   f"complete_task 하세요(이어가기는 횟수 제한 없음). 미완을 두고 다음으로 넘어가면 그 작업이 유실됩니다.")
    # owner에게 Work를 위임해 놓고(소유자 지정됨) 그 owner가 '검증된 산출물+응답'을 아직 내지 않았는데
    # 리더가 대신 완료하는 것을 막는다 — 이것이 사용자가 지적한 '허위 완료'(owner가 일하는 중/응답 전인데
    # 완료 때리고 다음 Task 열기)의 정확한 차단점. owner가 실제로 일하고 응답이 돌아와야(owner_delivered)
    # 완료 가능. (리더가 위임 없이 자기 도메인을 직접 한 Task는 owner==0이라 이 게이트를 건너뛴다.)
    if flow.current.owner and not flow.current.owner_delivered:
        return _ok(f"완료 거부: 이 Task는 owner({flow.current.status.owner or flow.current.owner})에게 "
                   f"위임돼 있는데 그 owner가 아직 '검증된 산출물'을 응답으로 내지 않았습니다(착수 전·작업 중일 "
                   f"수 있음). **owner가 일하는 중에 대신 완료하지 마세요(허위 완료 금지).** 같은 owner에게 "
                   f"request(Work)로 맡겨 run 검증 증거가 붙은 완료 응답을 받은 뒤 complete_task 하세요. "
                   f"끝내 무응답이면 recruit/재배정으로 다른 담당에게 맡기세요(리더가 대리 구현·완료 금지).")
    # [지각 비대칭 검증 — 범용 대문제 교정(사용자 "사운드 직군 없이 만들어냄" 2026-06-15)] LLM의
    # 외부현실 검증(비전: 스크린샷→Read, WebSearch 대조)은 '검증자가 지각 가능한 차원'(시각·텍스트)을
    # 암묵 전제한다. 검증자가 직접 경험해야만 품질을 아는 차원(들어야 아는 소리·음악, 느껴야 아는
    # 손맛 등)은 외부 대조가 불가능해 'presence(코드가 호출되나)'로 회귀 → 비전문가의 코드 합성
    # placeholder(라이브 P-010: 사운드=오실레이터 자급, 사운드 직군 0·recruit 0)가 완성으로 통과.
    # 지각 불가 차원은 '자기 판정'이 구조적으로 불가하므로, 자급을 완성으로 닫지 말고 '검증된 실제
    # 자원 또는 전문성'을 의무화한다(외부현실·전문화 원칙의 비시각 차원 확장). 흐름당 1회 보류 후
    # 재호출 통과(막지 않되 보이게 — 판단·범주는 리더. 직군·도메인 하드코딩 없음, 'gap_checked' 패턴).
    # [지각 비대칭 — 실제 자원 통합 요구(2026-06-15 P-015 라이브 규명으로 재강화). 외부소싱(WebFetch)
    # 증거게이트는 '레퍼런스 읽기'를 '자원 통합'으로 오인해 합성 placeholder를 통과시켰다(라이브: 사운드=
    # 오실레이터 합성, 작업공간 에셋 파일 0인데 WebFetch 11회로 읽기만 함). 증거를 '외부 접촉'→'실제 제작
    # 자원 파일 존재'로 강화: 작업공간에 코드 아닌 실재 에셋(_has_real_asset)이 있거나, 그런 자원이 필요
    # 없음을 result 첫 줄 '[지각차원 없음]'으로 의식적 명시해야 통과. 읽기만으론 안 닫힌다. 도메인 중립
    # (에셋=실재물 파일, 특정 장르/직군 아님), 명시 탈출구 상시(판단은 리더). 품질>과제한(사용자 승인).]
    if not getattr(flow, "percept_checked", False) and ("percept", flow.current.task_id) not in flow._gate_pass:
        _res = args.get("result") or ""
        # 마커 [지각차원 없음/불가] — 뒤(같은 줄)에 사유가 있으면 'reasoned'(의식적), 없으면 'bare'(반사적).
        _pm = re.search(r"\[\s*지각차원\s*(?:없음|불가)\s*[:：]?\s*\]?[ \t]*([^\n]*)", _res)
        _pd_any = bool(_pm)
        _pd_reasoned = bool(_pm) and len(_pm.group(1).strip()) >= 2
        # [탐지→강제] 이 Task가 *오디오* 지각차원을 가졌나(팀 직군 OR 합의기준·원문) — 가졌으면 빈 '없음' 불가
        _p_labels = [flow._info(m) for m in flow.current.team] + [flow._info(flow.leader)]
        _p_texts = [flow.current.status.goal, flow.current.acceptance,
                    flow.current.standard, getattr(flow, "origin_request", "")]
        _p_essential = _perceptual_essential(_p_labels, _p_texts)
        _p_asset = _has_real_asset(getattr(flow, "workspace", None))
        # 통과: 실제 에셋 OR 사유 있는 명시. 비-essential이면 반사적 빈 '없음'도 허용(가벼운 탈출 유지).
        if not (_p_asset or _pd_reasoned or (_pd_any and not _p_essential)):
            if flow.log:
                flow.log("complete_percept_gate", task=flow.current.task_id,
                         essential=_p_essential)
            if _p_essential and _pd_any and not _pd_reasoned:
                return _ok("마감 보류(지각 비대칭 — 오디오 차원이 *있는데* 빈 '없음' 선언): 이 Task엔 "
                           "사운드/음악 전문가가 있거나 합의 기준·원문이 소리·음악 품질을 명시합니다 — 즉 이 작품엔 "
                           "'들어야 아는' 지각차원이 분명히 있어 `[지각차원 없음]`(빈 선언)은 모순이라 통과 안 됩니다. "
                           "통과하려면 둘 중 하나: ① **실제 음원·효과음 파일을 WebSearch로 찾아 다운로드(CC0 등)해 "
                           "작업공간에 통합**하세요(코드 오실레이터 합성 ✕ — LLM은 소리를 못 들어 합성 placeholder가 "
                           "'완성'으로 통과됨). 직접 못 받으면 그 전문가를 recruit해 받게 하세요. ② 정말 불필요·불가하면 "
                           "(예: 사운드 설정 UI뿐, 폐쇄망) result에 **'[지각차원 불가: <사유>]'** 또는 "
                           "**'[지각차원 없음: <왜 소리·음악이 본질이 아닌지>]'**를 *사유와 함께* 적어 재호출하세요 "
                           "(반사적 빈 태그 ✕ — 의식적 판단).")
            return _ok("마감 보류(지각 비대칭 — 실제 자원 필요, 읽기·합성으론 통과 안 됨): 이 작품이 만든 것 "
                       "중 **직접 경험해야만(보는 것 말고 듣거나 느껴야) 품질을 아는 차원**이 있습니까? — 있다면 "
                       "LLM 검증자는 지각할 수 없어 코드로 합성한 placeholder가 '완성'으로 통과됩니다. 통과하려면 "
                       "둘 중 하나: ① **실제 제작 자원(코드로 합성한 게 아니라 외부에서 받아온 실재 에셋 파일)을 "
                       "WebSearch로 찾아 다운로드(CC0 등)해 작업공간에 통합**하세요 — *읽기만으론 안 됩니다, 실제 "
                       "파일이 있어야 게이트가 열립니다*. 직접 못 받으면 그 분야 **전문가를 recruit**해 받게 "
                       "하세요. ② 그런 실재 자원이 필요 없으면(순수 코드 작품, 또는 손맛처럼 코드로 구현·교차검증되는 "
                       "차원) result **첫 줄에 '[지각차원 없음] <이유>'**를 적어 재호출하세요(의식적 판단). 무엇이 "
                       "그런 차원인지는 작품을 아는 당신이 판단합니다(시스템은 특정 범주·직군을 지정하지 않음).")
        flow._gate_pass.add(("percept", flow.current.task_id))   # 이 산출물(Task)의 지각검사 통과 — 다음 Task는 다시 검사(per-Task)
        _ckpt(flow)              # [통과 영속] 보류 반환 전에도 누적 통과를 저장 → 복구가 재서술 안 시킴
    # [시각 검증 — percept 게이트의 *시각 평행판*(2026-07, 사용자 "시각 결과는 왜 안 본거야")] 위 percept
    # 게이트는 '오디오는 못 들으니 실제 에셋 요구'인데, *시각은 스크린샷으로 검증 가능*이라 **가정**했다(위
    # 주석 2067). 그러나 LLM 검증 환경(헤드리스)은 WebGL/GPU 렌더가 실패해 스크린샷이 검게/빈 화면으로
    # 나온다(라이브 P-003: 렌더 실패로 검은 맵이 presence·로직 QA를 통과·마감). 즉 시각도 '자동 검증 불가'일
    # 수 있다. 웹 UI(사용자가 화면으로 보는 것)를 마감하려면 실제 렌더를 *눈으로 확인*했음을 명시하거나
    # ('[시각 검증: 무엇이 보였나]'), 못 했으면 정직히 '[시각 미검증: 사유]'(사람 시각 확인으로 넘김)를
    # 적어야 한다 — presence만으론 못 닫는다. 반사적 빈 태그 차단(사유 ≥2자, percept·acceptance 동 패턴).
    # 도메인 중립(웹 UI=시각, 장르·직군 무관), 명시 탈출구 상시(판단은 리더). 흐름당 1회 보류.
    if not getattr(flow, "visual_checked", False) and ("visual", flow.current.task_id) not in flow._gate_pass:
        _vr = args.get("result") or ""
        _v_ok = bool(re.search(r"\[\s*시각\s*검증\s*[:：]\s*\S.{1,}", _vr))
        _v_none = bool(re.search(r"\[\s*시각\s*(?:미검증|불가|차원\s*없음)\s*[:：]\s*\S.{1,}", _vr))
        if _has_visual_runtime(getattr(flow, "workspace", None)) and not (_v_ok or _v_none):
            if flow.log:
                flow.log("complete_visual_gate", task=flow.current.task_id)
            return _ok(
                "마감 보류(시각 검증 — 실제 렌더를 봤는가): 이 산출물은 사용자가 *화면으로 봅니다*. LLM QA는 "
                "presence·로직(요소 존재·무크래시)은 봐도 **실제 렌더 결과**는 놓칠 수 있습니다 — 특히 "
                "WebGL/GPU는 헤드리스에서 렌더가 실패해 스크린샷이 검게 나옵니다(라이브 P-003: 렌더 실패로 검은 "
                "화면이 QA를 통과·마감). 통과하려면 둘 중 하나: ① 실제 렌더를 **스크린샷→눈으로 확인**하고 "
                "result에 '[시각 검증: 맵·캐릭터·UI가 실제로 어떻게 보였나]'(빈 'ok' ✕ — 무엇이 보였는지), "
                "또는 ② 못 봤으면(헤드리스 렌더 불가 등) '[시각 미검증: <사유>]'로 정직히 명시하세요(사람 "
                "시각 확인으로 넘어감). presence·'요소 존재'만으로 시각을 닫지 마세요.")
        flow._gate_pass.add(("visual", flow.current.task_id))
        _ckpt(flow)
    # [수용 계약 마감 바인딩 — 회의 전문성이 '코드'에 도달했는가(2026-06-15 P-015 규명)] verified·percept·
    # contrib·cross-check는 각각 '실행됨/실재 에셋/잠수 직군 실작업/홀리스틱 좋음'을 보지만, **회의에서
    # 합의한 구체 약속**(히트스톱·콤보·레이어드BGM 등)이 실제 산출물에 들어갔는지는 어느 게이트도 안 본다 —
    # 잠수 아닌 직군이 '뭔가'만 하면(사운드가 app.js에 6줄) contrib는 통과, cross-check는 '좋아?'로
    # satisfice된다. 라이브: 회의 제안 6개 중 코드 반영 0인데 마감(사용자 "플레이하면 감이 없다"). 수용
    # 계약을 마감에 구속한다 — 각 항목 충족 증거(result '[수용기준 검증]' + 항목별 충족·증거) 또는 의식적
    # 드롭. 반사적 재호출로는 통과 안 됨(percept·contrib와 동 원리, 증거/명시 통과형). 도메인 중립(기준은
    # 팀이 회의에서 자작), 자율 보존(의식적 드롭/N·A 상시 — 판단은 리더). 흐름당 1회.
    if not getattr(flow, "acceptance_checked", False) and ("acceptance", flow.current.task_id) not in flow._gate_pass:
        _result = args.get("result") or ""
        # [반사적 빈 탈출 차단 — percept와 동 원리(2026-06-19 감사)] '검증/충족/확인/반영'은 항목별
        # 증거가 *뒤따르는* 헤더(다음 줄에 회계 — 같은 줄 강제 불가)지만, '해당없음' 탈출(N/A·없음·
        # 면제·불필요)이 사유 없는 빈 마커만으로 통과하면 satisfice다 — 게이트 본문이 명시한 '반사적
        # 재호출로는 통과 안 됨'과 모순. 그래서 탈출 마커는 *사유(≥2자)* 를 요구한다(데이터출처·percept와
        # 같은 증거/명시 패턴). 헤더는 그대로(뒤따르는 항목 회계가 증거), 탈출만 사유 강제. 무한 반려 아님.
        _acc_hdr = bool(re.search(r"\[\s*수용\s*기준\s*(?:검증|충족|확인|반영)\s*\]", _result, re.I))
        _acc_escape = bool(re.search(
            r"\[\s*수용\s*기준\s*(?:N\s*/?\s*A|없음|면제|불필요)\s*[:：]?\s*\]?[ \t]*\S{2,}", _result, re.I))
        _accounted = _acc_hdr or _acc_escape
        acc = (flow.current.acceptance or "").strip()
        if not _accounted:
            if flow.log:
                flow.log("acceptance_gate", task=flow.current.task_id, defined=bool(acc))
            if acc:
                return _ok(
                    "마감 보류(수용 계약 검증 — 합의한 '좋음' 기준이 코드에 도달했나. 반사적 재호출로는 통과 "
                    "안 됨): 팀이 회의에서 합의한 수용 기준입니다 —\n" + _speech_clip(acc, 1500) + "\n\n각 항목이 "
                    "**실제 산출물에 들어가 검증됐는지** 직접 확인하세요(회의에서 '중요하다'고 한 게 코드에 "
                    "없으면 그게 '플레이하면 감이 없다'의 정확한 원인입니다 — 라이브 P-015: 제안 6개 중 0개 "
                    "반영). 미구현 항목은 그 도메인 owner에게 request(Work)로 맡겨 **실제로 넣고 교차검증까지** "
                    "받으세요. 그런 뒤 result에 **'[수용기준 검증]'** 헤더와 **항목별로 (충족 — 어떻게 "
                    "확인했나 / [드롭] <왜 뺐나>)**를 적어 재호출하세요(의식적으로 뺀 항목은 드롭으로 남음 — "
                    "판단은 당신).")
            return _ok(
                "마감 보류(수용 계약 미정의 — '좋음'의 구체 기준이 없음): 이 산출물이 '작동'을 넘어 '좋다"
                "(상용 수준)'가 되려면 참이어야 할 **구체·검증가능한 조건**이 무엇입니까? — 같은 종류의 "
                "**훌륭한 예와 대조**해 그것이 당연히 갖춘, '감'을 만드는 요소들(작품 종류를 아는 당신이 판단)을 "
                "기준으로 삼으세요. 회의를 했다면 거기서 전문가들이 제안한 구체 항목이 곧 기준입니다. "
                "set_goal(acceptance=…)로 박아 두거나, 지금 result에 **'[수용기준 검증]'** 헤더로 항목별 충족 "
                "증거를 적어 재호출하세요. 정말 품질 기준이랄 게 없는 단순 산출물이면 result에 **'[수용기준 "
                "N/A] <이유>'**를 적어 재호출하세요(의식적 판단 — 그냥 재호출론 통과 안 됨).")
        flow._gate_pass.add(("acceptance", flow.current.task_id))   # 이 산출물(Task)의 수용계약 검사 통과(per-Task)
        _ckpt(flow)              # [통과 영속] 보류 반환 전에도 누적 통과를 저장 → 복구가 재서술 안 시킴
    # [데이터 출처 게이트 — 합성/하드코딩 데이터를 '공공·실데이터 학습'으로 위장 차단(2026-06-18,
    # 라이브 P-021)] percept(합성 에셋)·acceptance(합의 약속)와 평행. 요청이 real/public 데이터
    # 학습을 요구하는데 모델이 *지어낸* 데이터로 학습됐고(코드에 합성 표식) 작업공간에 실제 데이터
    # 파일·출처 증거가 없으면 보류 — 실데이터를 받아 재학습하거나, 정말 불가하면 result 첫머리에
    # 의식적으로 명시. 도메인·직군 하드코딩 없음(요청 의도로만 발동), 흐름당 1회(percept와 동 패턴).
    if not getattr(flow, "data_prov_checked", False) and ("data_prov", flow.current.task_id) not in flow._gate_pass:
        _st = flow.current.status if getattr(flow.current, "status", None) else None
        _intent = " ".join([str(getattr(flow, "origin_request", "") or ""),
                             str((_st.purpose if _st else "") or ""),
                             str((_st.goal if _st else "") or "")])
        if _wants_real_data(_intent):
            _res = args.get("result") or ""
            _declared = bool(re.search(r"\[\s*데이터\s*출처\s*[:：\]]", _res)) or \
                bool(re.search(r"\[\s*합성\s*(?:불가피|허용|의도|데이터\s*명시)", _res))
            _synth = _synthesizes_data(getattr(flow, "workspace", None))
            if _synth and not _has_real_dataset(getattr(flow, "workspace", None)) and not _declared:
                if flow.log:
                    flow.log("data_provenance_gate", task=flow.current.task_id,
                             file=_synth[0], marker=_synth[1])
                return _ok(
                    "마감 보류(데이터 출처 — '실제·공공 데이터로 학습'인데 데이터를 지어냈습니다): 사용자 "
                    f"요청이 실제/공공 데이터 학습을 요구하는데, `{_synth[0]}`이(가) 데이터를 **합성/하드코딩**"
                    f"으로 만들고(표식 '{_synth[1]}') 작업공간에 받아온 실제 데이터 파일(csv/parquet 등)이 "
                    "없습니다 — 모델이 배운 건 자기가 지어낸 분포라 'MAE 성공'도 순환논리입니다(요구 위반). "
                    "통과하려면 둘 중 하나: ① 그 데이터 owner가 **실제 데이터를 받아**(공공데이터의 무키 "
                    "벌크 다운로드·공개 데이터셋 미러를 WebSearch로 찾아 curl/WebFetch) 작업공간에 두고 "
                    "그것으로 재학습하세요 — *합성으로 때우지 말 것*. ② 실제 데이터가 정말 닿지 않으면(키 "
                    "필수·폐쇄망) result 첫 줄에 **'[데이터 출처: <실제 출처 또는 왜 불가한지>]'**를 적어 "
                    "의식적으로 명시하고 재호출하세요(가짜를 진짜인 척 닫지 말 것 — 그냥 재호출론 통과 안 됨).")
        flow._gate_pass.add(("data_prov", flow.current.task_id))   # 이 산출물(Task)의 데이터출처 검사 통과(per-Task)
        _ckpt(flow)              # [통과 영속] 보류 반환 전에도 누적 통과를 저장 → 복구가 재서술 안 시킴
    # [검증 분업 — 1회 보류] 품질 판정이 리더 1인에게 독점되는 것을 구조적으로 흔든다(라이브
    # P-009: QA·교차 검증 0인 채 단독 마감 → 브라우저 렉·적 돌진 등 사용성 결함이 그대로 통과,
    # 사용자가 첫 발견). owner 인도 후 '다른 멤버'의 검증 참여가 0이면 첫 호출만 보류하고 검증
    # 위임을 안내한다 — 재호출은 통과(판단은 결국 리더 몫, 무한 반려 금지. 직군 키워드 없음).
    # [교차 검증 의무 — Rule/Task.md 6 (사용자 확정: 범용 이치는 하드 제한도 옳다)]
    # 작업자(owner) 아닌 멤버가 산출물을 '사용자처럼 실제로 사용해' 검증해야 완수 선언 가능.
    # 제3멤버가 팀에 있는 한 우회 없음(거부 반복) — 라이브 P-009: 단독 마감이 렉·사용성
    # 결함을 통과시킴. 제3멤버가 정말 없을 때만 예외(단독 마감 마커가 기록에 남는다).
    third = [m for m in flow.current.team
             if m not in (flow.leader, flow.current.owner)]
    # [발견1 교정] 검증 대상: owner 위임 산출물 OR 리더 직접구현(leader_writes>0). 리더 독식도
    # 제3자 검증을 면제하지 않는다(보편 이치). 산출물도 없으면(아무것도 안 만든 Task) 게이트 무의미.
    has_product = bool(flow.current.owner) or getattr(flow.current, "leader_writes", 0) > 0
    # [독립 검증 = 다른 도메인 (동질 모델 원리)] 검증은 '다른 관점'이라야 결함을 본다 — 같은 Claude·
    # 같은 직군 검증자는 같은 맹점(에코)이라 독립이 아니다. owner와 도메인 다른, 지금 도달 가능한
    # 검증자가 있으면 그 독립 검증을 요구하고, 그런 동료가 없거나 전원 타 흐름 점유면 같은 직군 검증으로
    # 폴백(단일도메인 팀·교착 방지). cross-check 자체(≥1 타멤버)는 종전대로 하드 의무.
    _engx, _scopex = flow.comm.engagement, flow.comm.scope
    _ownx = flow.current.owner or flow.leader
    _odx = {_norm_job(j) for j in _jobs_of(flow._info(_ownx) or "")} - {""}
    def _offdom_reach(m):
        md = {_norm_job(j) for j in _jobs_of(flow._info(m) or "")} - {""}
        if not (_odx and md and not (md & _odx)):
            return False
        return not (_engx is not None and _scopex is not None and _engx.busy_elsewhere(m, _scopex))
    third_offdom = [m for m in third if _offdom_reach(m)]
    cc_ok = (flow.current.cross_check_offdomain > 0
             or (flow.current.cross_checks > 0 and not third_offdom))
    if has_product and not cc_ok and third:
        # [반-스래싱 — 리더 독점 차단(2026-06-20 P-024 규명: 리더가 같은 Task를 7회 재마감 + run 98회
        # 자가검증)] 교차검증 게이트가 보류될 때마다 카운트해, 3회+면 '혼자 떠안고 헛돎' 경보로 에스컬레이트.
        # 리더의 run은 교차검증으로 안 쳐주므로(peer 필수) 결과 문구만 바꿔 재호출하면 영원히 막힌다 —
        # "멈추고 검증 1회 위임하라"를 하드 문구로. cross_check가 오르면 cc_ok=True로 자연 통과(교착 0).
        flow.current.cc_held = getattr(flow.current, "cc_held", 0) + 1
        _thrash = ""
        if flow.current.cc_held >= 3:
            if flow.log:
                flow.log("complete_thrash", task=flow.current.task_id, holds=flow.current.cc_held)
            _thrash = (f"\n\n⚠ [반복 마감 {flow.current.cc_held}회 — 독점·헛돎 경보] 같은 Task를 계속 "
                       f"마감 시도하는데 **다른 멤버의 검증이 여전히 0**입니다. 결과 문구만 바꿔 재호출하면 "
                       f"*영원히* 막힙니다 — **리더가 run으로 혼자 반복 검증하는 건 교차검증으로 인정 안 됩니다.** "
                       f"지금 **멈추고 딱 하나**: 위에 지목된 검증자 1명에게 request(Work)로 '실제로 실행·사용해 "
                       f"검증'을 맡기고 **그 응답이 올 때까지 complete_task를 다시 부르지 마세요**(응답이 오면 "
                       f"게이트가 자동으로 열립니다). 전체를 혼자 떠안지 말 것 — 그게 '리더 독점'입니다.")
        idle = [m for m in third if flow.act_by.get(m, 0) == 0]
        idle_note = (f"\n[정보] 이 Task 팀에서 **실작업·검증 참여 0**인 멤버: {flow._names(idle)} — "
                     f"goal에 이들의 전문 영역이 있다면 그 부분의 검증·보완을 이들에게 맡기는 것이 "
                     f"자연스럽습니다." if idle else "")
        per_item = ("goal의 각 부분이 '존재하나'가 아니라 **그 산출물을 쓰는 사람으로서 처음부터 끝까지 "
                    "써본 경험**으로 — **시각 산출물이면 화면을 스크린샷으로 찍어 Read로 직접 '눈으로 보고'**"
                    "(존재가 아니라 '실제로 이렇게 보인다/작동한다'를 직접 확인 — 임시방편처럼 보이는지 상용 "
                    "수준인지), **같은 종류의 훌륭한 예를 WebSearch로 실제로 찾아 대조**해 '써보니 좋은가·"
                    "답답한가? 뭐가 싸구려 같고 빠지거나 떨어지나? 상용 수준이면 당연히 있을 게 없나?'를")
        # [RFC-008 P0 + RFC-010 P4 — 직무 기준을 '좋은가' 비평 루브릭으로] 산출물 도메인의 craft
        # profile을 검증 루브릭으로 제공한다. 검증자가 "작동하는가"(holistic)가 아니라 "**실제로
        # 써보니 이 도메인 기준에 비춰 좋은가·뭐가 아쉬운가**"를 차원별로 보게 — rubric-guided judge가
        # 인간 일치 2배(LLM-Rubric 2501.00274). 단일 점수 아닌 차원별 비평 + 사용자가 최종 취향 앵커.
        rubric = ""
        owner_job = ((flow._info(flow.current.owner) if flow.current.owner
                      else flow._info(flow.leader)) or "").strip()
        if callable(getattr(flow, "craft_of", None)) and owner_job:
            parts = [flow.craft_of(j) for j in owner_job.split("·") if j.strip()]
            parts = [p for p in parts if p]
            if parts:
                rubric = (f"\n[검증 루브릭 — 산출물 도메인 '{owner_job}'의 품질 기준. 검증 위임 본문에 "
                          f"**이 기준을 그대로 전달**하고, 검증자가 **실제로 실행/플레이한 뒤** 각 항목을 "
                          f"'써보니 좋은가·충분한가'로 평가하게 하세요 — '돌아가는가'가 아니라 '이 기준에 "
                          f"비춰 좋은가, 뭐가 아쉬운가'가 질문입니다(미달은 구체적 결함으로):\n"
                          + _speech_clip("\n---\n".join(parts), 2500) + "]")
        fb_v = [f.get("text", "") for f in (getattr(flow, "user_feedback", None) or []) if f.get("text")]
        taste_v = ""
        if fb_v:
            taste_v = ("\n[사용자 표준 — 이 사용자가 *여러 작업에 걸쳐* 반복 요구한 것(크로스-프로젝트)] "
                       "이 작품뿐 아니라 *과거 프로젝트들*에서도 사용자가 해 온 비평입니다 — *유일하게 믿을 수 "
                       "있는 품질 앵커*(같은 모델 taste는 천장이라). 검증자에게 **이 표준들이 이번 산출물엔 "
                       "처음부터 반영됐는지 직접 써보고 확인**하라 전하세요(한 작품서 고친 걸 또 틀리지 말 것 — "
                       "되풀이된 불만이 곧 이 사용자의 상용 기준):\n"
                       + "\n".join(f"  · “{_speech_clip(t, 140)}”" for t in fb_v[:10]))
        # [수용 계약을 검증 기준으로] 교차 검증을 '좋아?'(satisfice되는 홀리스틱)가 아니라 팀이 합의한
        # 구체 기준 대조로 — 검증자가 각 항목의 실제 충족을 써보고 확인하게(회의 전문성→검증 루프 연결).
        acc_x = (flow.current.acceptance or "").strip()
        acc_v = ("\n[수용 계약 — 이 항목들이 실제로 충족됐는지 검증] 팀이 합의한 '좋음' 기준입니다. 검증자에게 "
                 "**각 항목이 산출물에 실제로 들어가 작동하는지 써보고 확인**하라고 전하세요(존재가 아니라 "
                 "'이 기준대로 됐나'):\n" + _speech_clip(acc_x, 1200)) if acc_x else ""
        std_x = (flow.current.standard or "").strip()
        standard_v = ("\n[최대성 기준 — *구성요소별 분해 대조* (PHASE 3, 외부 grounding)] 이 작품의 *최대판 "
                      "구성요소(분해)*입니다(목표 단계서 실제 exemplar에 앵커해 박은 부품 목록). 검증자(다른 "
                      "도메인·신선한 시각)에게: '좋아?'(홀리스틱이라 satisfice됨)를 묻지 말고 — **위 *각 "
                      "구성요소를 하나씩* 워크스페이스 ls/read로 실측**해 (요소: 있나? / 최대만큼 깊나? / "
                      "빠졌나?)를 *항목별*로 보고하게 하세요(구성적 품질은 *보지 않아도* 구성요소로 판정 "
                      "가능 — taste 아님, 천장은 좁은 지각 잔여뿐). **그리고 *기능 점검*에 그치지 말고 "
                      "검증자가 *진짜 사용자처럼 주 사용 흐름을 직접 걸어보게* 하세요 — '핵심 목표(예: 내 위치 "
                      "결과 알기)를 *몇 단계*에 달성하나? 자동(위치감지·기본값·원탭)인가 *수동 다단계 폼*인가? "
                      "최고 앱의 흐름과 대조해 마찰이 어디서 나나?'(라이브 갭: 위치기반인데 자동위치 0·수동 "
                      "select 강제 = 기능완비여도 실사용 실패). 접근성(라벨·키보드)도 함께. 못 미치거나 빠진 "
                      "요소·마찰은 그 도메인 owner에게 개선 위임 → 재검증(갭이 닫힐 때까지 — 이게 깊이의 반복, "
                      "0-redo를 깨는 지점). 정말 불필요한 요소만 result에 사유 영속(의식적 N/A — 객관 거짓주장 "
                      "불가):\n" + _speech_clip(std_x, 1500)) if std_x else ""
        iface_x = (flow.current.interfaces or "").strip()
        iface_v = ("\n[통합 검증 — 인터페이스 계약 준수 (PHASE 1.3 / L2)] 도메인 간 계약입니다. 검증자에게 — "
                   "이 계약이 *실제로 지켜지나*(예: 프론트가 백 포맷을 진짜 소비? VFX-사운드 타이밍 싱크?)를 "
                   "워크스페이스 실측으로 확인하게 하세요('각자 만들고 안 붙는' 사일로 차단):\n"
                   + _speech_clip(iface_x, 800)) if iface_x else ""
        _ver_state = ("**다른 멤버의 검증 참여가 0**입니다" if flow.current.cross_checks == 0
                      else "검증이 **같은 직군(에코)뿐**이라 독립 검증이 없습니다(같은 관점=같은 맹점)")
        indep_note = (f"\n[독립 검증 — 다른 도메인 필수] owner와 **다른 도메인** 동료가 검증해야 독립적입니다"
                      f"(같은 직군 검증은 에코라 같은 결함을 못 봄). 지금 가능한 독립 검증자: "
                      f"{flow._names(third_offdom)}." if third_offdom else "")
        # [검증 역할(QA) 우대 — 최종·전체 인수(2026-06-19 사용자 설계)] 팀에 '검증/품질' 기능 역할이 있으면
        # 전체·사용자관점 검증은 그 역할이 특화돼 있으니 우선 라우팅한다(만든 사람의 저자편향 밖 = 최종 측정).
        # 부분·기술 검증은 도메인 동료도 OK — QA는 '완성품 전체를 사용자처럼' 보는 데 우대. 타이틀이 아니라 기능.
        _verifiers = [m for m in third if _is_verifier(flow._info(m))]
        qa_note = (f"\n[검증 역할 우대 — 전체·최종 인수는 QA에게] 이 팀에 **검증 전문 역할**이 있습니다: "
                   f"{flow._names(_verifiers)} — 부분 검증은 도메인 동료도 되지만, **완성품 전체를 사용자처럼 "
                   f"처음부터 끝까지 써보는 '최종 인수'는 이 역할에게 우선 맡기세요**(만든 사람·도메인 전문가는 "
                   f"자기 부분만 보고 저자편향이 있어 전체 사용자경험을 객관적으로 못 봅니다 — 그래서 QA가 전담). "
                   f"이들이 그 Task 멤버가 아니면 create_task members에 넣거나 request(Work)로 검증을 위임하세요."
                   if _verifiers else "")
        # [수평 수렴 — '단방향 검증 1회'를 'meet 교차비평+peer→owner 보완'으로 (2026-06-19 사용자 설계:
        # "사람 많은데 대화 적음")] cross-check 게이트는 종전 '검증자→리더 1회 보고'면 충족돼, 빌드가
        # '검증자 1명이 읽고 끝'인 얕은 파이프라인이 되곤 했다(P-023: 참여자 많고 대화 1회). 게이트 바닥은
        # 그대로 두되(교착 위험 없이), 인도 후 동료들을 meet로 다시 모아 '합쳐 놓고 좋은가'를 *수평으로*
        # 교차비평하고 비평자가 owner에게 직접 보완을 넘기도록 안내한다 — peer→owner는 다른 위임쌍이라
        # 리더 Redo 예산도 안 쓰고(프로토콜이 이미 선호하는 경로), 1라운드는 '반복'이 아니라 정상 수렴이다.
        _horiz_note = ("\n[수평 수렴 — 단방향 검증보다 meet 교차비평] 검증을 '검증자→리더 1회 보고'로 받고 "
                       "끝내지 말고, **owner와 독립 검증자(있으면 QA)를 meet로 함께 다시 모아** '합쳐 놓고 보니 "
                       "좋은가'를 서로 교차비평하게 한 뒤, **비평한 검증자가 그 owner에게 직접 request(Work)로** "
                       "개선점을 넘겨 owner가 1회 끌어올리게 하세요(수평 수렴; 리더 허브 우회 — peer→owner 보완은 "
                       "리더 Redo 예산도 안 씁니다). 품질 근거를 든 이 1라운드는 '결함 없는 반복'이 아니라 정상 "
                       "수렴입니다(금지되는 건 기준 없는 반사적 반복).")
        return _ok(f"완료 거부(교차 검증 의무 — Rule/Task + RFC-010 P1·P2 / RFC-011 M2): 산출물 인도 후 "
                   f"{_ver_state}. **만든 사람이 아닌** 다른 멤버에게 request(Work)로 "
                   f"'**코드만 읽지 말고 산출물을 처음부터 끝까지 실제로 실행·사용·플레이해 본 뒤**(라이브 "
                   f"근거: 실제로 써본 검증자가 읽기만 한 쪽보다 결함을 훨씬 많이 잡음 — TITAN 82% vs 18%) "
                   f"{per_item} 보고하라'고 맡긴 뒤 마감하세요. **'요소 존재·JS 에러 0·서버 기동됨' 같은 것은 "
                   f"'작동'이지 '좋음'의 증거가 아닙니다 — 검증으로 인정하지 마세요**(라이브: 그렇게 통과시킨 게 "
                   f"'상용 수준 아님'으로 반려됨). 검증자의 결함·아쉬움 보고가 Redo(창의적 개선)의 근거입니다. "
                   f"**자기 산출물 자기검증은 무효**(편향 — Pride&Prejudice): 반드시 만든 사람이 아닌, 실제로 "
                   f"써본 다른 멤버. 검증 응답이 오면 게이트는 자동으로 열립니다.{rubric}{acc_v}{standard_v}{iface_v}{taste_v}{idle_note}{indep_note}{qa_note}{_horiz_note}{_thrash}")
    # [최대성 마감 바인딩 — 구조적 강제(2026-06-20 사용자 "프롬프트 의존 제거")] gap_check가 standard
    # (최대 표준)를 *기록*하게 강제해도, 그게 코드에 *도달*했는지는 종전엔 교차검증 standard_v 프롬프트
    # (검증자 satisfice 가능)에만 맡겨 '최소 구현 통과'가 남았다. 이제 standard가 박혀 있으면 마감이 그 최대
    # 대비 *항목별 충족/의식적 드롭*을 result에 회계해야 한다(acceptance와 평행, 단 *외부 최대* 기준).
    # flow.current.standard로 키잉(per-Task, 플래그 없음 → 매 Task 재평가). 헤더(검증/충족/회계/드롭)는 항목
    # 회계가 뒤따르는 통과, 탈출(N/A·불필요·면제)은 사유 필수. 교차검증 통과 *뒤* 단계(standard_v 테스트 보존).
    _std_bind = (flow.current.standard or "").strip()
    # standard가 비었거나 그 자체가 '[최대화 N/A …]' 면제선언이면 미발동(과제한 방지 — 요구가 부를 때만 강제)
    if (_std_bind and not re.match(r"^\s*\[\s*최대화\s*(?:N\s*/?\s*A|면제|불필요)", _std_bind)
            and ("standard", flow.current.task_id) not in flow._gate_pass):   # [누적] 이미 통과면 재확인 안 함(오락가락 차단)
        _res2 = args.get("result") or ""
        # [구성요소별 분해 강제(2026-06-20 사용자: '잘된 구성인가는 구성요소로 판정 가능')] 바 헤더·한 줄
        # 회계는 satisfice라 불충분 — 헤더 뒤에 *여러 구성요소* 항목(분해)이 실제로 있어야 통과. 구성적
        # 품질은 보지 않아도 구성요소 대조로 분석 가능(taste 아님 — 천장은 좁은 지각 잔여뿐).
        _std_acc = re.search(r"\[\s*최대성\s*(?:검증|충족|회계)\s*\]([\s\S]*)", _res2)
        _std_hdr = bool(_std_acc and len(re.findall(r"\n|/|·|•|\d[).]", _std_acc.group(1))) >= 2)
        _std_na = bool(re.search(r"\[\s*최대성\s*(?:N\s*/?\s*A|불필요|면제)\s*[:：]?\s*\]?[ \t]*\S{2,}", _res2))
        if not (_std_hdr or _std_na):
            if flow.log:
                flow.log("standard_bind_gate", task=flow.current.task_id)
            return _ok("마감 보류(최대성 검증 — 최대판 *구성요소별* 대조 강제, 바 헤더·재호출만으론 통과 안 됨): "
                       "이 Task엔 팀이 합의한 **최대판 구성요소(분해)**가 박혀 있습니다 —\n"
                       + _speech_clip(_std_bind, 1200) + "\n\n**'좋은가?'(홀리스틱이라 satisfice됨)가 아니라 위 "
                       "*각 구성요소*가 산출물에 실제로 있나·최대만큼 깊나를 하나씩 분석**해 result에 "
                       "**'[최대성 검증]'** 헤더로 *항목별*(요소: 충족 — 증거 / [드롭] <왜 이 작품엔 과한지>, "
                       "여러 항목)을 적어 재호출하세요 — 구성적 품질은 *보지 않아도* 구성요소로 판정 가능합니다"
                       "(taste 아님). 미달·누락 요소는 그 도메인 owner에게 개선 위임 후 재검증(이게 깊이의 "
                       "반복 — 0-redo를 깨는 지점). 표준이 통째로 부적용이면 **'[최대성 N/A: <사유>]'**(빈 "
                       "재호출 통과 안 됨 — 사유 필수).")
        flow._gate_pass.add(("standard", flow.current.task_id))   # [누적·영속] 통과 기록 → 다음 호출엔 standard 건너뜀(acceptance·data_prov와 동일)
        _ckpt(flow)
    # [협업 — 인터페이스 직접 합의 강제(2026-06-22 사용자: '전문가끼리 서로 대화하는가')] interfaces(도메인
    # 간 계약)를 선언했는데 owner들이 서로 직접 확인(peer↔peer Info)한 적이 없으면 = 계약을 리더만 경유
    # 전달(사일로·중계 병목)했거나 owner가 추측한 것(P-028 API 미스매치). ≥2개 도메인이 실작업했을 때만
    # (맞물릴 대상이 있을 때) 발동 — 과발동 차단. peer 직접 대화가 생기거나 '[인터페이스 직접합의 N/A:
    # 사유]'일 때까지 보류(persistent-until-resolved — staffing 게이트와 동형, 1회 재호출론 통과 안 됨).
    _iface_x = (getattr(flow.current, "interfaces", "") or "").strip()
    _iface_na = bool(re.search(r"\[\s*인터페이스\s*직접\s*합의\s*(?:n\s*/?\s*a|면제|단독|불필요)",
                               (args.get("result") or ""), re.IGNORECASE))
    if (has_product and _iface_x and not getattr(flow.current, "peer_info_pairs", None)
            and not _iface_na and not getattr(flow, "iface_dialogue_checked", False)
            and ("iface", flow.current.task_id) not in flow._gate_pass):   # [누적] 이미 통과면 재확인 안 함
        _iwk = [m for m in flow.current.team if m != flow.leader and flow.act_by.get(m, 0) > 0]
        _idoms = {_norm_job(j) for m in _iwk for j in _jobs_of(flow._info(m) or "")} - {""}
        if len(_idoms) >= 2:
            if flow.log:
                flow.log("iface_dialogue_gate", task=flow.current.task_id)
            return _ok("완료 보류(인터페이스 직접 합의 — 전문가끼리 직접 대화): 이 Task는 도메인 간 "
                       "인터페이스 계약(interfaces)을 선언했는데 **owner들이 서로 직접 확인한 기록이 "
                       "없습니다**(리더만 경유 = 사일로·중계 병목, owner는 계약을 추측 → 통합 불일치). "
                       "맞물리는 도메인 owner끼리 **request(Info)로 계약을 직접 합의**하게 하세요(데이터 "
                       "포맷·API·이벤트 타이밍을 리더 중계·추측 말고 *당사자끼리*). 정말 단방향/단독이라 "
                       "직접 합의가 불필요하면 result에 **'[인터페이스 직접합의 N/A: <사유>]'**를 적어 "
                       "재호출하세요(사유 필수).")
    if _iface_x and ("iface", flow.current.task_id) not in flow._gate_pass:   # [누적·영속] iface 통과(peer 합의/N-A) 기록 → 재호출엔 건너뜀
        flow._gate_pass.add(("iface", flow.current.task_id))
        _ckpt(flow)
    # [팀 기여 의무 게이트 — RFC-009] 교차 검증(cross_checks)과 **독립**. 검증이 됐어도(검증은
    # 기능 위주라 폴리시 부재를 못 잡음 — RFC-009 §3), 팀에 부른 직군이 이 흐름에서 회의 발언만 하고
    # 실작업·검증 0(act_by==0: Write/Edit/run 한 번도 없음)이면 그 도메인(타격감·그래픽·사운드·디자인·
    # UX 등 폴리시)은 작품에 '반영되지 않은' 것이다 — 라이브 P-010: VFX·디자이너·모션·게임비주얼이
    # 실구현 0인 채 마감돼 "단순 나열 웹·타격감 없는 게임"이 됨(발언≠기여). 직군 키워드 없이 '실작업
    # 0'만 본다(보편 이치: 부른 직군은 기여한다, 회의 참석≠기여). [증거/명시 통과(2026-06-15 라이브
    # 교정): soft '1회 보류 후 재호출 통과'는 마감 관성에 무력(라이브 3/3 반사적 재호출로 폴리시 또 빠짐
    # — 아래 task_contrib_overridden가 그 증거). percept와 같은 원리로 강화 — 잠수 직군이 실제로 기여하거나
    # (idle 해소), 정말 불필요함을 result '[기여 불필요]'로 의식적으로 명시해야 통과. 무한 반려 아님(명시
    # 탈출구 상시 — 판단은 리더). 동면 복구로 act_by가 0에서 재시작해도 명시/기여로 통과(반사적 재호출은 X).]
    if has_product and not flow.current.contrib_checked:
        contrib_idle = [m for m in third if flow.act_by.get(m, 0) == 0]
        _cd = bool(re.search(r"\[\s*기여\s*(?:불필요|제외|면제)\s*\]", args.get("result") or ""))
        # [흡수 차단 — [기여 불필요] 블랭킷 우회 봉쇄(2026-06-21, 라이브 P-026 규명)] 회의에 참여
        # (participated)했는데 이 Task에서 Work를 한 번도 못 받고(work_delegated_to 밖) 실작업 0인 멤버 =
        # 그 전문 도메인이 '흡수'된 것이다(리더가 전문가에게 위임 안 하고 제너럴리스트가 그 도메인까지 다 써버림
        # = 리더 독점의 핵심). 이런 멤버는 [기여 불필요] 한 줄로 묵살 못 한다 — 실제로 한 번은 위임(①)하거나
        # 팀에서 빼야(②) 한다. 위임받으면(work_delegated_to 진입) 그 뒤엔 [기여 불필요]로 마감 가능(기회는 줬다).
        # 도달 가능자만(예비·타 흐름 점유 제외) → 맡길 사람이 없으면 통과(교착 없음).
        def _reach_for_work(m):
            if str((flow._info(m) or "")).startswith("예비"):
                return False
            return not (_engx is not None and _scopex is not None and _engx.busy_elsewhere(m, _scopex))
        _part = getattr(flow.current, "participated", None) or set()
        _deleg = getattr(flow.current, "work_delegated_to", None) or set()
        _absorbed = [m for m in contrib_idle if m in _part and m not in _deleg and _reach_for_work(m)]
        if contrib_idle and (not _cd or _absorbed):
            if flow.log:
                flow.log("task_contrib_idle", task=flow.current.task_id,
                         idle=[int(m) for m in contrib_idle])
            # [RFC-009 2단계 정수 — 발언→책임] 회의록(meet 미니츠는 '[NR] 직군: 발언'으로 화자
            # 귀속)에서 잠수 직군 '본인의 발언'을 끌어와 게이트에 그대로 되돌린다 — "당신이 회의에서
            # 한 이 말이 산출물에 들어갔나?"(발언≠구현). 직군 키워드 없이 본인 발언만 에코(보편
            # 이치). 발언은 collab_notes로 Work 위임에 자동 동봉되므로(577·1562) ①로 맡기면 본인
            # 약속이 구현자=본인에게 전달돼 루프가 닫힌다 — 별도 '발언→Task' 게이트가 불필요(중복).
            notes_lines = (getattr(flow.current, "collab_notes", "") or "").splitlines()
            commits = []
            for m in contrib_idle:
                role = (flow._info(m) or "").strip()
                said = [ln.split(":", 1)[1].strip() for ln in notes_lines
                        if role and f"] {role}:" in ln]
                said = [s for s in said if s]
                if said:
                    commits.append(f"· {role}: “{_speech_clip(' / '.join(said), 240)}”")
            commit_note = ("\n[회의 발언 대조 — 발언≠구현] 아래는 이 직군들이 회의에서 한 말입니다 — "
                           "각 발언이 실제 산출물에 반영됐는지 직접 확인하고, 안 됐으면 ①로 맡기세요:\n"
                           + "\n".join(commits)) if commits else ""
            if _absorbed and flow.log:
                flow.log("task_absorbed_blocked", task=flow.current.task_id,
                         absorbed=[int(m) for m in _absorbed])
            absorbed_note = (
                f"\n\n⚠ [흡수 차단 — {flow._names(_absorbed)}은(는) '[기여 불필요]'로 넘길 수 없습니다] "
                f"이들은 **회의에서 의견을 냈는데 이 Task에서 Work 위임을 한 번도 못 받고** 실작업 0입니다 — "
                f"그 전문 도메인이 다른 사람에게 **흡수**된 것입니다(리더 독점의 핵심: 전문가에게 안 맡기고 "
                f"제너럴리스트가 그 도메인까지 다 써버림). 반드시 이들에게 request(Work)로 **실제로 맡기세요**(①) — "
                f"그래야 흡수가 풀립니다. 정말 불필요했으면 팀에서 빼세요(②). 한 번 위임한 뒤에도 본인이 안 하면 "
                f"그땐 [기여 불필요]로 마감 가능합니다(기회는 줬으니)." if _absorbed else "")
            return _ok(
                f"완료 보류(팀 기여 의무 — 증거/명시 필요, 반사적 재호출로는 통과 안 됨): 팀의 "
                f"{flow._names(contrib_idle)}이(가) 이 흐름에서 **회의 발언 외 실작업·검증이 0**입니다"
                f"(Write/Edit/run 0회) — 이 직군의 도메인('되는가'를 넘는 그 직군의 품질·폴리시)이 "
                f"**작품에 반영되지 않았습니다**. 셋 중 하나를 택하세요: ① 필요한 도메인이면 request(Work)로 "
                f"맡겨 **실제로 만들게** 하고 그 산출물을 교차 검증까지 받으세요 ② 애초에 불필요했으면 "
                f"팀에서 빼세요(왜 불렀나=다음 학습) ③ 정말 불필요하면 result **첫 줄/본문에 "
                f"'[기여 불필요] <이유>'**를 적어 재호출하세요(의식적 판단 — 그냥 재호출로는 통과 안 됨; "
                f"'이 직군들을 뺀 채 마감'이 Task 기록에 남습니다). 특히 회의에서 '중요하다'고 한 "
                f"부분이 실제 산출물에 들어갔는지 확인하세요 — 발언만으로는 작품이 바뀌지 않습니다."
                f"{commit_note}{absorbed_note}")
        flow.current.contrib_checked = True   # 기여(idle 해소)·명시 확보 → 이 Task에선 다시 묻지 않음
    # [저작 다양성 게이트 제거 — 2026-06-21 아키텍처 감사] 기존 '1~2직군이 ≥80% 저작(≥6파일) = 모놀리스'는
    # 순수 숫자추측(80%/6/2)이라 ① 빗나갔고(P-027: 3직군 57%인데 전문가 2명이 0저작인데도 미발동) ② '부른
    # 전문가가 실제로 일했나'는 바로 위 기여 게이트가 act_by==0(관계적)로 더 정확히 잡아 *중복*이었다. 사용자
    # 지적("의미없는 숫자기반 추측 제거")에 따라 숫자 휴리스틱 게이트를 폐기 — 관계적 기여 게이트로 일원화한다.
    # ('전문가를 애초에 안 부른' 과소채용은 완료시 저작%가 아니라 채용·기획 단계의 문제이고 프롬프트가 이미 유도.)
    done_ref = flow.current
    # 허위보고 차단(도메인 무관): 완료의 '진짜'는 에이전트 산문이 아니라 시스템이 캡처한 실행 영수증.
    # 코드는 합격/불합격을 판단하지 않고(하드코딩·QA역할 가정 X), 보고 옆에 실제 출력을 떼어낼 수 없게 묶는다.
    report = _speech_clip(args.get("result") or "", 800)   # Task 블록(Discord 2000 한도) 안에 들어가는 요약
    # [침묵 강행 불가] 검증 분업 보류를 재호출로 강행한 '단독 마감'은 기록에 그렇게 보이게 한다
    # ("자를 수는 있어도 조용히는 못 자른다"의 마감 버전) — 행동은 막지 않되(자동 회사·리더 판단),
    # 사후 분석·사용자가 한눈에 보게(범용 이치의 구조 잠금, 사용자 승인 2026-06-12).
    solo = bool((flow.current.owner or getattr(flow.current, "leader_writes", 0) > 0)
                and flow.current.cross_checks == 0)
    if solo and flow.log:
        flow.log("task_solo_completed", task=flow.current.task_id, owner=int(flow.current.owner or 0))
    # [기여 미흡 마감 가시화 — RFC-009, 침묵 강행 불가] 게이트 1회 보류를 재호출로 통과해(옵션③)
    # 잠수 직군이 여전히 실작업 0인 채 마감되면, '이 직군들을 뺀 채 마감'을 결과에 박아 영속한다 —
    # 라이브 3/3 게이트가 전부 반사적 재호출로 통과해 폴리시가 또 빠짐(사용자 지적). 행동은 막지
    # 않되(리더 자율) 사후 분석·사용자·학습이 한눈에 보게(단독 마감 마커와 같은 정신). 직군 키워드 없음.
    contrib_idle_now = [m for m in third if flow.act_by.get(m, 0) == 0] if has_product else []
    if contrib_idle_now and flow.log:
        flow.log("task_contrib_overridden", task=flow.current.task_id,
                 idle=[int(m) for m in contrib_idle_now])
    done_ref.status.status = "완료"
    # [보고=관찰, 주장 아님 — '거짓말'의 *핵심* 교정(2026-06-20 라이브 P-025)] 봇 narrative의 URL은
    # confabulate된다(봇이 *요청한* 이름 taas-…를 URL로 보고 → 404; 실제 배포는 시스템이 캐논 슬롯
    # organt-p-NNN으로 _check_live 검증해 flow.deployed에 보유). 게이트를 차원마다 N개 더 다는 대신,
    # 시스템이 *관찰한 사실*(검증된 배포 URL)을 *권위*로 주입하고 봇이 보고한 다른 onrender URL은 '무효'로
    # 박는다 — 이미 있는 [시스템 실행기록](run 영수증) 주입과 같은 원리를 배포 URL로 확장(보고를 관찰에 묶음).
    _url_truth = ""
    _auth = re.search(r"https://[a-z0-9-]+\.onrender\.com", str(getattr(flow, "deployed", "") or ""))
    if _auth:
        _au = _auth.group(0)
        _wrong = {u for u in re.findall(r"https://[a-z0-9-]+\.onrender\.com", report) if u != _au}
        _url_truth = (f"[시스템 검증 — 라이브 URL(권위)] {_au}"
                      + (f"  ⚠ 봇 보고 {', '.join(sorted(_wrong))} 는 배포된 적 없는 무효 링크"
                         if _wrong else "") + "\n")
    done_ref.status.result = (
        _url_truth
        + (f"[검증: 단독 마감 — 교차 검증 0, 리더 판정만]\n" if solo else "")
        + (f"[기여 미흡: {flow._names(contrib_idle_now)} 실작업 0 — 리더 판단으로 마감(폴리시 미반영 가능)]\n"
           if contrib_idle_now else "")
        + f"[보고] {report}\n"
        f"[시스템 실행기록 {done_ref.run_count}회·마지막] {done_ref.evidence or '(없음)'}"
    )[:1400]
    await flow.refresh(done_ref)
    await _react(g, flow.project_channel, done_ref.block_id, "✅")  # 완료=이모지
    flow.current = None
    _ckpt(flow)                       # 크래시-세이프: 마감 즉시 '미완 없음'으로 영속(유령 복원 방지)
    return _ok(f"task={done_ref.task_id} 완료 마감 (시스템 실행기록 {done_ref.run_count}회 첨부)")
