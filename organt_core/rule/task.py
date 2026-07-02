"""[Task Rule] 작업(Task) 완료·인수 검증 규칙 — 원래 설계(REWORK_DESIGN §7 rule/task.py) 복원.
잘못된 구현이 guide_tools에 병합했던 Task 완료 게이트(실제 제작자원·시각 런타임·데이터 출처·QA 검증)를
여기로 되돌린다. 전부 순수 함수(workspace/text/labels → bool/…): Organt이 complete_task로 마감을
선언할 때 SYS가 강제하는 *광역 Task Rule*. guide_tools는 이 모듈을 import해 도구에서 소비한다."""
import os


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
