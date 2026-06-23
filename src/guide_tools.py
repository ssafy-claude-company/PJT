"""Organt가 쓰는 Guide 도구셋 (P2P Communication + 다중 Task + 팀 배정 모델).

회사식 인력 구조: **채용 풀(전체 로스터) → 프로젝트 팀(규모 산정해 배정) → Task 팀(필요 인원)**.
- 깨어난 Organt는 `request`로 *현재 Task 팀의 동료*에게 요청한다(Info=질문/Work=작업).
- 인원이 부족하면 `recruit`로 풀에서 현재 Task에 합류시킨다("더 필요하면 더 가져온다").
SYS가 대상 동료를 중첩 베턴으로 깨워(flow.wake) 응답을 돌려준다 → 항상 1명만 활성(단일흐름).

리더(첫 Organt)는 추가로:
- create_project(name, team): 규모를 산정해 프로젝트 팀 배정 + 전용 채널 생성
- create_task(purpose, goal, members): Task에 필요한 인원 배정 + 상태블록/Thread 생성(반복 가능)
- complete_task(result): 현재 Task를 완료로 마감
대화는 '현재 Task' 스레드에서. 보고는 별도 툴이 아니라 반환값(=Response)이 origin까지 unwind.
"""
import asyncio
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional

import anyio

from claude_agent_sdk import create_sdk_mcp_server, tool

from .communication import BusyInOtherFlow, CommError, CommunicationManager, RedoLimitExceeded
from .protocol import Kind, TaskStatus

_DEBUG = bool(os.environ.get("ORGANT_DEBUG"))


def _dbg(msg):
    """진단 로그(기본 off). ORGANT_DEBUG 설정 시에만 stdout으로."""
    if _DEBUG:
        print(msg, flush=True)


ORIGIN = 0
REQUEST_TOOL = "mcp__guide__request"
RECRUIT_TOOL = "mcp__guide__recruit"
RUN_TOOL = "mcp__guide__run"
# 모든 Organt 공통 흐름 도구(요청/채용/실행검증). 리더 전용 셋업 도구는 LEADER_TOOLS.
FLOW_TOOLS = [REQUEST_TOOL, RECRUIT_TOOL, RUN_TOOL]
# 리더(코디네이터) 흐름 도구: 조율만(run 없음) — 구현·실행은 owner/QA가 한다.
COORD_TOOLS = [REQUEST_TOOL, RECRUIT_TOOL]
LEADER_TOOLS = [f"mcp__guide__{n}" for n in
                ("create_project", "create_task", "set_goal", "complete_task", "deploy", "send_file",
                 "vote", "meet", "parallel_work")]

# run 툴 안전 차단: 파괴/탈출/저장소·시스템 경로/네트워크 외 명령은 막는다(npm·node·curl·python은 허용).
_RUN_DENY = ("rm -rf", "rm -r ", "sudo", "shutdown", "reboot", "mkfs", "dd if=", ":(){",
             "git ", "/home/user/pjt", "/etc/", "/usr/", "/root", "> /", "chmod ", "chown ",
             "pkill", "kill -9 1 ", "wget ", "ssh ", "scp ", "npm publish", "history")
# run으로 '파일 작성'(heredoc·cat>·tee)을 막는다 — 산출물 작성/수정은 Write/Edit로 해야 권한·협의
# 게이트(협의 중 선구현 금지)가 적용되고 '누가 무엇을 만들었나'가 기록된다. run은 실행·빌드·검증 전용.
# (이 백도어로 리더가 위임 없이 전부 혼자 작성해 독점하거나, 협의 단계 동료가 선구현하는 걸 차단.)
_RUN_AUTHOR = ("<<", "cat >", "cat>", "tee ", "tee\t")


def _resolve_members(spec, flow, allowed) -> List[int]:
    """'12, 백엔드A' 처럼 id 또는 역할명으로 동료를 지정 → allowed 안의 id 리스트(중복 제거)."""
    out: List[int] = []
    for tok in str(spec or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.lstrip("-").isdigit():
            v = int(tok)
            if v in allowed and v not in out:
                out.append(v)
        else:  # 역할명(부분일치)로도 지정 가능
            for i in allowed:
                if i not in out and tok.lower() in (flow._info(i) or "").lower():
                    out.append(i)
                    break
    return out


def _uniq(xs) -> List[int]:
    seen: List[int] = []
    for x in xs:
        if x not in seen:
            seen.append(x)
    return seen


def _looks_transient(text: str) -> bool:
    """동료 응답이 일시적 API 오류로 보이는지 — 그렇다면 답으로 취급하지 말고 재시도."""
    t = (text or "").strip().lower()
    return t.startswith("api error") or t.startswith("(동료 처리 중 오류")


# [실제 제작 자원 검증 — percept 마감 게이트의 증거(2026-06-15)] '코드 아닌 실재 자원'(사운드·이미지·3D·
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


def _is_verifier(label) -> bool:
    """역할 라벨이 '검증/품질(QA) 기능'인가 — 전체·사용자관점 최종 인수의 자연 담당."""
    t = str(label or "").lower()
    return any(h in t for h in _VERIFIER_HINTS)


# [스태핑 커버리지 — 리더 흡수 차단(2026-06-19, 사용자: '전문가 분배 무조건, 리더는 자기 직군만')]
# 기존 게이트(#4 owner도메인 대리구현 금지 / #6 리더독식)는 '전문가가 *있으면*' 리더 흡수를 막지만,
# 리더가 그 도메인 전문가를 *안 뽑으면*(언더스태핑) 보호할 owner가 없어 리더가 흡수한다(라이브 P-022:
# 'AI를 학습' 요청에 AI엔지니어 미투입 → 백엔드 리더가 AI·data 53건 흡수). 그래서 set_goal에서 '목표가
# *명시적으로* 부른 전문 능력을 팀이 보유했나'를 본다 — 없으면 recruit 강제(그러면 owner가 박혀 기존
# #4가 자동으로 리더를 자기 직군에 가둠). 기능 식별(능력 needs↔팀 라벨)이라 직군 타이틀 하드코딩이 아니다.
# 고신호 능력만(오발 최소). 새 능력은 (이름, needs(text)→bool, providers(label keywords)) 한 줄로 확장.
def _kw(*kws):
    """키워드 중 하나라도 문자열에 있으면 True인 술어 생성(능력 need/cover 판정용)."""
    return lambda s: any(k in s for k in kws)


# 능력 표 — (표시명, need(goal 소문자)→bool, cover(labels 소문자 합본)→bool). 고신호만(과채용 최소):
# 그 능력이 *작업의 실질 축*일 때만 need=True. cover는 관대(누군가 plausibly 덮으면 갭 아님).
# 일반화 동기(2026-06-22 사용자 '데브옵스·DBA 채용이 안 보인다'): 단일 AI/ML만 보던 탓에 반복 수요인
# 공공데이터 수집이 게이트에 안 걸려 흡수됐고(실데이터를 합성·가짜로 위장하는 사고의 *상류* 원인),
# 배포 인프라는 아무도 전담 안 해 리더에 귀속됐다(P-028 배포 1인 루프). 기능으로 식별(직군 타이틀 X).
_CAPS = [
    # AI/ML 모델링 — 모델 학습·예측이 핵심인데 AI/ML 직군이 없을 때(백엔드는 cover 아님 — 별도 전문성).
    ("AI/ML(모델 학습·예측)",
     lambda t: (_kw("학습시키", "머신러닝", "딥러닝", "신경망", "ml 모델", "예측 모델", "ai 모델")(t)
                or ("ai" in t and _kw("학습", "예측", "모델")(t))),
     _kw("ai", "머신", "딥러닝", "인공지능", "ml", "데이터 과학", "데이터 사이언", "data scien", "machine learn")),
    # 실데이터 수집·파이프라인 — 실/공공 데이터를 받아와 쓰는 게 전제일 때(백엔드/AI가 흡수하던 영역이라
    # 백엔드는 cover 아님 — 전담 데이터 직군 강제 → 데이터엔지니어↔AI엔지니어 핸드오프 협업도 생긴다).
    ("실데이터 수집·파이프라인",
     lambda t: (_kw("공공데이터", "공공 데이터", "실데이터", "실제 데이터", "오픈데이터", "open data")(t)
                and _kw("받아", "수집", "연동", "활용", "파이프라인", "크롤", "가져", "fetch", "적재")(t)),
     _kw("데이터 엔지니", "데이터엔지니", "data eng", "데이터 수집", "데이터 파이프", "etl", "데이터 분석")),
    # 데이터 영속·DB — 계정·기록·랭킹 등 지속 저장이 핵심일 때. 기본 CRUD는 백엔드가 덮으니 백엔드·DBA가
    # 둘 다 없을 때만 갭(과채용 방지 — 백엔드 있으면 발동 안 함).
    ("데이터 영속·DB",
     _kw("데이터베이스", "데이터 베이스", "database", "영속 저장", "계정", "로그인", "회원가입",
         "랭킹 저장", "기록 저장", "쿼리 최적"),
     _kw("dba", "데이터베이스", "데이터 베이스", "백엔드", "backend", "서버 개발")),
    # 배포·인프라(DevOps) — 배포 파이프라인·운영 자동화가 *명시적으로* 요구될 때만(평범한 웹 배포는 표준
    # 파이프라인이 처리 → 안 걸림). 키워드를 좁혀 과채용 방지.
    ("배포·인프라(DevOps)",
     _kw("ci/cd", "cicd", "파이프라인 구축", "도커", "컨테이너 오케", "쿠버네티스", "kubernetes",
         "오토스케일", "무중단", "로드밸런", "인프라 구축", "운영 자동화", "sre"),
     _kw("devops", "데브옵스", "인프라", "sre", "배포 엔지니", "플랫폼 엔지니")),
]


def _capability_gaps(goal_text, labels):
    """목표가 요구하는 전문 능력 중 팀(라벨들)이 *아무도 보유 못 한* 것 — 능력명 리스트. 리더가 자기 직군
    밖 도메인을 흡수(언더스태핑)하는 걸 set_goal에서 잡기 위함. 기능 식별(직군 타이틀 하드코딩 아님)."""
    t = str(goal_text or "").lower()
    have = " ".join(str(l or "").lower() for l in (labels or []))
    return [name for name, need, covered in _CAPS if need(t) and not covered(have)]


def _needed_caps_coverage(goal_text, labels):
    """목표가 *요구하는* 능력(need True)별 '덮는 팀원 수' {능력명: 수}. 깊이 게이트가 '필요 능력이 다 1명뿐'
    (그 도메인 품질이 한 사람 지능에 인질)인지 보는 데 쓴다 — 갭(0)은 staffing이 먼저 잡으므로 여기선 1명 이상 전제."""
    t = str(goal_text or "").lower()
    out = {}
    for name, need, covered in _CAPS:
        if need(t):
            out[name] = sum(1 for l in (labels or []) if covered(str(l or "").lower()))
    return out


def _offdomain_capability_hit(flow, to, body):
    """[직군밖 사전 차단 — P4 직군밖 거부 부활(2026-06-22)] Work body가 요구하는 능력(_CAPS need) 중 수신자(to)
    직군이 못 덮고 *다른* 팀원(리더 제외)이 덮는 것 → {능력명: [멤버]}. 비면 직군밖 아님(또는 덮는 전문가가
    없어 staffing 영역). 종전 [직군밖]는 받은 봇이 거부하는 사후 채널인데 1회만 쓰였다(봇은 받으면 그냥 흡수)
    — 이건 *위임 전에* 능력표로 잡아 그 전문가에게 리다이렉트(P-022 백엔드가 AI·data 흡수 차단). 의식적 예외는
    body '[직군초과: 사유]'. 능력표 밖 도메인(사운드↔VFX 등)은 봇-side [직군밖] 반려가 백스톱."""
    if "[직군초과" in (body or ""):
        return {}
    tl = (flow._info(to) or "").lower()
    bn = [name for name, need, covered in _CAPS if need((body or "").lower()) and not covered(tl)]
    if not bn:
        return {}
    hit = {}
    for name, need, cov in _CAPS:
        if name in bn:
            ms = [m for m in flow.current.team if m != to and m != flow.leader
                  and cov((flow._info(m) or "").lower())]
            if ms:
                hit[name] = ms
    return hit


# 채용 대기 인력(직군 미배정). recruit(role=…)로 런타임에 '게임 기획자·UX 디자이너' 등 필요한 직군으로
# 채용해 합류시킨다. 로스터에서 라벨이 '예비'인 봇들이며, 첫 '전원 기획'엔 안 들어가고 필요할 때 합류한다.
_SPARE_LABEL = "예비"


def _is_spare(flow, oid) -> bool:
    return (flow._info(oid) or "").strip().startswith(_SPARE_LABEL)


def _norm_job(name: str) -> str:
    return " ".join((name or "").split()).casefold()


# 겸직 라벨 구분자: '백엔드·QA' = 주직군 + 부직군. 겸직은 예외(예비 0명 또는 유사 직무)에서만,
# 봇당 최대 2개 — 더하기만 하던 시절의 '직군 5~6개 스택'(라이브 관측)으로 회귀하지 않기 위한 한도.
_JOB_SEP = "·"


def _jobs_of(label) -> List[str]:
    """라벨 → 보유 직군 목록('백엔드·QA' → ['백엔드','QA']). 단일 직군이면 1개짜리 리스트."""
    return [j.strip() for j in str(label or "").split(_JOB_SEP) if j.strip()]


def _job_tokens(name: str):
    return {t.casefold() for t in (name or "").split() if t}


def _free_alternatives(flow, me_id, to) -> str:
    """[전역 점유] 타 흐름에 점유된 to 대신 '지금 가용한 같은 직군 동료'와 채용 옵션을 안내문으로.
    재시도(폴링) 대신 구조적 선택지를 줘서, 점유 거부가 막다른 길이 아니라 분기점이 되게 한다."""
    eng, scope = flow.comm.engagement, flow.comm.scope
    jobs = {_norm_job(j) for j in _jobs_of(flow._info(to) or "")} - {""}
    alts = []
    for b in flow.pool:
        if b in (to, me_id) or _is_spare(flow, b):
            continue
        if jobs and not (jobs & {_norm_job(j) for j in _jobs_of(flow._info(b) or "")}):
            continue
        if eng is not None and scope is not None and eng.busy_elsewhere(b, scope):
            continue
        alts.append(f"{flow._info(b)}(id {b})")
    spares = [s for s in flow.pool if _is_spare(flow, s)]
    parts = []
    if alts:
        parts.append("지금 가용한 같은 직군 동료: " + ", ".join(alts[:4]))
    if spares:
        parts.append(f"또는 recruit(role=…)로 예비 {len(spares)}명 중 채용")
    return ("; ".join(parts) if parts else
            "지금은 같은 직군의 가용 동료가 없습니다 — 다른 직군 동료로 진행 가능한 부분을 먼저 하거나, "
            "불가하면 그 사정을 보고에 남기세요")


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


async def _fork_collect(flow, me_id, members, body_of, kind=Kind.INFO):
    """[병렬 Info fork-join] '독립 의견 수집'(표결·회의 1라운드)을 동시에 돈다 — Communication.md
    13–14행("여럿(병렬)은 이 제약을 완화하는 Feature로 둔다")의 구현. 완화는 정확히 이 구간뿐:
    - 가지(branch)는 comm 프레임을 열지 않는다 → 가지 봇은 '활성'이 아니므로 request가 규약
      에러로 자연 차단된다(가지의 중첩 요청 금지가 프롬프트가 아니라 구조로 강제 — 답만 한다).
    - 회사 풀 관점은 전역 점유로 일관: 수집 동안 가지 봇은 점유돼 타 흐름이 못 집어가고, 끝나면
      즉시 풀로 돌아간다. 타 흐름 점유/이 흐름에서 위임 보유 중인 멤버는 건너뛴다(부분 조인 —
      일부 멤버 때문에 수집 전체가 막히지 않는다).
    - 행 안전: 각 가지는 워커 침묵 워치독이 종결을 보장 → 조인이 영원히 안 닫히는 일이 구조적으로
      없다. 동시 폭은 ORGANT_FORK_FAN(기본 3)으로 묶는다(토큰 속도 운영 노브, 1이면 직렬과 동일).
    kind: 가지의 작업 종류 — Info(의견 수집, 기본)면 훅이 가지의 선구현(Write/Edit)을 종전대로
    차단한다(flow.fork_kind로 프레임 없는 가지에 게이트 연결; Work 가지는 휴면 — 호출부 없음).
    수집 동안 flow.fork_active를 올려 신규 요청/중첩 수집을 [대기]로 막는다 — CLI가 같은 턴에
    병렬 도구 호출을 내도(vote+request 등) 가지와 같은 동료를 이중으로 깨우는 일이 구조적으로 없다.
    반환: 멤버 순서 보존 [(member, res|None, 제외/실패 사유)]."""
    eng, scope = flow.comm.engagement, flow.comm.scope
    sem = asyncio.Semaphore(max(1, int(os.environ.get("ORGANT_FORK_FAN", "3"))))

    async def _branch(m):
        if flow.comm.is_busy(m):
            return (m, None, "(이 흐름에서 진행 중인 위임 보유 — 이번 수집에서 제외)")
        if eng is not None and scope is not None and eng.busy_elsewhere(m, scope):
            return (m, None, f"(타 흐름({eng.holder(m)}) 참여 중 — 이번 수집에서 제외)")
        if eng is not None and scope is not None:
            eng.engage(m, scope)
        flow.fork_kind[m] = kind
        try:
            async with sem:
                return (m, await flow.wake(m, body_of(m), kind), "")
        except Exception as e:
            return (m, None, f"(수집 실패: {e})")
        finally:
            flow.fork_kind.pop(m, None)
            if eng is not None and scope is not None and not flow.comm.is_busy(m):
                eng.release(m, scope)

    flow.fork_active = getattr(flow, "fork_active", 0) + 1
    try:
        return list(await asyncio.gather(*(_branch(m) for m in members)))
    finally:
        flow.fork_active -= 1


def _find_variant_job(name: str, existing) -> Optional[str]:
    """기존 직군과 '이름은 다른데 토큰을 공유'하면 변형(중복 생성) 의심으로 그 기존 직군을 돌려준다.
    recruit가 자유 텍스트 직군명을 받다 보니 흐름마다 'VFX 전문가'/'VFX 아티스트' 같은 변형이 새 역할로
    계속 불어났다(중복 생성 오류의 뿌리). 무엇이 '정답 이름'인지는 시스템이 정하지 않는다(하드코딩 금지)
    — 같은 이름(공백·대소문자 무시)은 기존 역할 재사용이라 통과시키고, 변형만 멈춰 세워 에이전트가
    '재사용'인지 '진짜 새 직군'인지 명시하게 한다."""
    mine_n, mine_t = _norm_job(name), _job_tokens(name)
    if not mine_t:
        return None
    if any(_norm_job(ex) == mine_n for ex in existing):
        return None                        # 같은 이름이 이미 있음 → 그대로 재사용(변형 아님), 즉시 통과
    for ex in sorted(existing):            # 정렬: 같은 입력엔 같은 안내(메시지 결정성)
        if mine_t & _job_tokens(ex):
            return ex
    return None


# 협의로 '인정되는' Info인지 — 순수 응답확인 핑('응답 가능하신가요?')은 합의로 치지 않는다(빈 핑 차단).
# 짧은데 핑 문구가 거의 전부일 때만 비실질(긴 메시지는 핑 문구가 섞여도 실질로 본다).
_HOLLOW_PING = ("응답 가능", "응답가능", "응답 되시", "응답되시", "계신가요", "준비되셨", "들리시",
                "확인 가능하신", "ready?", "available?", "are you there", "are you available")


def _is_substantive(body: str) -> bool:
    b = (body or "").strip()
    if not b:
        return False
    low = b.lower()
    return not (len(b) <= 30 and any(h in low for h in _HOLLOW_PING))


def _reap_pgroup(pgid: int):
    """프로세스그룹 pgid에 남은 프로세스를 모두 종료한다(백그라운드 서버 누수 차단).
    셸을 self-session으로 띄우면 모든 자손이 pgid==셸pid를 공유한다. 다만 리더(셸)가
    먼저 끝나 reap되면 '고아 프로세스그룹'이 돼 killpg가 안 먹으므로, /proc를 훑어
    pgid가 같은 잔여 프로세스를 PID로 직접 SIGKILL한다(이게 run 간 포트충돌의 구조적 해결)."""
    try:
        os.killpg(pgid, signal.SIGKILL)   # 리더 생존 시 빠른 경로
    except (ProcessLookupError, PermissionError, OSError):
        pass
    me = os.getpid()
    try:
        entries = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return
    for d in entries:
        pid = int(d)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                data = f.read()
            # stat: 'pid (comm) state ppid pgrp ...' → comm의 마지막 ')' 뒤 3번째가 pgrp
            if int(data[data.rindex(b")") + 1:].split()[2]) == pgid:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ValueError, IndexError):
            continue


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


class Flow:
    """하나의 활성 흐름(단일흐름 보존). 풀→프로젝트 팀→Task 팀으로 인력을 구조적으로 배정."""

    def __init__(self, guide, channel_id, guild_id, leader_id, bot_info=None):
        self.guide = guide
        self.user_channel = channel_id
        self.guild_id = guild_id
        self.leader = leader_id
        self.bot_info = bot_info or {}
        self.comm = CommunicationManager(ORIGIN)
        self.pool = list((bot_info or {}).keys()) or [leader_id]   # 채용 가능 전체(로스터)
        if leader_id not in self.pool:
            self.pool.insert(0, leader_id)
        # 기본 프로젝트 팀 = 직군 보유자(예비 제외) — 예비는 '채용 대기'라 기본 팀에 안 넣는다(recruit로만 합류).
        # 리더는 예비여도 포함. 담당자가 create_project/create_task로 더 좁히거나 recruit로 직군을 채운다.
        self.project_team: List[int] = [m for m in self.pool if m == leader_id
                                        or not str((bot_info or {}).get(m, "")).startswith("예비")]
        self.project_channel: Optional[int] = None
        self.tasks: List[TaskRef] = []
        self.current: Optional[TaskRef] = None
        self._base = time.strftime("%H%M%S")
        self._n = 0
        self.done = False
        self.final: Optional[str] = None
        self.root_id: Optional[str] = None
        self.advice = []
        self.workspace = None   # run 툴 cwd(작업공간 경로). SYS가 주입.
        self.wake = None   # async (to_id, body, kind) -> result text  (SYS가 주입)
        self.register_project = None   # (channel_id, name) -> project_id (SYS 주입)
        self.project_id = None         # [Project-XXXX] 식별번호
        self.intervention = None       # 기존 프로젝트 개입이면 그 정보(dict)
        self.origin_request = ""       # 이 흐름의 '사용자 원문 요청'(SYS 주입) — 모든 프롬프트에 '진짜 의도'로 주입.
                                       #   반드시 흐름별 격리: 전역 단일 필드면 동시 흐름이 서로의 원문을 덮어써
                                       #   한 프로젝트의 봇이 '남의 프로젝트 원문'을 진짜 의도로 받아 엉뚱한 걸
                                       #   만든다(라이브: 웹 흐름이 게임 개입 원문을 받아 게임을 짓기 시작).
        self.deployed = None           # deploy 툴이 불리면 결과 문자열(배포 강제용 추적)
        self._deploy_count = 0         # [런어웨이 차단] 흐름당 실배포 횟수 — 상한 넘으면 차단+사용자 보고로
                                       #   에스컬레이트(라이브 P-028: 깨진 배포를 코드 바꿔가며 23회 재배포한 루프 방지)
        self.pending_clarify = None    # 위임자에게 되묻기(확인요청 반환) 임시 보관
        self.pending_coordination = [] # [리더 조율 강제(2026-06-23)] 게이트가 막은 비-리더 교차도메인 Work를
                                       #   리더 다음 턴에 'SYS 확인 사실'로 주입할 큐 — 워커가 핑계로 보고하고
                                       #   리더가 묵살·재발사하던 루프(P-030 backend2↔PM 핑퐁) 차단. 리더가
                                       #   직접 그 도메인 전문가에게 위임하게 한다(sys_core continue 루프에서 소비).
        self.leader_segment = 0        # 리더 턴 세그먼트 번호(시작=1, continue마다 +1) — 관측용
        self.req_results = {}          # (seg,from,to,kind,body)->응답: 같은 턴 병렬 중복요청 합치기용 캐시
        self.act_count = 0             # 작업공간 변경(run/Write/Edit) 누계 — 훅이 +1. '위임 도중 owner가 실제로
                                       #   일했나'를 wake 전후 스냅샷 차이로 판정(허위완료/독점 차단)
        self.act_by = {}               # 행위자별 작업 누계(actor→count) — 요청자 자신의 활동을 빼고 재기 위함
        self._stall_victim = None      # [막힘 흡수 차단] 하위 담당이 막혀 베턴이 위임자에게 되돌아온 순간, 막힌 사람 id를
        self._stall_victim_acts = 0    #   기록. 위임자가 '내가 하지'로 그 사람 일을 흡수하는 걸 게이트가 막고 '같은 사람 재요청'을
        self._stall_blocks = 0         #   유도(재채용 X). 막힌 사람이 다시 act하면 해제, 끝내 무응답이면 N회 후 폴백(교착 방지).
        self._gate_pass = set()        # [per-Task 게이트(2026-06-20 전수검사)] 통과한 (게이트명, task_id) 집합 —
                                       #   percept·acceptance·data_prov를 *흐름당 1회*(과의존)가 아니라 *산출물(Task)별*로
                                       #   강제한다(다중-Task서 첫 Task만 검사하던 구멍 차단). bool 플래그(X_checked)는
                                       #   *테스트 우회*로만 남긴다(프로덕션은 이 집합 + task_id로 판정 → 우회와 분리).
        self.writes_by_role = {}       # [메커니즘② 저작 다양성] 직군별 파일 저작(Write/Edit, run 제외) 누계. 완료 시
                                       #   '한 직군이 산출물을 독점'(P-017: 백엔드 혼자 20중 19, 단일 app.js)을 출구
                                       #   게이트가 잡는다 — '분리 모듈은 분리 전문가가 있을 때만 존재'(라이브 규명).
        self.tentative_roles = {}      # [일로 직업 획득 — 영속 이연] 예비→직군 채용은 *잠정*(런타임 bot_info만). 영속
                                       #   (jobs.json+Discord)은 그 봇이 *첫 실작업*을 한 순간에만 — '직업=기억'을 문자
                                       #   그대로. 일 안 하면 영속 안 돼 다음 흐름에 예비로 사라짐 → '0-기억 recruit
                                       #   직군'이 구조적으로 불가(양산 래칫의 근본 차단). mid→직군명.
        self.role_earned_queue = []    # 첫 실작업으로 '획득'된 직군의 Discord 역할 부여 대기열(비동기) — SYS가 턴에서 드레인.
        self.consec_fail = 0           # 연속 '응답 실패(무응답/타임아웃)' 횟수 — 시스템 일시불안정 판별(충원 루프 차단)
        self.inflight_tasks = set()    # 진행 중 위임의 '완주 태스크'들 — CLI가 도구 호출을 포기해도 위임은
                                       #   계속 완주하며(중첩 가능), SYS가 이어가기 전에 이들의 완주를 기다린다
        self.detached_results = []     # 포기당한(detached) 위임의 완주 결과 — 이어가기 리더에게 전달
        self.handoff_inflight = {}     # [논블로킹 핸드오프] 요청자 id→그가 만든 인플라이트 위임. 중첩 위임을
                                       #   SYS가 호출 밖에서 직렬 완주시키고(블록킹 도구호출 없음 → CLI 75초
                                       #   포기·detach·비동기 churn 차단), 요청자가 활성일 때만 1건(베턴=단일).
        self.write_lease = {}          # 행위자→샌드박스(쓰기 리스, 휴면 인프라): 훅이 리스 밖 Write/Edit 거부
        # [소유-기반 도메인 경계(2026-06-23, 사용자) — 분류 아닌 *기록*] 파일 절대경로→생성 직군. 봇이
        # 새 파일을 만들면 그 직군이 owner. 타 직군이 그 파일을 Edit하려 하면 PreToolUse 훅이 막고
        # '보고/요청'으로 돌린다(키워드 분류 폐기 — 무한 하드코딩 종결). 프로젝트 단위로 projects.json에
        # 영속(복구 때 리셋 안 되게 — act_by·_gate_pass의 인메모리 결함 반복 차단). persist_owner는 SYS 주입.
        self.file_owner = {}           # realpath(str) → 직군(normalized). PostToolUse가 기록, PreToolUse가 강제.
        self.persist_owner = None      # () -> None: file_owner를 proj에 써 영속(SYS가 주입)
        self.fork_kind = {}            # [fork 수집] 행위자→Kind: 프레임 없는 가지에도 선구현 게이트 적용
        self.fork_active = 0           # [fork 동시성 가드] 수집 진행 수 — 수집 중 신규 요청/수집은 [대기]
        self.log = None                # (event, **fields) 콜백 — SYS가 주입(flow.jsonl 영속)

    def start_root(self, root_id):
        self.root_id = str(root_id)
        self.comm.request(ORIGIN, self.leader, root_id, Kind.WORK)

    def next_task_id(self) -> str:
        self._n += 1
        return f"{self._base}-{self._n}"

    async def refresh(self, task: Optional[TaskRef] = None):
        t = task or self.current
        if t and self.project_channel and t.block_id:
            await self.guide.update_status(self.project_channel, t.block_id, t.status)

    def _info(self, oid):
        return self.bot_info.get(oid, "")

    def _names(self, ids):
        return [self._info(i) or str(i) for i in ids]


# [배포 타겟 호환 — Render Node 전용(2026-06-22 P-028 규명)] deploy_sync는 Node만 빌드한다(runtime:node
# 하드코딩, package.json 필수). 흔한 사고: Node 서버가 *런타임*에 Python을 spawn/exec → Render Node 환경엔
# Python이 없어 백엔드가 안 떠 502(P-028: ECONNREFUSED:8001, 28모델 고아). 런어웨이 5회 상한은 *사후* 차단
# (그 전에 토큰·빌드 낭비) → *첫 배포 전에* 구조적으로 잡고 명확한 처방을 준다. (빌드타임 학습용 Python은 OK —
# 런타임 spawn/exec와 start 커맨드의 Python 실행만 차단.) 오발 최소: spawn/exec에 python/pip가 *명시*될 때만.
_RUNTIME_PY_RE = re.compile(
    r"(?:spawn|exec|execSync|execFile|fork)\s*\(\s*[`'\"]\s*(?:python|pip|gunicorn|uvicorn|flask|streamlit)",
    re.IGNORECASE)
_PY_START_RE = re.compile(r"\b(?:python3?|pip|gunicorn|uvicorn|flask|streamlit|fastapi|conda)\b", re.IGNORECASE)


def _deploy_infeasibility(workspace) -> str:
    """배포 타겟(Render Node)에서 못 뜨는 구조면 사유 문자열, 아니면 ''. ① package.json start/main이 Python류를
    직접 실행하나 ② .js 서버가 런타임에 python/pip를 spawn/exec 하나. (Python을 빌드타임 학습에만 쓰는 건 통과.)"""
    ws = str(workspace or "")
    if not ws or not os.path.isdir(ws):
        return ""
    pj = os.path.join(ws, "package.json")
    if os.path.isfile(pj):
        try:
            with open(pj, encoding="utf-8") as fh:
                data = json.load(fh)
            start = str((data.get("scripts") or {}).get("start") or "") + " " + str(data.get("main") or "")
            if _PY_START_RE.search(start):
                return f"package.json의 start/main이 Python류를 실행합니다('{start.strip()[:60]}')."
        except Exception:
            pass
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "dist", "build", ".next", "__pycache__")]
        for fn in files:
            if not fn.endswith((".js", ".mjs", ".cjs", ".ts")):
                continue
            try:
                with open(os.path.join(root, fn), encoding="utf-8", errors="ignore") as fh:
                    m = _RUNTIME_PY_RE.search(fh.read())
            except Exception:
                continue
            if m:
                return f"{os.path.relpath(os.path.join(root, fn), ws)}가 런타임에 Python을 spawn/exec 합니다('{m.group(0)[:40]}…')."
    return ""


def deploy_service_name(flow, arg_name: str = "") -> str:
    """배포 서비스명 결정 — [멀티 프로젝트] 등록 프로젝트는 식별번호(P-번호)로 **결정적으로**
    정한다: 같은 프로젝트는 늘 같은 서비스, 다른 프로젝트는 다른 서비스. 미등록 흐름은 슬롯이
    **없다**("") — 배포 신원은 프로젝트가 보증한다(사용자 설계 확인 2026-06-12: 배포는
    프로젝트마다. 과거의 DEPLOY_NAME env 폴백은 미등록 배포를 공유 슬롯(P-002 라이브 겸용
    todo-organt-demo)으로 보내 덮어쓰기 위험을 남겼었다). 에이전트 임의 명명(arg_name)은
    등록·미등록 어디서도 슬롯이 되지 못한다(작명 사고 차단)."""
    pname = getattr(flow, "project_name", None)
    pid = getattr(flow, "project_id", None)
    if pid:
        # [신원=번호] 등록 프로젝트의 슬롯은 무조건 식별번호 — 이름 슬러그를 쓰면 일반명사 이름이
        # 충돌할 때 다른 작품이 같은 슬롯을 덮어쓴다(라이브: 지진·모션이 대기질 슬롯을 연쇄 점유).
        return f"organt-{str(pid).lower()}"
    if pname:
        slug = re.sub(r"[^a-z0-9-]", "-", str(pname).lower()).strip("-")[:40]
        if slug:
            return f"organt-{slug}"
    return ""


def _ok(text):
    return {"content": [{"type": "text", "text": text}]}


def _group_of(flow, team):
    return [(f"<@{i}>", flow._info(i)) for i in team]


async def _react(g, channel_id, message_id, emoji):
    """이모지 반응(상태 표시). Guide에 react가 없으면(테스트 등) 조용히 건너뜀."""
    fn = getattr(g, "react", None)
    if fn:
        await fn(channel_id, message_id, emoji)


async def _add_members(g, thread_id, member_ids):
    """Task 스레드에 팀원 추가(멤버십=팀). Guide에 메서드 없으면 건너뜀."""
    fn = getattr(g, "add_thread_members", None)
    if fn:
        await fn(thread_id, member_ids)


def _speech_clip(s, n=1500) -> str:
    """발언 안전망: 폭주만 막고 **침묵 절단하지 않는다** — 잘리면 잘렸다고 표기한다.
    종전의 하드컷([:300]/[:400])은 '3~5줄' 지시를 지킨 발언(한국어 200~400자+)까지 단어
    중간에서 잘랐다(라이브: 회의 발언 전원이 307~308자로 박제, "…프론트엔"에서 끊김 — 사용자
    관측). 더 나쁜 건 회의록도 잘려 **다음 발언자들이 서로의 잘린 주장을 보고 토론**한 것 —
    분량 통제는 지시(프롬프트)와 모델 판단의 몫이고, 시스템은 안전망만 친다."""
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + f" …(발언 {len(s)}자 — {n}자 안전망에서 잘림)"


def make_guide_tools(flow: Flow, me_id: int, role: str):
    g = flow.guide
    tools = []

    async def _say(who, text):
        """회의·표결 발언을 '그 봇 본인 명의'로 스레드에 남긴다 — 4명의 독립 의견이 리더 명의
        [안내] 묶음으로 게시돼 '중앙 공지'처럼 보이던 착시(사용자 관측) 제거. 협업의 실체와
        가시성을 일치시킨다. 실패는 조용히(가시화는 best-effort, 흐름은 안 멈춤)."""
        try:
            if flow.current:
                await g.post(int(flow.current.thread_id), who, text)
        except Exception:
            pass

    @tool("request", "현재 Task 팀의 동료 한 명에게 요청(kind: Info=질문 / Work=작업, to_id 문자열)",
          {"to_id": str, "kind": str, "body": str})
    async def request(args):
        to = int(args["to_id"])
        kind = Kind.WORK if str(args["kind"]).strip().lower().startswith("w") else Kind.INFO
        body = args["body"]
        tag = f"[REQ] {me_id}({flow._info(me_id)})→{to}({flow._info(to)}) {getattr(kind, 'value', kind)}"
        if flow.current is None:
            _dbg(f"{tag} ✗거부:Task없음")
            return _ok("오류: 진행 중인 Task가 없습니다. (리더가 create_task 먼저 여세요.)")
        # 직군 미배정(예비) 봇에게는 위임/질의 불가 — 말로 '너는 X야' 하고 일을 시키는 걸 구조적으로 막는다.
        # 먼저 recruit(role='직군')로 실제 직군을 부여해야 그 봇이 일할 수 있다(말로만 배정 차단).
        if _is_spare(flow, to):
            _dbg(f"{tag} ✗거부:직군 미배정(예비)")
            return _ok(f"요청 거부: {flow._info(to) or to}는 아직 직군 미배정('예비')입니다 — 말로 직군을 정하지 말고 "
                       f"recruit(member='{to}', role='직군명')으로 직군을 실제로 부여한 뒤 요청하세요(직군이 부여돼야 일을 맡길 수 있음).")
        # 위임자에게 되묻기(확인요청 반환): 직속 위임자에게 Info로 물으면 '재진입 불가' 에러 대신
        # 베턴을 위임자에게 질문과 함께 돌려준다 — 위임자가 답하고 그 일을 다시 맡긴다(협업 가능).
        if kind == Kind.INFO and to == flow.comm.direct_delegator(me_id) and to != me_id:
            flow.pending_clarify = {"from": me_id, "to": to, "q": body}
            flow.comm.history.append(("clarify", me_id, to, "pending", Kind.INFO))
            _dbg(f"{tag} ↩확인요청→위임자")
            return _ok(f"확인요청을 직속 위임자({flow._info(to)})에게 전달했습니다. 지금 이 턴을 즉시 "
                       f"마치고(추가 도구 호출·추측 진행 금지) 짧게 반환하세요 — 위임자가 답한 뒤 이 작업을 "
                       f"당신에게 다시 맡깁니다.")
        if to not in flow.current.team:
            if to in flow.project_team:
                # 프로젝트 팀원이면 이 Task에 자동 합류 — Task 내 관련 인원을 최소화할 이유는 없다.
                flow.current.team.append(to)
                flow.current.status.group = _group_of(flow, flow.current.team)
                await flow.refresh()
                _dbg(f"{tag} +Task자동합류(프로젝트팀원)")
            elif to in flow.pool:
                # [원인 교정 — 정보가 있는 거부] 리더가 회사 풀(전체 로스터)과 프로젝트 팀을 혼동해
                # 팀 밖 동료를 반복 호출하던 라이브 관측(7회 우회, SIGTERM 기억구멍이 증폭)의 뿌리:
                # 거부가 '안 된다'만 말하고 '그 직군이 팀에 누구인지'를 안 알려줘 같은 실수가 반복됐다.
                # 올바른 대안(팀 내 같은 직군)과 현재 팀 명단을 동봉해 첫 거부에서 바로 교정되게 한다.
                same = [m for m in flow.project_team
                        if m != me_id and not _is_spare(flow, m)
                        and ({_norm_job(j) for j in _jobs_of(flow._info(to) or "")}
                             & {_norm_job(j) for j in _jobs_of(flow._info(m) or "")})]
                alt = (" 같은 직군의 **팀 내 동료**: "
                       + ", ".join(f"{flow._info(m)}(id {m})" for m in same)
                       + " — 이들에게 요청하세요(재시도 금지)." if same else
                       " 팀에 그 직군이 없습니다 — 정말 필요하면 recruit(member=…, role=…)로 합류시킨 뒤 요청하세요.")
                _dbg(f"{tag} ✗거부:프로젝트밖")
                return _ok(f"요청 거부: {to}({flow._info(to)})는 이 프로젝트 팀이 아닙니다 — 회사 풀에는 "
                           f"있지만 이 프로젝트 구성원이 아닙니다(팀은 create_project 때 당신이 구성했습니다)."
                           f"{alt} 현재 프로젝트 팀: {flow._names(flow.project_team)}")
            else:
                return _ok(f"요청 거부: {to}는 채용 풀에 없습니다. 풀: {flow._names(flow.pool)}")
        if flow.wake is None:
            return _ok("오류: 시스템 준비 안 됨")
        # 직렬화: 베턴이 내 차례가 될 때까지 대기(거부 아님). 서로 다른 동료로의 병렬 요청은 순차 처리되며,
        # 첫 요청이 길게(중첩 협의·긴 구현) 걸려도 베턴은 결국 돌아오므로 위임이 끊기지 않는다. 데드라인은
        # 교착 안전장치 — 게임처럼 한 동료가 10분+ 작업하는 경우까지 넉넉히(1시간) 둬 '활성=동료' 반려가
        # 안 뜨게 한다(이전 600초는 긴 작업 중 병렬요청이 타임아웃돼 무서운 '거부' 노이즈를 냈다).
        # 직전 위임이 detach 상태로 완주 중이면(도구 호출은 포기됐지만 위임은 계속) 새 요청을 길게
        # 재우지 않고 즉시 안내한다 — 리더가 '보류' 헛돌이 대신 턴을 마치게(시스템이 완주 후 다시 깨움).
        if (any(not t.done() for t in getattr(flow, "inflight_tasks", ()))
                and flow.comm.alive != me_id and not flow.comm.done):
            return _ok("[대기] 직전 위임이 아직 진행 중입니다 — 추가 요청을 보내지 말고 이 턴을 간결히 "
                       "마치세요. 위임이 완료되면 시스템이 그 결과와 함께 당신을 다시 깨웁니다.")
        # [fork 동시성 가드] 의견 수집(표결·회의 1R)이 도는 동안엔 새 요청을 보내지 않는다 — fork 중엔
        # 베턴(alive)이 리더에 머물러, CLI가 같은 턴에 병렬 도구 호출(vote+request)을 내면 수집 가지와
        # 같은 동료를 이중으로 깨워 '같은 봇 두 턴'(세션 충돌)이 될 수 있다(직렬 vote 시절엔 alive 이동이
        # 자연 차단). 수집은 조인이 보장돼 짧으므로 대기 안내가 정답.
        if getattr(flow, "fork_active", 0) > 0:
            return _ok("[대기] 의견 수집(표결/회의)이 진행 중입니다 — 수집 결과를 받은 뒤 요청하세요.")
        deadline = time.monotonic() + 3600
        while flow.comm.alive != me_id and not flow.comm.done and time.monotonic() < deadline:
            await anyio.sleep(0.05)
        # 같은 턴에 '같은 동료에게 같은 요청'을 다발로 보낸 병렬 중복은 합친다(idempotent): 동료를 다시
        # 깨우지 않고 직전 응답을 그대로 재사용한다 → 반사적 중복 wake 차단(직렬화는 유지, 중복만 제거).
        dupkey = (flow.leader_segment, me_id, to, str(getattr(kind, "value", kind)), body)
        if dupkey in flow.req_results:
            if flow.log:
                flow.log("dup_parallel_merged", frm=me_id, to=to,
                         kind=str(getattr(kind, "value", kind)), seg=flow.leader_segment)
            _dbg(f"{tag} ⇉병렬중복 합침(동료 재호출 없이 같은 응답 재사용)")
            return _ok(f"[{to} 응답] {_speech_clip(flow.req_results[dupkey], 4000)}\n"
                       f"(같은 턴에 이미 보낸 동일 요청 — 동료를 다시 호출하지 않고 같은 응답을 재사용)")
        # 대기 한도까지 베턴이 안 돌아옴(동료가 비정상적으로 오래 작업) — 규약위반이 아니므로 무서운 '거부'
        # 안내를 사용자에게 띄우지 않고 조용히 '보류'로 소프트 반환(리더는 응답 받은 뒤 다시 시도).
        if flow.comm.alive != me_id and not flow.comm.done:
            _dbg(f"{tag} ⏸보류:대기 한도 초과(활성={flow.comm.alive})")
            return _ok(f"[보류] {flow._info(to) or to}가 아직 작업 중이라 지금은 보내지 않았습니다 — 그 동료의 "
                       f"응답을 받은 뒤 다시 요청하세요(오류 아님).")
        # 검증→점유는 await 없이 인접 실행 → 형제 요청과 경합하지 않음(원자적).
        try:
            flow.comm.check_request(me_id, to, kind)
        except BusyInOtherFlow as e:
            # [전역 점유] 규약 위반이 아니라 '그 동료가 지금 다른 흐름에서 일하는 중' — 무서운 '거부'
            # 대신 가용 대안(같은 직군 동료·채용)을 안내한다. 같은 동료 재시도(폴링)는 금지 문구로 차단.
            if flow.log:
                flow.log("req_busy_elsewhere", frm=me_id, to=to, holder=str(e.holder_scope or ""),
                         kind=str(getattr(kind, "value", kind)), seg=flow.leader_segment)
            _dbg(f"{tag} ⏸점유:타 흐름({e.holder_scope})")
            return _ok(f"[동료 점유] {flow._info(to) or to}는 지금 다른 흐름({e.holder_scope})에서 일하는 "
                       f"중입니다 — 같은 동료에게 재시도하며 기다리지 마세요(폴링 금지). "
                       f"{_free_alternatives(flow, me_id, to)}.")
        except CommError as e:
            if flow.log:   # 관측: 거부 시점의 베턴 상태(alive)·요청자를 영속 기록 → 원인 규명
                flow.log("req_rejected", frm=me_id, to=to, kind=str(getattr(kind, "value", kind)),
                         alive=flow.comm.alive, seg=flow.leader_segment, reason=str(e)[:70])
            _dbg(f"{tag} ✗거부:규약 ({e})")
            return _ok(f"요청 거부(규약): {e}")
        # Work 위임은 Goal 확정 뒤에만 — '목표 합의(set_goal) → 분배' 순서를 구조적으로 강제(선분배 금지).
        # Info(합의용)는 언제든 허용 → Goal을 정하는 논의 자체는 막지 않는다.
        goal = (flow.current.status.goal or "").strip()
        if kind == Kind.WORK and not goal:
            _dbg(f"{tag} ✗거부:Goal미확정")
            return _ok("Work 위임 거부: 이 Task의 Goal이 아직 확정되지 않았습니다. 먼저 동료와 request(Info)로 "
                       "목표를 합의하고 set_goal로 확정한 뒤 Work로 맡기세요(목표는 팀 합의의 산물 — 선분배 금지).")
        me_is_leader = (me_id == flow.leader)
        # [비-리더 교차도메인 Work 게이트 — 구조적 조율 단일화(2026-06-22, 사용자: '주어진 일과 무관한 일을
        #  다른 도메인에 시키는 이상한 협업'은 구조 문제다)] 비-리더는 *받은 일*을 한다 — 같은 도메인 동료에게
        # 분담(서브태스킹)하거나 검증자(QA)에게 검증을 맡기는 건 자유고, 막히거나 궁금한 건 request(Info)로
        # 어느 도메인 전문가에게든 *자문*(자유·권장)한다. 그러나 *다른 도메인의 새 Work*를 직접 여는 것은
        # 리더의 조율 역할이다(SINGLE FLOW·중앙 조율). 프롬프트로 '하지 마'가 아니라 구조로 막고 리더로 보낸다.
        # 검증·자문을 막는 게 아니라 '의미없는 교차도메인 Work 위임'만 막는다(사용자 설계 방향).
        if (kind == Kind.WORK and goal and not me_is_leader and to != flow.leader
                and not getattr(flow, "crossdomain_checked", False)):
            my_jobs = {_norm_job(j) for j in _jobs_of(flow._info(me_id) or "")} - {""}
            to_jobs = {_norm_job(j) for j in _jobs_of(flow._info(to) or "")} - {""}
            same_domain = bool(my_jobs & to_jobs)
            to_verifier = _is_verifier(flow._info(to) or "")
            cap_hit = _offdomain_capability_hit(flow, to, body)   # 같은 도메인이라도 내 도메인 밖 능력 요구면 hit
            if (not same_domain or cap_hit) and not to_verifier:
                if flow.log:
                    flow.log("work_crossdomain_blocked", frm=me_id, to=to, my=sorted(my_jobs),
                             to_jobs=sorted(to_jobs), caps=list(cap_hit.keys()), seg=flow.leader_segment)
                _dbg(f"{tag} ✗보류→리더조율큐:비리더 교차도메인")
                # [리더 조율 강제(2026-06-23, 사용자)] 막힌 교차도메인 Work를 그냥 거부하지 않고 '리더 조율
                # 큐'에 적재한다 — 워커가 이를 '핑계'로 보고하고 리더가 묵살·재발사하던 라이브 루프(P-030
                # backend2↔PM 핑퐁)를 끊기 위함. sys_core continue 루프가 이 큐를 리더 다음 턴에 'SYS 확인
                # 사실'로 주입해 리더가 *직접* 그 도메인 전문가에게 위임하게 한다. 같은 (요청자→대상)은 중복 적재 X.
                try:
                    if not any(c.get("requester") == me_id and c.get("to") == to
                               for c in flow.pending_coordination):
                        flow.pending_coordination.append({
                            "requester": me_id, "req_role": flow._info(me_id) or str(me_id),
                            "to": to, "to_role": flow._info(to) or str(to),
                            "to_jobs": sorted(to_jobs), "body": (body or "")[:500]})
                except Exception:
                    pass
                return _ok(
                    f"위임 보류(교차도메인 — **리더 조율 큐로 이관됨**): 당신({flow._info(me_id)})은 다른 도메인의 "
                    f"새 작업을 직접 맡길 수 없어, 이 요청을 **리더에게 조율 사안으로 올렸습니다** — 리더가 그 도메인 "
                    f"전문가에게 직접 배정합니다. 지금 이 턴은 **당신 도메인의 일을 계속**하세요(막힌 그 부분은 리더가 "
                    f"처리하니 기다리거나 다른 동료에게 떠넘기지 마세요). 질문·QA 검증은 그대로 자유입니다.")
        # [직군밖 사전 차단 — 리더 라우팅] 능력표로 *위임 전에* 능력 미스매치를 잡아 그 전문가에게 리다이렉트
        # (흡수의 씨앗 차단). 리더는 조율 권한이 있어 직접 적임자에게 보낸다(비-리더는 위 교차도메인 게이트가
        # 이미 리더로 돌렸다). 상세·근거는 _offdomain_capability_hit 참고. offdomain_checked는 테스트 우회 플래그.
        if kind == Kind.WORK and goal and me_is_leader and not getattr(flow, "offdomain_checked", False):
            _hit = _offdomain_capability_hit(flow, to, body)
            if _hit:
                if flow.log:
                    flow.log("work_offdomain_blocked", to=to, caps=list(_hit.keys()), seg=flow.leader_segment)
                _who = "; ".join(f"{n} → {flow._names(ms)}" for n, ms in _hit.items())
                return _ok(
                    f"위임 거부(직군밖 — 능력 미스매치): 이 작업은 **{', '.join(_hit)}** 능력이 필요한데 "
                    f"{flow._info(to) or to}의 직군 밖입니다. 그 능력을 가진 전문가가 팀에 있습니다 — {_who}. "
                    f"**그 전문가에게 위임**하세요(범용·비전문이 떠안으면 흡수 — placeholder 품질). 정말 {to}가 "
                    f"맡아야 할 합당한 이유가 있으면 body에 '[직군초과: <사유>]'를 적어 다시 보내세요.")
        # Work Response → Accept/Redo (docs Communication.md §5). 이미 이 owner가 '완료 응답'까지 낸
        # 산출물을 같은 위임자가 또 Work로 보내면, 그건 '새 위임'이 아니라 직전 산출물의 Redo다.
        # → 새 프레임이 아니라 redo()로 처리한다(한계까지만, 초과 시 반복 위임 거부). 이로써 '되풀이
        #   위임'이 구조적으로 '직전 결함을 고치는 보완'으로만 성립한다(반사적 중복요청 차단·정당한 보완 허용).
        is_redo = kind == Kind.WORK and flow.comm.delivered_work(me_id, to)
        owner_body = body
        if is_redo:
            try:
                frame = flow.comm.redo(me_id, to, "pending", body=body)    # 베턴 점유 + Redo 카운트(한계 시 RedoLimitExceeded)
            except RedoLimitExceeded:
                _dbg(f"{tag} ✗재위임 한도초과")
                # [품질>토큰 — 리더 셀프 마무리 권유 제거] 종전 안내("직접 Write/Edit로 마무리")는
                # Redo 실패의 끝에서 중앙집권·비전문 마감을 권하는 셈이었다(탈중앙·전문화 역행).
                return _ok(f"재위임 거부(Redo 한도 초과): {to}({flow._info(to)})는 이미 이 산출물을 여러 번 "
                           f"보완했습니다. 같은 사람에게 같은 식으로 또 떠넘기지 마세요 — 품질 경로는: "
                           f"① 검증자(타 멤버)의 결함 보고로 **무엇이 왜 미달인지 정밀화**해 마지막 1회를 명확히 맡기거나 "
                           f"② 같은 직군의 **다른 전문가**(없으면 recruit)에게 결함 보고와 함께 맡기거나 "
                           f"③ goal이 이미 충족이면 complete_task, 끝내 미달이면 사용자에게 정직하게 보고하세요"
                           f"(리더가 비전문 직접 마무리로 덮지 말 것).")
            owner_body = (f"[보완 요청(Redo) — 직전 산출물이 목표에 못 미쳐 되돌아왔습니다] 고칠 구체적 결함: {body}\n"
                          f"[이 Task의 Goal] {goal}\n결함만 정확히 고치고 run으로 재검증해 그 증거와 함께 보고하세요.")
        else:
            frame = flow.comm.request(me_id, to, "pending", kind, body=body)   # 베턴 점유(alive→to) + 원문(정밀복구)
            if kind == Kind.WORK:
                # 위임의 '계약'은 리더가 매번 새로 쓰는 스펙이 아니라 팀 합의로 확정된 Goal이다(스펙 리파인
                # 루프=재요청의 뿌리를 끊는다). owner가 그 목표를 끝까지(구현+검증) 책임진다.
                owner_body = (f"[위임 — 이 목표를 끝까지 책임지는 owner는 당신입니다] 이 Task의 Goal: {goal}\n"
                              f"직접 구현하고 run으로 '목표가 충족됨'을 검증한 뒤(리더에게 되넘기지 말 것), "
                              f"그 실행 증거와 함께 간결히 보고하세요.\n"
                              f"큰 목표는 **수직 슬라이스 우선**: '끝까지 관통하는 최소 동작 버전'을 먼저 만들어 "
                              f"검증하고 그 위에 살을 붙이세요 — 마지막 통합 몰빵 금지(오차를 일찍 드러내는 것이 "
                              f"빠른 길입니다. RFC-005: 검증 신호는 연속적이어야 한다).\n"
                              f"보고는 다음 골격으로(보고 계약 — 받은 쪽이 산출물을 재탐색하지 않아도 되게): "
                              f"[결과] 한 줄 결론(완료/부분/실패) / [변경] 파일·핵심 변경 목록 / "
                              f"[검증] 방법→결과 / [리스크] 남은 것·주의점.\n"
                              f"단, 이 Goal에 **당신 직군의 전문성으로 만드는 게 아닌 범주**가 섞여 있으면 — "
                              f"코드로 흉내낼 수 있다고 당신 일인 게 아닙니다('할 수 있다'와 '그 분야 전문성으로 "
                              f"잘한다'는 다릅니다 — 비전문 자급은 placeholder일 뿐) — 어설프게 떠안지 말고 보고 "
                              f"**첫 줄**에 `[직군밖] 필요직군명` 을 적어 반려하세요. 리더가 그 직군을 채용하거나 "
                              f"실제 제작 자원으로 충족합니다(전문화 원칙: '구현 가능'이 아니라 '전문성 정합'으로 판단).\n"
                              f"[요청 맥락] {body}")
                iface = (getattr(flow.current, "interfaces", "") or "").strip()
                if iface:
                    # [협업 — 인터페이스 직접 전달·합의(2026-06-22 사용자: '전문가끼리 서로 대화하는가')] 종전엔
                    # interfaces가 Task에만 저장되고 owner에게 전달 안 돼(여기 누락) owner가 계약을 못 보고 추측
                    # → 통합 불일치(P-028 API 미스매치). 이제 계약을 owner에게 주고, 맞물리는 부분은 그 도메인
                    # owner에게 *직접 request(Info)*로 확인하게 한다(리더 중계·추측 금지).
                    owner_body += (f"\n[도메인 간 인터페이스 계약 — 준수]\n{_speech_clip(iface, 1500)}"
                                   f"\n[직접 합의 — 리더 중계 금지] 당신 작업이 다른 도메인과 맞물리면(데이터 포맷·"
                                   f"API·이벤트 타이밍 등) 추측하거나 리더에게 되묻지 말고 **그 도메인 owner에게 "
                                   f"직접 request(Info)**로 계약을 확인·합의하세요 — 전문가끼리 직접 소통합니다.")
                notes = getattr(flow.current, "collab_notes", "")
                if notes:
                    # [스펙 증발 방지] 회의·표결의 합의는 리더 머릿속이 아니라 위임 계약에 실린다 —
                    # 라이브 P-009: 9직군이 회의로 정한 스펙(상태머신·SLA·타이밍 계약)이 구현자에게
                    # 전달되지 않아(스코프 단절·리더 요약 의존) 결과물 품질로 이어지지 못함.
                    owner_body += f"\n[팀 협의 기록(회의·표결) — 구현·검증 시 이 합의를 준수]\n{_speech_clip(notes, 6000)}"   # 저장 한도(6000)와 일치 — 전달에서 합의가 또 잘리지 않게(품질>토큰)
                # [RFC-008 P0 — 검증 위임에 루브릭 자동 주입] owner 인도 후 '다른 멤버'에게 가는 Work =
                # 검증 위임 → owner 산출물 도메인의 직무 기준을 루브릭으로 동봉. 라이브 P-010 1차에서 루브릭이
                # complete_task 거부 메시지에만 있어 0회 발동(검증이 카운트되면 게이트를 안 탐) — 검증자에게
                # 직접 주입해야 'owner 도메인 기준 채점'이 실제로 일어난다. '돌아가는가'가 아니라 '충분한가'.
                if (getattr(flow.current, "owner_delivered", False) and flow.current.owner
                        and to != flow.current.owner and callable(getattr(flow, "craft_of", None))):
                    owner_job = (flow._info(flow.current.owner) or "").strip()
                    rub = [flow.craft_of(j) for j in owner_job.split("·") if j.strip()]
                    rub = [r for r in rub if r]
                    if rub:
                        # [발견2 완화] owner 인도 후 타 멤버 Work가 '검증'인지 '후속 구현'인지 구조로 완벽히
                        # 구분 불가(의도의 문제) — 메시지가 양쪽을 다 커버해 오발동을 무해화한다: 검증 위임이면
                        # 채점, 후속 구현이면 같은 기준을 '참고'(통합 시 품질 인식). 어느 쪽이든 owner 도메인
                        # 기준이 주입되는 건 손해가 아니다('충분한가'의 눈을 공유).
                        owner_body += (f"\n[산출물 품질 기준 — '{owner_job}' 도메인. 이 요청이 **검증**이면 산출물을 "
                                       f"'사용자처럼 실제로 사용·플레이'하며 아래 각 항목을 충족/미달로 채점하고 미달은 "
                                       f"구체적 결함으로 보고하세요(돌아가는가 아니라 '충분한가'). 이 요청이 **후속 "
                                       f"구현/통합**이면 아래 기준을 참고해 같은 품질 수준을 맞추세요:\n"
                                       + _speech_clip("\n---\n".join(rub), 2500))
        thread_id = flow.current.thread_id
        # Owner = 그 일을 Work로 받은 동료(수신=소유). 선배정이 아니라 요청으로 owner가 떠오른다 —
        # 이 Task에 아직 owner가 없을 때 첫 Work-request 수신자가 책임자가 된다(중앙집권 방지).
        if kind == Kind.WORK and not flow.current.owner:
            flow.current.owner = to
            flow.current.status.owner = flow._info(to) or f"<@{to}>"
            await flow.refresh(flow.current)
            _ckpt(flow)                       # 크래시-세이프: owner 확정 영속(복구 때 같은 담당이 잇게)
        req = await g.send_request(thread_id, me_id, to, kind, body)
        frame.request_id = str(req)                              # 실제 메시지 id로 기록 갱신
        if kind == Kind.WORK and flow.current:
            flow.current.work_delegated_to.add(to)   # 누가 위임했든(리더든 peer든) 'Work를 실제로 받은' 멤버 기록
            if to == flow.current.owner:
                # [정밀 복구] owner에게 보낸 Work 원문 보관(레벨1 fallback).
                flow.current.last_work_body = body
            if me_id == flow.leader:
                flow.current.work_delegated += 1   # 리더의 구현 위임 카운트 — 0이면 '자문만 받고 독식'(권한 훅이 차단)
            # [정밀 복구 — 체인 깊이 영속] 모든 Work 위임마다 체크포인트 → 스냅샷의 active_chain이 *현재 깊이*를
            # 반영. 끊김 시 가장 깊은 활성 워커(체인 끝)를 그 원문으로 재개(리더로 안 튐). 깊은 전문가 협업 보존.
            _ckpt(flow)
        _dbg(f"{tag} ✓전송 req={req}{' (Redo)' if is_redo else ''}")
        if flow.log:   # 관측: 모든 요청을 '보낸 순서'대로 영속 기록(중첩 PostToolUse 타이밍에 안 묻힘)
            flow.log("req_sent", frm=me_id, to=to, kind=str(getattr(kind, "value", kind)),
                     seg=flow.leader_segment, redo=is_redo, body=body[:60])
        # Task 정의 '실질 협의' 참여 기록 — 보낸 쪽·받은 쪽 모두(누가 물었든: 리더든 peer든). 빈 핑은 제외.
        # → set_goal 게이트가 'peer 협의도 합의로 인정'하고 '빈 핑은 불인정'하게 만든다(허브 완화·실질 강제).
        if kind == Kind.INFO and flow.current and _is_substantive(body):
            for x in (me_id, to):
                if x in flow.current.team and x != flow.leader:
                    flow.current.participated.add(x)
            # [협업 — 전문가 간 직접 대화(2026-06-22 사용자 설계)] 양쪽 다 비-리더 팀원이면 owner↔owner 직접
            # Info(리더 경유 아님) — 쌍으로 기록. 인터페이스 계약을 '리더 중계·추측'이 아니라 *당사자끼리*
            # 합의했는지 마감 게이트(iface_dialogue)가 본다.
            if (me_id != flow.leader and to != flow.leader
                    and me_id in flow.current.team and to in flow.current.team):
                flow.current.peer_info_pairs.add(frozenset((me_id, to)))
        # ── 위임 완주 보장(detach-safe) ─────────────────────────────────────────
        # 여기서부터의 '깨우기→응답 처리→프레임 close'는 별도 태스크(_deliver)로 돌고, 도구 호출
        # 자체는 shield로 감싼다. CLI가 (자체 한도 등으로) 이 도구 호출을 포기·취소해도 위임은
        # 끝까지 완주하고 규약(베턴·게이트·기록)이 일관되게 닫힌다 — 라이브 관측: 위임 포기가
        # '이중 활성'(리더+사슬 동시 작업)과 리더의 '비동기 작업 중' 오인을 만들던 결함의 차단.
        # 완주 결과는 flow.detached_results로 남아 SYS가 이어가기 리더에게 전달한다.
        detached = {"on": False}

        async def _deliver():
            runs_before = flow.current.run_count if flow.current else 0
            acts_before = flow.act_count   # 위임 도중 owner(단일흐름이라 깨운 동료만 활성)가 실제로 일했는지 측정
            mine_before = flow.act_by.get(me_id, 0) if getattr(flow, "act_by", None) is not None else 0
            _body_local = owner_body
            result = ""
            _nest_guard = 0
            while True:
                try:
                    result = await flow.wake(to, _body_local, kind)     # 동료 깨워 응답(중첩 베턴)
                    if _looks_transient(result):                        # 일시 오류면 한 번 더(답으로 취급 X)
                        result = await flow.wake(to, _body_local, kind)
                except Exception as e:
                    result = f"(동료 처리 중 오류: {e})"
                # [중첩 위임 — 동기처럼 완주(논블로킹 핸드오프)] `to`가 자기 턴에서 다른 동료에게 핸드오프했으면
                # SYS가 그 하위 위임을 호출 *밖*에서 완주시키고 `to`를 그 결과로 이어간다 — 블록킹 도구호출 없이
                # 중첩이 직렬로 완주(75초 미닿음). 판정은 `to`의 출력 문자열('[위임됨' 등 — 봇 표현은 못 믿음)이
                # 아니라 **handoff_inflight[to]에 실제로 하위 위임이 등록됐다는 사실**로 한다(견고). _nest_guard는
                # 폭주 백스톱(같은 `to`가 끝없이 재위임만 하는 병적 경우) — 정상 사슬은 한참 못 미친다.
                _sub = (getattr(flow, "handoff_inflight", None) or {}).pop(to, None)
                _nest_guard += 1
                if _sub is None or _nest_guard > 50:
                    if _sub is not None and flow.log:
                        flow.log("handoff_nest_guard", to=to, depth=_nest_guard)
                    break
                try:
                    _sr = await _sub
                    _srt = _sr["content"][0]["text"] if isinstance(_sr, dict) else str(_sr)
                except Exception as e:
                    _srt = f"(하위 위임 오류: {e})"
                _body_local = ("[당신이 맡긴 위임의 결과가 도착했습니다 — 이어서 통합·검증·완성하세요(추가 위임이 "
                               f"더 필요하면 한 번에 하나씩, 끝나면 보고로 응답):\n{_speech_clip(_srt, 4000)}")
            # 깨운 동료가 '나(위임자)에게 확인요청'을 남기고 턴을 마쳤으면, 그 질문을 응답으로 표면화 →
            # 내가 답을 정해 다시 맡긴다(되묻기가 에러가 아니라 협업으로 흐름). 이는 '완료'가 아니므로
            # delivered로 기록하지 않는다(되묻기 후 재위임은 Redo가 아니라 '첫 구현').
            was_clarify = False
            if (flow.pending_clarify and flow.pending_clarify.get("to") == me_id
                    and flow.pending_clarify.get("from") == to):
                q = flow.pending_clarify["q"]
                flow.pending_clarify = None
                was_clarify = True
                result = (f"[확인요청 from {flow._info(to)}] {q}\n"
                          f"(→ 답을 정한 뒤, 이 작업을 {flow._info(to)}에게 request(Work)로 다시 맡기세요)")
            failed = _looks_transient(result)
            # [직군밖 반려 — 전문화의 구조 채널] 도메인 적합성은 시스템이 키워드로 판정하지 않는다 —
            # 그 분야 전문가(수신 owner)가 판정한다(자기정의 원칙). owner가 첫 줄에 '[직군밖] 필요직군'
            # 을 적으면: 실패도 미완도 아닌 '올바른 반려'로 분류하고, 소유를 해제하며, 리더에게 채용을
            # 구조적으로 지시한다 — 관계없는 직군이 일을 흡수해 어설픈 산출물을 내던 경로(라이브:
            # ML이 백엔드에 묶여 감)의 차단.
            refused_m = re.match(r"^\s*\[직군밖\]\s*([^\n]*)", result or "")
            refused = bool(kind == Kind.WORK and not was_clarify and not failed and refused_m)
            if refused and flow.current is not None and flow.current.owner == to:
                flow.current.owner = 0                 # 소유 해제 — 채용된 전문가가 새 owner가 되게
                flow.current.status.owner = ""
                flow.current.owner_incomplete = False
                _ckpt(flow)
            # owner가 '위임 도중 실제로 일했나' — 단일흐름이라 깨운 동료(+그 하위)만 활성이므로 wake 전후
            # act_count(run/Write/Edit) 증가 = owner 작업. 거짓이면 owner는 깨어났지만 착수 전/계획만 하고
            # 곧장 반환한 것(허위완료의 씨앗). 이걸로 '검증된 인도'와 '빈 응답'을 가른다.
            # '요청자 자신'의 활동(detach 뒤 리더가 모델 쪽에서 돌린 폴링 run 등)은 빼고 잰다 —
            # 위임 측정창의 인도 신호(owner_acted)가 이중 활성 잔재로 오염되지 않게(허위완료 차단 정확성).
            mine_delta = (flow.act_by.get(me_id, 0) - mine_before) if getattr(flow, "act_by", None) is not None else 0
            owner_acted = (flow.act_count - acts_before) > mine_delta
            # 진짜 행(무활동)으로 끊긴 인프라 타임아웃인데 owner가 그 전에 실제로 작업을 했다면, 한 작업은
            # 작업공간에 남아 있다 → '실패'로 끝내 유실시키지 말고 '이어가기'(미완)로 처리한다. (하트비트
            # 타임아웃이 일하는 워커는 안 자르므로 드문 경우지만, 안전망으로 작업 유실·허위완료를 막는다.)
            infra_timeout = (kind == Kind.WORK and not was_clarify
                             and "api error: timeout" in (result or "").lower())
            resumable_timeout = infra_timeout and owner_acted
            # 동료가 'turn 한도'로 미완 반환했나(Work) — 그러면 이 Task는 완료로 못 닫고(complete_task 거부),
            # 같은 owner에게 '이어서(continuation)' 재위임해 끝내야 한다(허위완료→다음 Task churn 차단). 미완은
            # delivered(accept)로 안 쳐서 respond 마커를 'incomplete'로 두면, 재위임이 Redo 한도에 안 걸린다
            # (이어가기는 '직전 결함 보완'이 아니라 '남은 작업 마저 하기'이므로 횟수 제한 없이 계속 가능).
            incomplete = (kind == Kind.WORK and not was_clarify and not failed and not refused
                          and "턴 한도 도달" in (result or "")) or resumable_timeout
            # 미완 게이트(owner_incomplete)는 '의미 있는 신호'로만 갱신한다: 미완 신호면 True, owner가
            # '실작업을 담은 정상 응답'으로 마무리하면 False(이어가기 완료 = 게이트 자동 해제). 크래시(failed)
            # ·실작업 없는 응답은 완료의 증거가 아니므로 직전 상태를 유지한다 — 타임아웃 미완이 후속 크래시/
            # 빈 응답으로 풀려 미완인 채 complete가 통과되는 구멍 차단.
            if kind == Kind.WORK and not was_clarify and flow.current:
                if incomplete:
                    flow.current.owner_incomplete = True
                elif not failed and owner_acted:
                    flow.current.owner_incomplete = False
            is_owner_work = (kind == Kind.WORK and not was_clarify and not failed and not incomplete
                             and not refused
                             and flow.current is not None and to == flow.current.owner)
            # owner가 Work를 받고도 실작업(run/Write) 0회로 곧장 반환 = 착수 전/계획만 = '인도 아님'.
            premature = is_owner_work and not owner_acted
            if premature and flow.current is not None:
                # 미착수도 '구조적 미완'이다 — 마커를 세워 complete를 막고, 리더 세그먼트가 여기서
                # 끝나도 SYS 자동 이어가기가 같은 owner를 다시 깨운다(판단이 아니라 기계적 행동).
                flow.current.owner_incomplete = True
            if is_owner_work and owner_acted and _is_substantive(result):
                flow.current.owner_delivered = True   # 이 owner가 실작업+응답을 냈다 → complete_task 허용 근거
            try:
                await g.send_response(thread_id, to, req, result)
                await _react(g, thread_id, req, "⚠️" if failed else "✅")  # 상태=이모지(해소/실패)
                _dbg(f"{tag} {'⚠실패' if failed else ('…미완' if (incomplete or premature) else '✓응답')} len={len(result)}")
            finally:
                # 프레임 close = 베턴 복귀(누수 방지). 정상이면 alive==to 라 그대로 닫힌다. 미완·미착수(premature)는
                # 'accept'로 안 쳐서 delivered로 기록 안 함 → 같은 owner 재위임이 Redo 한도에 안 걸리고 '실제 첫 인도'로 성립.
                # 크래시(failed)도 'accept'가 아니다 — 인프라 실패가 '완료 인도'로 기록되면 직후 재요청이
                # Redo(보완)로 둔갑해 한도를 태우고 owner에게 '직전 산출물 결함' 프레임으로 잘못 전달된다.
                try:
                    flow.comm.respond(to, "clarify" if was_clarify else
                                      ("refused" if refused else
                                       "incomplete" if (incomplete or premature) else
                                       "failed" if failed else "accept"), result)
                except CommError:
                    # to의 중첩 하위요청이 응답 없이 끝나(크래시/이탈) 베턴이 to에 '굳은' 비정상 상황 →
                    # me_id(요청자)가 다시 alive 될 때까지 위 프레임을 강제 close. 흐름 교착(굳음) 방지.
                    _stuck = flow.comm.alive
                    if flow.log:
                        flow.log("baton_recover", me=me_id, stuck_alive=_stuck, to=to)
                    # [막힘 흡수 차단 — 막힌 사람 기록] 베턴이 막힌 하위 담당에서 위임자에게 되돌아온다. 위임자가
                    # '내가 하지'로 그 사람 일을 흡수하지 못하게 막힌 사람을 기록 — 게이트가 '같은 사람 재요청'을
                    # 유도(재채용 X). 막힌 사람이 다시 일하면 해제. (origin/리더 자신이 막힌 건 흡수 대상 아님.)
                    # *새* victim일 때만 기준치·카운터 초기화 — 같은 사람이 반복해 막히면 카운터가 누적돼 N회 후
                    # 게이트가 폴백(통과)하므로, 진짜 죽은 동료에 무한 재요청·무한 차단으로 빌드가 얼지 않는다.
                    if (_stuck and _stuck != flow.comm.origin and _stuck != getattr(flow, "leader", None)
                            and getattr(flow, "_stall_victim", None) != _stuck):
                        flow._stall_victim = _stuck
                        flow._stall_victim_acts = (getattr(flow, "act_by", None) or {}).get(_stuck, 0)
                        flow._stall_blocks = 0
                    guard = 0
                    # origin 프레임(스택 마지막 1장)은 여기서 닫지 않는다 — 핸들러 레벨 복구가
                    # 흐름 자체를 종료시키면 안 됨(origin 마감은 SYS의 _close_flow 책임). detach로
                    # 프레임 순서가 어긋난 최악 타이밍에 흐름이 통째로 드레인되던 위험 차단.
                    while (not flow.comm.done and flow.comm.alive != me_id
                           and len(flow.comm.open_requests) > 1 and guard < 30):
                        flow.comm.escalate("베턴 굳음 안전복구")
                        guard += 1
            if failed:
                if resumable_timeout:
                    # owner가 작업을 진행하다 '무활동'으로 끊긴 경우 — 한 작업은 작업공간에 보존돼 있다.
                    # 실패로 끝내지 말고 같은 owner에게 '이어서' 재위임(연속). owner_incomplete=True라 complete는
                    # 막히고, 프레임 마커가 incomplete라 redo 한도와 무관하게 계속 이어갈 수 있다(유실·허위완료 동시 차단).
                    if flow.log:
                        flow.log("owner_resumable_timeout", to=to, seg=getattr(flow, "leader_segment", 0))
                    return _ok(f"[{flow._info(to)}] 작업을 진행하던 중 일시 무응답으로 끊겼습니다 — 한 작업은 "
                               f"작업공간에 보존돼 있습니다. **같은 담당자에게 request(Work)로 '이어서 남은 부분을 "
                               f"마저 끝내라'**고 다시 맡기세요(이어가기 — 횟수 제한 없음). 다른 사람으로 바꾸거나 "
                               f"새로 뽑지 마세요(같은 환경이라 같은 문제).")
                # 구조적 사실: 단일흐름은 한 번에 한 명만 일한다 → 요청자는 그 동료가 끝날 때까지 '블록'된다.
                # 따라서 여기서의 '실패'는 그 동료가 느리거나 불응한 게 아니라 그 동료의 LLM 서브프로세스가
                # '크래시'(SIGTERM/143·연결끊김·과부하)한 것 — 즉 인프라/환경 문제다. 새 사람으로 바꾸거나
                # 충원하면 '같은 환경'에서 똑같이 크래시한다(이게 '백엔드 6명' 루프의 뿌리). 그래서 실패엔
                # '재배정·채용'을 절대 권하지 않는다 — 같은 동료 1회 재시도(블립 회복용) 또는 사용자 보고만.
                flow.consec_fail = getattr(flow, "consec_fail", 0) + 1
                if flow.log:
                    flow.log("req_failed", to=to, consec=flow.consec_fail, seg=flow.leader_segment)
                if flow.consec_fail >= 2:
                    return _ok(f"[{to}] 또 실패 — **연속 {flow.consec_fail}회**. 이건 그 동료가 아니라 **환경(인프라) 일시 "
                               f"불안정**입니다(단일흐름이라 한 명만 도는데 그 서브프로세스가 크래시한 것). **새로 뽑거나 "
                               f"다른 사람으로 바꾸지 마세요 — 같은 환경이라 똑같이 실패합니다.** 진행 상황을 사용자에게 "
                               f"'환경 불안정으로 일시 중단'이라 보고하고 멈추세요(무한 재시도·충원 금지).")
                return _ok(f"[{to}] 응답 실패. 단일흐름에선 한 명만 일하므로 이건 그 동료 탓이 아니라 거의 항상 **인프라/일시 "
                           f"오류(서브프로세스 크래시)**입니다 — **다른 사람으로 바꾸거나 새로 뽑지 마세요(같은 환경이라 똑같이 "
                           f"실패).** 같은 동료에게 한 번만 다시 요청해보고(블립이면 회복), 또 실패하면 사용자에게 보고하고 멈추세요.")
            flow.consec_fail = 0   # 정상 응답 → 연속 실패 카운터 리셋(일시 블립 회복)
            if refused:
                need = (refused_m.group(1) or "").strip() or "해당 전문 직군"
                if flow.log:
                    flow.log("work_refused_offdomain", to=to, need=need[:30], seg=flow.leader_segment)
                return _ok(f"[직군밖 반려] {flow._info(to) or to}가 이 일을 **자기 직군 밖**으로 판정했습니다 — "
                           f"필요 직군: {need}.\n**recruit(role='{need}')로 예비를 채용해 그 전문가에게 Work로 "
                           f"맡기세요** — 같은 동료나 관계없는 직군에 다시 떠넘기지 마세요(이 반려는 실패가 아니라 "
                           f"올바른 전문화 신호입니다. 소유는 해제됐고, 채용된 전문가가 새 owner가 됩니다).\n"
                           f"--- 반려 보고 원문 ---\n{_speech_clip(result, 1500)}")
            # owner가 깨어났지만 '실작업 없이'(run/Write/Edit 0회) 곧장 반환 = 아직 착수 전/계획만. 리더가 대신
            # 구현·완료하지 말 것(독점·허위완료의 정확한 진입점). 같은 owner에게 다시 맡겨 '검증된 산출물'을 받게
            # 안내한다. 이 응답은 캐시하지 않는다 → 같은 턴에 재위임해도 합쳐지지 않고 실제로 다시 깨운다.
            if premature:
                _dbg(f"{tag} ⚠owner 미착수(실작업 0)")
                if flow.log:
                    flow.log("owner_no_work", to=to, seg=flow.leader_segment)
                return _ok(f"[{to} 응답] {_speech_clip(result, 1500)}\n\n[중요] {flow._info(to) or to}가 아직 산출물을 만들지 "
                           f"않았습니다(run/파일작성 0회 — 착수 전이거나 계획만). **당신이 대신 구현하거나 이 Task를 "
                           f"완료하지 마세요(독점·허위완료 금지).** 같은 owner에게 request(Work)로 다시 맡겨 'run으로 "
                           f"검증한 실제 산출물'을 받은 뒤 진행하세요. 정말 끝까지 무응답이면 recruit/재배정으로.")
            # 위임 응답엔 owner가 '직접 돌린 실행 증거(시스템 캡처)'를 붙여 돌려준다 — 위임자가 말이 아니라
            # 증거로 '검증 후 수락'할 수 있게(반사적 재요청 대신). owner가 이번에 run을 돌렸을 때만.
            receipt = ""
            if (kind == Kind.WORK and not was_clarify and flow.current
                    and flow.current.run_count > runs_before and flow.current.evidence):
                receipt = f"\n[owner 실행 증거(시스템 캡처)] {_speech_clip(flow.current.evidence, 1000)}"
            # [발견1 교정 2026-06-13] 검증 대상 산출물이 '존재'하면(owner 위임 인도 OR 리더가 직접
            # 구현=leader_writes>0) 그 후 타 멤버 응답을 교차 검증 참여로 센다 — 리더 독식 Task(owner==0)도
            # 제3자 검증 대상('누가 만들었든 제3자 검증'은 보편 이치). 종전엔 owner_delivered만 봐서 리더
            # 독식이 검증 면제되던 구멍.
            product_ready = (flow.current.owner_delivered
                             or (not flow.current.owner and getattr(flow.current, "leader_writes", 0) > 0))
            if flow.current and product_ready and to != flow.current.owner:
                flow.current.cross_checks += 1
                # [독립 검증 = 다른 도메인 — 동질 모델] 같은 Claude·같은 직군 검증자는 에코(같은 관점→같은
                # 맹점). owner와 도메인이 다른 검증자만 '독립'으로 따로 센다(owner 미상이면 리더 기준).
                _own = flow.current.owner or flow.leader
                _od = {_norm_job(j) for j in _jobs_of(flow._info(_own) or "")} - {""}
                _vd = {_norm_job(j) for j in _jobs_of(flow._info(to) or "")} - {""}
                if _od and _vd and not (_od & _vd):
                    flow.current.cross_check_offdomain += 1
            flow.req_results[dupkey] = result   # 같은 턴 병렬 중복요청이 재사용할 응답 캐시(동료 재호출 방지)
            return _ok(f"[{to} 응답] {_speech_clip(result, 4000)}{receipt}")


        async def _deliver_tracked():
            payload = await _deliver()
            if detached["on"]:
                try:
                    txt = payload["content"][0]["text"]
                except Exception:
                    txt = str(payload)[:400]
                flow.detached_results.append(f"{flow._info(to) or to} → {_speech_clip(txt, 4000)}")
            return payload

        inner = asyncio.ensure_future(_deliver_tracked())
        flow.inflight_tasks.add(inner)
        inner.add_done_callback(flow.inflight_tasks.discard)
        if getattr(flow, "_handoff", False):
            # [논블로킹 핸드오프 — 단일흐름 안정성(2026-06-22 사용자 설계)] 동료의 *턴 전체*를 도구호출 안에서
            # 기다리지 않는다. 기다리면 75초 넘을 때 CLI가 도구호출을 포기→CancelledError→detach→백그라운드
            # 비동기 churn(P-029: 6위임 전부 detach·'처리 중 턴종료' 누수·빈 산출물). 대신 위임을 인플라이트로
            # 등록하고 *즉시* 반환 — 동료 작업은 SYS 이어가기 루프(_drain_inflight)와 _deliver 중첩 루프가 호출
            # *밖*에서 완주시켜 결과로 요청자를 잇는다. 베턴은 이미 to로 넘어가 요청자는 비활성 → 재위임 불가
            # (규약이 막음). 도구호출이 1초라 75초가 닿지 않고, 베턴 1개라 비동기 다중실행이 구조적으로 불가 = 단일흐름.
            detached["on"] = True
            flow.handoff_inflight[me_id] = inner
            return _ok("[위임됨 — SYS가 동료를 끝까지 완주시켜 *결과로 당신을 이어줍니다*(비동기 아님 · 한 번에 "
                       "한 위임). **'처리 중' 같은 말이나 재위임·추가 행동 없이 이 턴을 여기서 마치세요** — "
                       "결과가 도착하면 SYS가 자동으로 당신을 재개합니다.]")
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            if not inner.done():
                detached["on"] = True       # 도구 호출만 죽고 위임은 계속 — 결과는 detached로 전달
                if flow.log:
                    flow.log("delegation_detached", to=to, seg=flow.leader_segment)
            raise

    tools.append(request)

    @tool("recruit",
          "인원이 부족하거나 '새 직군'이 필요하면 채용한다. role=맡길 직군(예: 게임 기획자, UX 디자이너, "
          "사운드 — '예비' 인력을 이 직군으로 신규 채용). member=특정 동료 id/역할명(비우고 role만 주면 예비에서 "
          "자동 선발). reason=사유. 로스터에 없는 직군도 이렇게 런타임에 채용해 쓴다. **1봇 1직업 원칙** — "
          "이미 직군이 있는 봇에 새 직군이 필요하면 예비를 그 직군으로 뽑는 게 정도다. 겸직(기존 직군 유지+추가, "
          "봇당 최대 2개)은 예외 — 예비가 없거나 기존 직군과 비슷한 일일 때만 허용된다. "
          "**직군명은 기존 것 재사용 우선** — 같은 도메인 직군이 이미 있으면 그 이름 그대로 쓰고(변형 금지), "
          "정말 다른 일을 하는 새 직군일 때만 new_role='yes'를 함께 줘 명시적으로 만든다.",
          {"member": str, "role": str, "reason": str, "new_role": str})
    async def recruit(args):
        role_name = (args.get("role") or "").strip()
        spec = (args.get("member") or "").strip()
        # [전문화 정책 — 범용 직군 금지(사용자 결정)] 범용(풀스택 등)은 모든 일을 흡수해 전문 채용을
        # 억제하고(라이브: AI·서버·데이터가 한 봇에 22건 집중) 병렬의 병목이 된다. 전문 직군으로 나눠 뽑는다.
        if role_name and any(g in _norm_job(role_name)
                             for g in ("풀스택", "풀 스택", "fullstack", "full stack", "full-stack",
                                       "제너럴", "generalist", "만능", "올라운드")):
            return _ok(f"채용 거부(전문화 정책): '{role_name}' 같은 범용 직군은 두지 않습니다 — 범용은 모든 "
                       f"일을 흡수해 전문 채용을 막고 병렬의 병목이 됩니다(1봇 1직업 전문화가 회사 원칙). "
                       f"필요한 전문 직군으로 나눠 뽑으세요(예: 백엔드 / 프론트엔드 / AI 엔지니어 / 데이터 엔지니어).")
        # [직군 중복 생성 게이트 — 근본] recruit가 자유 텍스트 직군명을 받다 보니 흐름마다 변형 이름
        # ('VFX 전문가' 있는데 'VFX 아티스트')으로 '같은 도메인 직군'이 새 Discord 역할로 계속 불어났다.
        # 비교 풀은 현재 팀 라벨 + '서버의 커스텀 역할 전체'(직군 역할은 서버 영속이라, 토큰 유실/오프라인
        # 봇의 직군도 보인다). 변형이 감지되면 생성하지 않고 멈춰 세운다 — 재사용(기존 이름 그대로)이나
        # 명시적 신설(new_role='yes')은 에이전트가 정한다(시스템이 정답 이름을 정하는 하드코딩 아님).
        if role_name:
            existing_jobs = {j for v in flow.bot_info.values()
                             if v and not str(v).startswith(_SPARE_LABEL)
                             for j in _jobs_of(v)}   # 겸직 라벨은 구성 직군으로 풀어 비교
            fn_roles = getattr(g, "get_custom_role_names", None)
            if fn_roles and getattr(flow, "guild_id", None):
                try:
                    existing_jobs |= set(await fn_roles(flow.guild_id) or [])
                except Exception:
                    pass
            dup = _find_variant_job(role_name, existing_jobs)
            if dup and _norm_job(args.get("new_role") or "") not in ("yes", "y", "true", "1"):
                if flow.log:
                    flow.log("recruit_variant_blocked", asked=role_name, existing=dup)
                return _ok(f"직군 중복 의심으로 보류: '{role_name}'은(는) 이미 있는 직군 '{dup}'의 변형으로 "
                           f"보입니다(같은 도메인을 다른 이름으로 또 만들면 직군이 계속 불어납니다). 같은 일이면 "
                           f"role='{dup}' 그대로 다시 호출해 기존 직군으로 채용하세요. 정말 '{dup}'과(와) 다른 "
                           f"일을 하는 새 직군이 필요하면 new_role='yes'를 함께 줘 명시적으로 신설하세요.")
        if flow.current is None:
            # [예비 담당자 '자기 직군 우선'] Task 열기 전에 담당자가 자기 직군부터 정하는 건 허용한다 — 자기
            # 자신 + role 지정일 때만. 이래야 '예비'인 채로 create_project/create_task를 열어 화면(상태블록·동료
            # 프롬프트)에 '예비'로 박히는 걸 막는다(사용자가 본 '담당자가 예비로 들어옴'의 직접 원인). 다른 사람
            # 채용 등은 종전대로 Task가 먼저 있어야 한다.
            self_pick = _resolve_members(spec, flow, flow.pool) if spec else []
            if role_name and ((not spec) or (self_pick and self_pick[0] == me_id)):
                # 1봇 1직업: 이 분기는 '예비(무직)' 담당자용이다 — 이미 직군이 있는 봇이 자기 직군을
                # 덮어쓰면(디자이너→게임 기획자) 전문화 기억이 영속 오염된다(라이브 관측). 같은 직군
                # 재확인만 통과시키고, 다른 직군은 거부한다(필요하면 예비를 그 직군으로 뽑는 것).
                cur = (flow._info(me_id) or "").strip()
                new_label = role_name
                if cur and not _is_spare(flow, me_id):
                    cur_jobs = _jobs_of(cur)
                    if any(_norm_job(j) == _norm_job(role_name) for j in cur_jobs):
                        return _ok(f"이미 '{role_name}' 직군을 보유하고 있습니다 — 그대로 진행하세요(변경 없음).")
                    # 겸직 예외(사용자 정책): ① 풀에 예비가 한 명도 없거나 ② 새 직군이 기존 직군과
                    # '비슷한 일'(도메인 토큰 공유)일 때만, **기존 직군을 유지한 채** 새 직군을 더한다
                    # (교체 아님 — 전문화 기억 보존). 봇당 최대 2개(직군 스택 누적 재발 방지). 그 외에는
                    # 1봇 1직업 원칙 — 예비를 그 직군으로 새로 뽑는 게 정도.
                    spares_left = [s for s in flow.pool if _is_spare(flow, s)]
                    similar = any(_job_tokens(j) & _job_tokens(role_name) for j in cur_jobs)
                    if spares_left and not similar:
                        return _ok(f"자기 직군 추가 거부: 당신은 이미 '{cur}' 직군입니다 — **1봇 1직업** 원칙이라 "
                                   f"무관한 직군('{role_name}') 겸직은 예비가 없거나 비슷한 일일 때만 허용됩니다"
                                   f"(전문화 보호). '{role_name}'이 필요하면 Task를 연 뒤 recruit(role='{role_name}')로 "
                                   f"'예비'를 그 직군으로 채용하세요(예비 {len(spares_left)}명).")
                    if len(cur_jobs) >= 2:
                        return _ok(f"겸직 한도 초과: 당신은 이미 직군 2개('{cur}')를 보유하고 있습니다 — 봇당 "
                                   f"겸직은 최대 2개입니다. '{role_name}'은 예비나 다른 동료에게 맡기세요.")
                    new_label = f"{cur}{_JOB_SEP}{role_name}"
                flow.bot_info[me_id] = new_label
                if getattr(flow, "persist_role", None):
                    try:
                        flow.persist_role(me_id, new_label)
                    except Exception:
                        pass
                fn = getattr(g, "assign_job_role", None)
                if fn and getattr(flow, "guild_id", None):
                    try:
                        await fn(flow.guild_id, me_id, new_label)
                    except Exception:
                        pass
                what = "겸직 추가" if _JOB_SEP in new_label else "확정"
                return _ok(f"자기 직군 {what}: 당신(id {me_id})의 직군 = '{new_label}' — 한 직원으로 "
                           f"참여합니다. 이어서 create_project → create_task로 팀을 꾸려 시작하세요.")
            return _ok("오류: 진행 중인 Task가 없습니다. 먼저 create_task로 Task를 여세요. (단 '예비' 담당자가 자기 "
                       "직군을 정하는 recruit(member=자신, role=…)는 Task 전에도 됩니다 — 자기 직군부터 정하세요.)")
        # 충원 루프 하드 차단: 최근 요청이 연속 2회+ 실패(시스템 일시불안정)면 채용을 막는다 — 지금 새로
        # 뽑아도 같은 불안정으로 똑같이 실패한다('백엔드 6명' 사태의 구조적 차단; 안내가 아니라 거부).
        # 기존 동료에게 다시 요청해 한 명이라도 응답이 오면 consec_fail이 리셋돼 다시 채용 가능.
        if getattr(flow, "consec_fail", 0) >= 2:
            return _ok(f"채용 보류: 최근 요청이 연속 {flow.consec_fail}회 무응답/실패 — 시스템 일시 불안정입니다. "
                       f"지금 새로 뽑아도 같이 실패하니 채용을 막습니다(무한 충원 루프 방지). 기존 동료에게 잠시 뒤 "
                       f"다시 요청해 한 명이라도 응답이 오면 그때 충원하거나, 계속 안 되면 사용자에게 보고하고 멈추세요.")
        cand = _resolve_members(spec, flow, flow.pool) if spec else []
        if not cand:
            # member 미지정(또는 못 찾음): 직군 채용이면 '예비' 인력에서 자동 선발(아직 프로젝트팀에 없는 예비)
            spares = [m for m in flow.pool if _is_spare(flow, m) and m not in flow.project_team]
            if role_name and spares:
                cand = [spares[0]]
            else:
                return _ok(f"채용할 인력을 못 찾음 — member로 기존 동료(id/역할)를 지정하거나, role로 새 직군을 "
                           f"적어 '예비'를 채용하세요. 남은 예비: {len(spares)}명 / 현재 풀: {flow._names(flow.pool)}")
        mid = cand[0]
        # 예비(직군 미배정)는 'role=직군'을 줘야만 채용된다 — 말로만 배정 차단(직군은 구조적으로 부여).
        if _is_spare(flow, mid) and not role_name:
            return _ok(f"채용 거부: {flow._info(mid) or mid}는 '예비'(직군 미배정)입니다 — role='직군명'을 함께 "
                       f"지정해 어떤 직군으로 채용할지 정하세요(예: recruit(member='{mid}', role='게임 기획자')). "
                       f"직군 없이는 합류·위임 불가(말로만 배정 금지 — 직군이 실제로 부여돼야 일을 맡길 수 있음).")
        # [같은 직군 채용도 자유] role 중복/실패상태로 채용을 거부하지 않는다 — 반복 채용('백엔드 6명')의 진짜
        # 원인은 '동료 무응답(서브프로세스 행)'이었고 그건 워커 턴 타임아웃으로 끊었다(8분 내 인프라실패 처리).
        # 따라서 필요하면 같은 직군을 더 뽑아도 된다. '무응답=인프라'라는 판단·안내는 요청 실패 메시지로만 한다.
        hired = ""
        if role_name:
            cur = flow._info(mid)
            if _is_spare(flow, mid) or not cur:
                flow.bot_info[mid] = role_name                    # 예비/무직 → 그 직군으로 (런타임만, 이 흐름)
                hired = f" — '{role_name}' 직군으로 채용(잠정 — 첫 실작업 시 영속)"
                # [일로 직업 획득 — 영속 이연] 예비를 직군으로 뽑아도 *지금은 영속하지 않는다*(jobs.json·Discord
                # 보류). 그 봇이 *첫 실작업(Write/Edit/run)*을 하는 순간에만 영속한다(권한 훅이 승격) — '직업=기억'을
                # 문자 그대로. 끝까지 일 안 하면 영속 안 돼 다음 흐름에 예비로 사라진다(0-기억 직군 양산의 근본 차단).
                # 충돌(같은 봇 이중채용)도 무해 — 둘 다 일 안 하면 둘 다 예비로 남는다.
                flow.tentative_roles[mid] = role_name
            elif not any(_norm_job(j) == _norm_job(role_name) for j in _jobs_of(cur)):
                # 이미 다른 직군 보유 — 원칙은 **1봇 1직업**(새 직군은 예비를 뽑는 게 정도). 겸직은 사용자
                # 정책의 예외 둘 중 하나일 때만: ① 풀에 예비가 한 명도 없음(어쩔 수 없음) ② 새 직군이
                # 기존 직군과 '비슷한 일'(도메인 토큰 공유). 허용 시 교체가 아니라 **추가**다 — 기존 전문화
                # 기억(주직군)을 유지한 채 부직군을 더하고, 봇당 최대 2개(직군 5~6개 스택 재발 방지).
                cur_jobs = _jobs_of(cur)
                spares_left = [s for s in flow.pool if _is_spare(flow, s)]
                similar = any(_job_tokens(j) & _job_tokens(role_name) for j in cur_jobs)
                if spares_left and not similar:
                    return _ok(f"채용 거부: {cur}(id {mid})는 이미 '{cur}' 직군입니다 — **1봇 1직업** 원칙이라 "
                               f"무관한 직군('{role_name}') 겸직은 예비가 없거나 비슷한 일일 때만 허용됩니다"
                               f"(전문화 기억 보호). '{role_name}'이 필요하면 recruit(role='{role_name}')로 "
                               f"'예비'를 그 직군으로 새로 뽑으세요(예비 {len(spares_left)}명).")
                if len(cur_jobs) >= 2:
                    return _ok(f"겸직 한도 초과: {flow._info(mid) or mid}(id {mid})는 이미 직군 2개('{cur}')를 "
                               f"보유 — 봇당 겸직은 최대 2개입니다. '{role_name}'은 예비나 다른 동료에게 맡기세요.")
                new_label = f"{cur}{_JOB_SEP}{role_name}"
                flow.bot_info[mid] = new_label
                hired = f" — '{role_name}' 겸직 추가(보유: {new_label})"
                if getattr(flow, "persist_role", None):
                    try:
                        flow.persist_role(mid, new_label)
                    except Exception:
                        pass
            # 이미 그 직군을 보유하고 있으면 라벨 변경 없이 그대로 합류.
            flow.current.status.group = _group_of(flow, flow.current.team)
            # 이름은 그대로 두고 '직군 라벨 전체'를 Discord 역할(권한)로 동기화 — best-effort. 단 *잠정 채용*
            # (예비→직군, 첫 실작업 전)은 보류한다 — 일로 획득하는 순간 SYS가 부여(영속 이연, 양산 차단).
            fn = getattr(g, "assign_job_role", None)
            if fn and getattr(flow, "guild_id", None) and mid not in flow.tentative_roles:
                try:
                    await fn(flow.guild_id, mid, flow.bot_info.get(mid) or role_name)
                except Exception:
                    pass
        if mid not in flow.project_team:
            flow.project_team.append(mid)
        if mid not in flow.current.team:
            flow.current.team.append(mid)
            flow.current.status.group = _group_of(flow, flow.current.team)
            await flow.refresh()
            await _add_members(g, flow.current.thread_id, [mid])   # 스레드에 합류(멤버십=팀)
        return _ok(f"{flow._info(mid) or mid} 합류{hired}(사유: {args.get('reason', '')}). "
                   f"현재 팀: {flow._names(flow.current.team)}")

    tools.append(recruit)

    @tool("run",
          f"작업공간에서 명령을 실행해 산출물을 직접 검증(빌드/구동/테스트). cwd={flow.workspace or '작업공간 루트'} "
          f"(이 절대경로가 작업공간 — `/workspace` 아님). 60s 제한, "
          "웹 작품은 **실제 브라우저 검증 가능**: playwright+chromium 설치됨 — 예: PJT venv의 python -c로 "
          "sync_playwright 페이지 로드→로드시간·콘솔에러·스크린샷 확인('실행됨'과 '사용할 만함'은 다르다). "
          "출력 반환. 서버 구동은 'node server.js & sleep 1; curl -s localhost:3000/'처럼 백그라운드+점검으로 "
          "묶으면 됨 — run이 끝나면 백그라운드 프로세스까지 자동 정리하므로 kill 불필요(다음 run의 포트 충돌 없음). "
          "파괴·git·시스템경로 명령은 차단.",
          {"command": str})
    async def run(args):
        cmd = str(args.get("command", ""))
        if not getattr(flow, "workspace", None):
            return _ok("실행 불가: 작업공간이 설정되지 않았습니다.")
        # [단일활성 구조화 — 논블로킹 핸드오프] 내가 위임을 보내 그 동료가 지금 활성(베턴=동료)인데 내가
        # solo run을 돌리면 '리더+동료 동시 실행'(이중 활성)이 된다. 핸드오프는 request를 즉시 반환하므로
        # 프롬프트가 아니라 구조로 막는다: 내 인플라이트 위임이 살아 있고 내가 비활성이면 run을 거부하고
        # 턴을 마치게 한다 — SYS가 위임을 완주시켜 결과로 나를 재개한다(활성은 언제나 한 명). 동료 자신은
        # 활성(alive==me_id)이라 이 게이트에 안 걸려 자기 작업을 정상 실행한다.
        if (any(not t.done() for t in getattr(flow, "inflight_tasks", ()))
                and flow.comm.alive != me_id and not flow.comm.done):
            return _ok("[대기] 직전 위임이 아직 진행 중입니다 — 지금 직접 실행(run)하면 동료와 동시 작업(이중 "
                       "활성)이 됩니다. 추가 행동 없이 이 턴을 마치세요. 위임이 완료되면 SYS가 그 결과와 함께 "
                       "당신을 다시 깨웁니다(그때 검증·통합하세요).")
        if any(d in cmd.lower() for d in _RUN_DENY):
            return _ok(f"실행 거부(안전): 파괴/저장소/시스템 패턴 포함 — {cmd[:80]}")
        if any(p in cmd for p in _RUN_AUTHOR):
            return _ok("실행 거부: run은 '실행·빌드·검증' 전용입니다 — 파일 작성/수정은 Write/Edit 도구로 "
                       "하세요(그래야 권한·협의 게이트가 적용되고 누가 무엇을 만들었는지 기록됩니다). 예: "
                       "server.js 작성은 Write, 패키지 설치·서버 구동·curl 점검은 run. 남의 도메인 산출물을 "
                       "run으로 대신 찍어내지 말고 그 owner에게 Work로 위임하세요.")

        def _exec():
            # 자체 세션(프로세스그룹)으로 실행 → 직속 셸 종료 후 그룹째 정리한다.
            # 이게 run 간 포트 충돌(EADDRINUSE)의 구조적 해결: 'node server.js &'로 띄운
            # 백그라운드 서버가 init으로 reparent돼 누수되는 일이 없다.
            # 출력은 파이프 대신 임시파일로 — 백그라운드 자식이 파이프를 잡고 있어도 wait가 안 막힌다.
            of, ef = tempfile.TemporaryFile(), tempfile.TemporaryFile()
            p = subprocess.Popen(cmd, shell=True, cwd=str(flow.workspace),
                                 stdout=of, stderr=ef, start_new_session=True)
            timed_out = False
            try:
                rc = p.wait(timeout=60)        # 직속 셸 종료까지만 대기
            except subprocess.TimeoutExpired:
                timed_out, rc = True, None
            finally:
                _reap_pgroup(p.pid)            # 백그라운드 자식까지 그룹째 정리(누수/포트충돌 차단)
                try:
                    p.wait(timeout=2)          # 셸 좀비 회수
                except Exception:
                    pass
            of.seek(0); ef.seek(0)
            out = of.read().decode("utf-8", "replace"); err = ef.read().decode("utf-8", "replace")
            of.close(); ef.close()
            return timed_out, rc, out, err

        try:
            timed_out, rc, out, err = await anyio.to_thread.run_sync(_exec)
        except Exception as e:
            return _ok(f"실행 오류: {e}")
        if timed_out:
            _dbg(f"[RUN] {me_id} `{cmd[:60]}` TIMEOUT")
            return _ok("실행 시간초과(60s) — 그룹째 정리함. 서버는 'node server.js & sleep 1; curl ...'처럼 "
                       "백그라운드로 띄우세요(포그라운드로 서버를 실행하면 멈춥니다). **큰 단일 다운로드/빌드"
                       "(수백MB+ 도구·모델)는 60초에 안 끝납니다 — 작은 패키지·에셋으로, 또는 닿는 경량 대안으로 "
                       "갈아타세요(이 환경엔 GPU 없음·Render는 Node-웹 전용).\n"
                       f"[부분 stdout]\n{out[-800:]}\n[부분 stderr]\n{err[-400:]}")
        _dbg(f"[RUN] {me_id} `{cmd[:60]}` exit={rc}")
        if flow.current is not None:
            flow.current.verified = True          # 실행 0회 완료 차단(layer1)
            flow.current.run_count += 1
            # 시스템이 직접 캡처한 영수증(에이전트 말이 아니라 실제 출력). 완료 보고에 떼어낼 수 없게 묶인다.
            errtail = ("\n[stderr] " + err[-200:]) if (err or "").strip() else ""
            flow.current.evidence = f"exit={rc} `{cmd[:50]}`\n{(out or '')[-400:]}{errtail}"
        return _ok(f"[exit {rc}] (작업공간)\n[stdout]\n{out[-1500:]}\n[stderr]\n{err[-600:]}")

    tools.append(run)

    if role == "leader":
        @tool("create_project",
              "Project로 판단되면 전용 채널 생성 + 규모를 산정해 팀 배정"
              "(team=쉼표구분 동료 id/역할명, 리더 제외분). 비우면 풀 전체.",
              {"name": str, "team": str})
        async def create_project(args):
            if flow.project_channel is not None:
                return _ok(f"이미 project_channel={flow.project_channel} (project_id={flow.project_id}) — "
                           f"개입 중이면 create_project 말고 바로 작업하세요.")
            # [방어] 봇이 이름 앞에 'P-번호'를 끼워넣으면 떼어낸다 — 식별번호는 시스템(register_project)이
            # 자동 부여하는데, 포트폴리오 목록의 'P-NNN' 표기를 흉내 내 이름에 번호를 박으면 채널 이름이
            # 'P-021 …'이 되고 작업공간 폴더가 'p-021-p-021-…'로 번호가 중복된다. 번호는 시스템 몫이다.
            _raw = str(args.get("name") or "").strip()
            _clean = re.sub(r"^\s*[Pp]-\d+[\s:·\-–—.]*", "", _raw).strip()
            args["name"] = _clean or _raw
            flow.project_channel = await g.create_project_channel(flow.guild_id, args["name"])
            # [작업공간 격리·신원] 폴더는 여기서 깎지 않는다 — 흐름은 시작부터 고유 임시 폴더(new-…)에서
            # 일했고, 아래 register_project가 그 폴더를 **식별번호 이름(p-00n-슬러그)으로 개명**해
            # 신원을 번호로 확정한다(리더 작명 충돌이 폴더·배포 수준에서 무해 — 사용자 제안).
            assigned = _resolve_members(args.get("team", ""), flow, flow.pool)
            if assigned:
                flow.project_team = _uniq([flow.leader] + assigned)
            # 프로젝트는 내부 레지스트리에만 등록(채널 자체가 프로젝트 식별자 — 채널에 앵커 안 박음).
            flow.project_name = args["name"]   # 배포 슬롯 유도용(프로젝트별 결정적 서비스명)
            if flow.register_project:
                flow.project_id = flow.register_project(flow.project_channel, args["name"])
            return _ok(f"project_channel={flow.project_channel} project_id={flow.project_id} "
                       f"프로젝트팀={flow._names(flow.project_team)}")
        tools.append(create_project)

        @tool("create_task",
              "Task '빈 껍데기'를 연다 — **Purpose도 비운 채 멤버만 배정**한다(리더가 할 일을 미리 못 박음 = 중앙집권 "
              "방지). 이후 **배정된 팀이 모여(request Info) Purpose(풀 문제)·Goal(성공기준)을 함께 정해 set_goal로 "
              "확정**한다. Owner는 그 일을 Work로 받은 동료가 된다(선배정 금지). **members=이 일에 필요한 직군 동료를 "
              "당신이 직접 고른다**(자동 전원 소집 아님 — 직군 고정 방지). 비우면 프로젝트팀(예비 제외) 기본, 모자란 "
              "직군은 recruit(role=)로 채운다.",
              {"members": str})
        async def create_task(args):
            if flow.current is not None and flow.current.status.status != "완료":
                return _ok(f"현재 Task({flow.current.task_id}: {(flow.current.status.purpose or '미정')[:24]})가 아직 "
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
                return _ok(f"단독 Task 거부: 이 프로젝트엔 동료({flow._names(others)})가 있는데 당신 혼자만 멤버인 "
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
            return _ok(f"task={tid} (빈 껍데기·담당자가 팀 선정) thread={thread_id} 팀={flow._names(team)}{size_note} — 이 팀은 "
                       f"당신이 고른 구성입니다(직군이 부족하면 recruit(role=)로 더하세요). 배정된 팀과 **meet(회의)로 "
                       f"'Purpose(풀 문제)·Goal(성공기준)·각자 도메인 할 일'을 함께 정한 뒤** set_goal로 확정하세요 — "
                       f"meet은 독립의견을 동시에 모으고(앵커링 방지) 토론·회의록(합의)까지 남깁니다(1:1 request(Info)를 "
                       f"여러 번 도는 것보다 합의가 또렷하고 빠름 — 개별 후속 확인만 Info로). 전원 협의 전엔 set_goal "
                       f"거부됨. 그 다음 일을 맡길 동료에게 Work로 위임.")
        tools.append(create_task)

        @tool("set_goal",
              "팀 회의로 정한 이번 Task의 **Purpose(풀 문제)와 Goal(측정가능한 성공기준)**을 확정·기록한다. 리더 "
              "단독/선지정 금지 — **이 Task의 멤버 전원**과 meet(회의)로 'Purpose·각 도메인의 목표·성공기준'을 "
              "수렴한 결과를 적는다(1:1 request(Info)보다 meet 권장 — 앵커링↓·회의록 자동 기록). Goal엔 '무엇이 "
              "되면 성공인가'(결과·시나리오)만 쓰고 '어떤 파일·엔드포인트·스택으로 만들지'(구현 방법)는 쓰지 말 것 — "
              "그건 owner가 정한다. Work 위임은 확정 뒤에만 가능. acceptance(수용 계약)엔 회의에서 각 전문가가 "
              "제안한 '좋음의 구체·검증가능 조건'(훌륭한 예 대비)을 항목으로 적는다 — 마감이 이 항목들의 실현을 검증한다.",
              {"purpose": str, "goal": str, "acceptance": str, "standard": str, "interfaces": str})
        async def set_goal(args):
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
        tools.append(set_goal)

        @tool("complete_task",
              "현재 Task의 목표가 충족되면 상태블록을 완료로 마감(result 기록). 다음 Task는 create_task로.",
              {"result": str})
        async def complete_task(args):
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
            if _std_bind and not re.match(r"^\s*\[\s*최대화\s*(?:N\s*/?\s*A|면제|불필요)", _std_bind):
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
            # [협업 — 인터페이스 직접 합의 강제(2026-06-22 사용자: '전문가끼리 서로 대화하는가')] interfaces(도메인
            # 간 계약)를 선언했는데 owner들이 서로 직접 확인(peer↔peer Info)한 적이 없으면 = 계약을 리더만 경유
            # 전달(사일로·중계 병목)했거나 owner가 추측한 것(P-028 API 미스매치). ≥2개 도메인이 실작업했을 때만
            # (맞물릴 대상이 있을 때) 발동 — 과발동 차단. peer 직접 대화가 생기거나 '[인터페이스 직접합의 N/A:
            # 사유]'일 때까지 보류(persistent-until-resolved — staffing 게이트와 동형, 1회 재호출론 통과 안 됨).
            _iface_x = (getattr(flow.current, "interfaces", "") or "").strip()
            _iface_na = bool(re.search(r"\[\s*인터페이스\s*직접\s*합의\s*(?:n\s*/?\s*a|면제|단독|불필요)",
                                       (args.get("result") or ""), re.IGNORECASE))
            if (has_product and _iface_x and not getattr(flow.current, "peer_info_pairs", None)
                    and not _iface_na and not getattr(flow, "iface_dialogue_checked", False)):
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
        tools.append(complete_task)

        @tool("vote",
              "팀 표결(구조적 합의): 선택지를 두고 멤버 전원의 선택+근거를 **동시에**(독립·앵커링 방지) "
              "수집·집계한다. question=안건, options='선택지1;선택지2;...', members=쉼표구분(비우면 현재 "
              "Task 팀 전원). 1:1 Info를 여러 번 도는 대신 합의를 구조화 — 결과(집계+근거)를 보고 리더가 확정한다.",
              {"question": str, "options": str, "members": str})
        async def vote(args):
            if flow.current is None:
                return _ok("오류: 진행 중인 Task가 없습니다. create_task 먼저 여세요.")
            opts = [o.strip() for o in str(args.get("options", "")).split(";") if o.strip()]
            if len(opts) < 2:
                return _ok("오류: options에 선택지 2개 이상을 ';'로 구분해 주세요.")
            voters = _resolve_members(args.get("members", ""), flow, flow.current.team) or \
                     [m for m in flow.current.team if m != me_id]
            voters = [v for v in voters if v != me_id and not _is_spare(flow, v)]
            if not voters:
                return _ok("오류: 표결할 멤버가 없습니다.")
            if (any(not x.done() for x in getattr(flow, "inflight_tasks", ()))
                    and flow.comm.alive != me_id and not flow.comm.done):
                return _ok("[대기] 직전 위임이 아직 진행 중입니다 — 표결은 그 결과를 받은 뒤 여세요.")
            if getattr(flow, "fork_active", 0) > 0:
                return _ok("[대기] 다른 의견 수집이 진행 중입니다 — 그 결과를 받은 뒤 여세요(중첩 수집 금지).")
            if flow.comm.done or flow.comm.alive != me_id:
                return _ok(f"지금은 표결을 열 수 없습니다(활성={flow.comm.alive}) — 진행 중인 요청의 "
                           f"응답을 받은 뒤 다시 시도하세요.")
            question = str(args.get("question", "")).strip()

            detached = {"on": False}

            async def _run_vote():
                # [병렬 fork-join] 표는 서로 '독립'(앵커링 방지)이라 동시 수집이 의미를 바꾸지 않고
                # 시간만 줄인다 — 수집이 싸지면 표결을 아껴 쓰지 않게 된다(협동 빈도↑ = 품질).
                def body_of(v):
                    return (f"[표결 — 독립 의견] 안건: {question}\n선택지: {' / '.join(opts)}\n"
                            f"동료들의 표는 보이지 않습니다(앵커링 방지). 당신의 전문가 관점에서 "
                            f"하나를 고르고 근거를 2줄 이내로. 반드시 형식: [표] 선택지명\n근거")
                tally, reasons = {o: 0 for o in opts}, []
                dom_picks = {o: set() for o in opts}   # 옵션 → 그 옵션을 고른 '도메인'들(같은 직군 중복 제거)
                for v, res, note in await _fork_collect(flow, me_id, voters, body_of):
                    if res is None:
                        reasons.append(f"{flow._info(v) or v}: {note}")
                        continue
                    m = re.search(r"\[표\]\s*([^\n]+)", res or "")
                    pick = (m.group(1).strip() if m else "")
                    chosen = next((o for o in opts if o in pick or pick in o), None)
                    if chosen:
                        # [동질 모델 — 표는 도메인(관점) 단위 집계] 같은 Claude·같은 직군 표는 같은 관점이라
                        # N표가 아니라 1관점이다. 봇 수가 아니라 '다른 관점 수'로 세야 표결이 다양성을 반영
                        # (같은 직군 3명이 같은 선택 = 3표가 아니라 그 직군 1표) — 봇 수 편향 제거. 도메인이
                        # 갈리면(동질 모델이라 드묾) 각 옵션에 그 도메인을 1회씩 센다.
                        _vd = {_norm_job(j) for j in _jobs_of(flow._info(v) or "")} - {""}
                        _vdk = sorted(_vd)[0] if _vd else f"·{v}"
                        if _vdk not in dom_picks[chosen]:
                            dom_picks[chosen].add(_vdk)
                            tally[chosen] += 1
                    # [판정자 사본도 침묵 절단 금지] 리더는 이 근거로 표결을 '판정'한다 — 채널
                    # 발언(400 안전망+잘림 표기)과 같은 내용이어야 한다. 종전 [:150] 하드컷은
                    # 판정자가 동강난 근거로 결정하게 만들던 같은 부류의 결함(잘림 사건의 잔재).
                    reasons.append(f"{flow._info(v) or v}: {(pick or '무효')} — {_speech_clip(res, 400)}")
                    await _say(v, f"[표] {(pick or '무효')} — {_speech_clip(res, 400)}")  # 본인 명의 발언
                    if v in flow.current.team and v != flow.leader:
                        flow.current.participated.add(v)        # 표결 참여 = 실질 협의 인정
                board = " / ".join(f"{o}: {n}관점" for o, n in tally.items())
                if flow.current is not None:
                    record = f"[표결] {question}\n{board}\n" + "\n".join(reasons)
                    flow.current.collab_notes = _speech_clip(
                        (getattr(flow.current, 'collab_notes', '') + '\n\n' + record).strip(), 6000)
                    _ckpt(flow)
                return _ok(f"[표결 집계 — 도메인(관점) 단위] {question}\n{board}\n\n[각자의 선택·근거]\n"
                           + "\n".join(reasons)
                           + "\n\n(집계는 **도메인 단위** — 같은 직군 N명의 같은 선택은 동질 모델이라 1관점으로 "
                           + "합산(봇 수가 아니라 다른 관점 수). 참고일 뿐, 최종 판정은 당신(리더).)")

            inner = asyncio.ensure_future(_run_vote())
            flow.inflight_tasks.add(inner)
            inner.add_done_callback(flow.inflight_tasks.discard)
            try:
                return await asyncio.shield(inner)
            except asyncio.CancelledError:
                if not inner.done():
                    detached["on"] = True
                    if flow.log:
                        flow.log("delegation_detached", to="vote", seg=flow.leader_segment)

                    def _hand(t):
                        try:
                            flow.detached_results.append(f"표결 완료 → {_speech_clip(t.result()['content'][0]['text'], 4000)}")
                        except Exception:
                            pass
                    inner.add_done_callback(_hand)
                raise
        tools.append(vote)

        @tool("meet",
              "라운드로빈 회의: 1라운드는 전원의 '독립 의견'을 동시에 수집하고(앵커링 방지), 2라운드부터 "
              "서로의 발언을 보며 직렬로 토론한다(회의록 반환). topic=주제, members=쉼표구분(비우면 현재 "
              "Task 팀 전원), rounds=라운드 수(기본 2). 1:1 중계 없이 실제 다자 토론을 구조화 — 회의록을 "
              "보고 리더가 수렴·확정한다.",
              {"topic": str, "members": str, "rounds": str})
        async def meet(args):
            if flow.current is None:
                return _ok("오류: 진행 중인 Task가 없습니다. create_task 먼저 여세요.")
            members = _resolve_members(args.get("members", ""), flow, flow.current.team) or \
                      [m for m in flow.current.team if m != me_id]
            members = [m for m in members if m != me_id and not _is_spare(flow, m)]
            if not members:
                return _ok("오류: 회의할 멤버가 없습니다.")
            if (any(not x.done() for x in getattr(flow, "inflight_tasks", ()))
                    and flow.comm.alive != me_id and not flow.comm.done):
                return _ok("[대기] 직전 위임이 아직 진행 중입니다 — 회의는 그 결과를 받은 뒤 여세요.")
            if getattr(flow, "fork_active", 0) > 0:
                return _ok("[대기] 다른 의견 수집이 진행 중입니다 — 그 결과를 받은 뒤 여세요(중첩 수집 금지).")
            if flow.comm.done or flow.comm.alive != me_id:
                return _ok(f"지금은 회의를 열 수 없습니다(활성={flow.comm.alive}) — 진행 중인 요청의 "
                           f"응답을 받은 뒤 다시 시도하세요.")
            topic = str(args.get("topic", "")).strip()
            try:
                rounds = max(1, min(3, int(str(args.get("rounds", "2")).strip() or "2")))
            except ValueError:
                rounds = 2

            async def _run_meet():
                minutes = []
                # 1라운드 = 독립 의견 fork(동시 수집) — 첫 입장은 서로를 안 보는 게 앵커링 없는
                # 진짜 다양성이고, 동시 수집이라 회의 비용도 준다(회의가 싸져야 자주 연다 = 협동성).
                def body_r1(m):
                    return (f"[회의 1라운드 — 독립 의견] 주제: {topic}\n(이 라운드에선 동료 발언이 "
                            f"보이지 않습니다 — 앵커링 방지)\n당신({flow._info(m)})의 전문 관점 "
                            f"입장을 3~5줄(최대 1000자)로, 근거와 함께.")
                for m, res, note in await _fork_collect(flow, me_id, members, body_r1):
                    cut = _speech_clip(res or note)   # 회의록·채널 발언은 같은 내용(기록 일치)
                    line = f"[1R] {flow._info(m) or m}: {cut}"
                    minutes.append(line)
                    await _say(m, f"[회의 1R] {cut}")  # 본인 명의 발언
                    if res is not None and m in flow.current.team and m != flow.leader:
                        flow.current.participated.add(m)        # 회의 발언 = 실질 협의 인정
                # 2라운드+ = 직렬 상호 토론(서로의 발언을 보며 동의/반박/보완) — 품질의 원천인
                # 순차 문맥은 병렬화 대상이 아니다(여기는 종전 그대로).
                for r in range(2, rounds + 1):
                    for m in members:
                        if flow.comm.done or flow.comm.alive != me_id:
                            break
                        log_txt = "\n".join(minutes[-8:]) or "(아직 발언 없음)"
                        body = (f"[회의 {r}라운드] 주제: {topic}\n지금까지의 발언:\n{log_txt}\n\n"
                                f"당신({flow._info(m)})의 차례입니다 — 앞 발언에 동의/반박/보완하며 "
                                f"당신 전문 관점의 입장을 3~5줄(최대 1000자)로. 맹목적 동의 금지(근거 필수).")
                        try:
                            frame = flow.comm.request(me_id, m, "meet", Kind.INFO)
                        except BusyInOtherFlow as e:
                            # 멤버 단위 사유(라운드 사이에 타 흐름이 데려감) — 회의를 끊지 않고 그
                            # 멤버만 건너뛴다(부분 진행). 베턴 경합(아래)과 달리 시스템 문제가 아니다.
                            minutes.append(f"[{r}R] {flow._info(m) or m}: (타 흐름({e.holder_scope}) "
                                           f"참여 중 — 이 라운드 불참)")
                            continue
                        except CommError as e:
                            minutes.append(f"(회의 중단 — 베턴 경합: {str(e)[:60]})")
                            break
                        try:
                            res = await flow.wake(m, body, Kind.INFO)
                        except Exception as e:
                            res = f"(발언 실패: {e})"
                        try:
                            flow.comm.respond(m, "accept", res)
                        except CommError:
                            pass
                        cut = _speech_clip(res)
                        line = f"[{r}R] {flow._info(m) or m}: {cut}"
                        minutes.append(line)
                        await _say(m, f"[회의 {r}R] {cut}")  # 본인 명의 발언
                        if m in flow.current.team and m != flow.leader:
                            flow.current.participated.add(m)    # 회의 발언 = 실질 협의 인정
                if flow.current is not None:
                    record = f"[회의] {topic} ({rounds}R)\n" + "\n".join(minutes)
                    flow.current.collab_notes = _speech_clip(
                        (getattr(flow.current, 'collab_notes', '') + '\n\n' + record).strip(), 6000)
                    _ckpt(flow)   # 합의는 크래시-세이프(재개 위임에도 동봉되도록 스냅샷에 포함)
                return _ok(f"[회의록] 주제: {topic} ({rounds}라운드, {len(members)}명)\n"
                           + "\n".join(minutes)
                           + "\n\n(수렴·확정은 당신(리더)의 몫 — 합의점을 정리해 set_goal/결정에 반영하세요.)")

            inner = asyncio.ensure_future(_run_meet())
            flow.inflight_tasks.add(inner)
            inner.add_done_callback(flow.inflight_tasks.discard)
            try:
                return await asyncio.shield(inner)
            except asyncio.CancelledError:
                if not inner.done():
                    if flow.log:
                        flow.log("delegation_detached", to="meet", seg=flow.leader_segment)

                    def _hand(t):
                        try:
                            flow.detached_results.append(f"회의 완료 → {_speech_clip(t.result()['content'][0]['text'], 4000)}")
                        except Exception:
                            pass
                    inner.add_done_callback(_hand)
                raise
        tools.append(meet)

        @tool("parallel_work",
              "파일 영역이 겹치지 않는 **독립 Work 여러 건을 동시에** 위임(병렬 실행+직렬 통합, RFC-006). "
              "assignments=JSON 배열 '[{\"to\":\"봇id\",\"files\":\"상대경로,상대경로\",\"body\":\"지시\"}]'. "
              "각자 배정된 files에만 쓸 수 있다(쓰기 리스 — 영역 겹침은 거부). 영역이 겹치거나 순서 의존이면 "
              "request(Work) 직렬로. 조인 후 통합·검증·마감은 직렬로 진행.",
              {"assignments": str})
        async def parallel_work(args):
            # [RFC-006 Work-fork v1] 검증된 fork 인프라(_fork_collect: 점유·부분 조인·FAN·detach-safe
            # 코어)에 Work 의미론(쓰기 리스·owner·실작업 판정)을 입힌다 — alive-집합 전면 개편 없이
            # '병렬 실행 + 직렬 통합'(RFC-005 P1)을 연다. 가지는 comm 프레임을 열지 않으므로 재위임
            # 불가(구조 강제) — 실측 근거: P-009·P-010 워커의 중첩 request 0회(막히면 보고→리더 직렬).
            # [병렬 비활성화 — 단일흐름 안정성(2026-06-22 사용자 결정)] 병렬 fork는 가지 에이전트의 작업공간
            # cwd 불일치 + 게이트#9(비-fork 전문가 idle 오발) + 쓰기리스로 Write를 잃어 산출물 0 churn을
            # 유발했다(P-029 규명). 전제가 '단일흐름 안정성'이므로 병렬 Work를 끄고 직렬(request)로 돌린다 —
            # 통합·검증은 어차피 직렬이라 손실 없음. 테스트는 _parallel_enabled로 실경로 검증(경로 수정 후 해제).
            if not getattr(flow, "_parallel_enabled", False):
                return _ok("[병렬 비활성화] 병렬 Work는 현재 비활성화돼 있습니다 — 작업공간/게이트 정합 문제로 "
                           "가지의 산출물이 유실되는 불안정이 확인됐습니다(P-029). **독립 영역도 request(Work)로 "
                           "한 명씩 직렬 위임**하세요(단일흐름 안정성 우선 — 통합·검증은 어차피 직렬).")
            if flow.current is None:
                return _ok("오류: 진행 중인 Task가 없습니다. create_task 먼저 여세요.")
            goal = (flow.current.status.goal or "").strip()
            if not goal:
                return _ok("오류: Goal 확정 전엔 병렬 위임 불가 — set_goal 먼저(분할은 합의된 목표 위에서).")
            if getattr(flow, "fork_active", 0) > 0:
                return _ok("[대기] 다른 수집/병렬이 진행 중입니다 — 조인 후 시도하세요(중첩 병렬 금지).")
            if (any(not x.done() for x in getattr(flow, "inflight_tasks", ()))
                    and flow.comm.alive != me_id and not flow.comm.done):
                return _ok("[대기] 직전 위임이 아직 진행 중입니다 — 결과를 받은 뒤 병렬을 여세요.")
            try:
                items = json.loads(args.get("assignments") or "")
                assert isinstance(items, list) and items
            except Exception:
                return _ok('형식 오류: assignments는 JSON 배열 — 예: [{"to":"12","files":"public/app.js","body":"..."}]')
            fan = max(1, int(os.environ.get("ORGANT_FORK_FAN", "3")))
            if len(items) < 2:
                return _ok("병렬은 2건부터입니다 — 1건은 request(Work)로 위임하세요.")
            if len(items) > fan:
                return _ok(f"병렬 폭 초과({len(items)} > {fan}) — 가장 독립적인 {fan}건만 먼저, 나머지는 조인 후.")
            ws = str(getattr(flow, "workspace", "") or "")
            plan = []
            for it in items:
                try:
                    to = int(str(it.get("to")).strip())
                except Exception:
                    return _ok(f"형식 오류: to가 봇 id가 아닙니다: {it.get('to')!r}")
                if to == me_id:
                    return _ok("자기 자신에게는 병렬 위임 불가 — 자기 몫은 조인 후 직접.")
                if to not in flow.current.team:
                    return _ok(f"요청 거부: {flow._info(to) or to}는 이 Task 팀이 아닙니다 — 팀에 더한 뒤 위임하세요.")
                if _is_spare(flow, to):
                    return _ok(f"요청 거부: {flow._info(to) or to}는 직군 미배정('예비') — recruit로 직군 부여 먼저.")
                files = [f.strip() for f in str(it.get("files") or "").split(",") if f.strip()]
                if not files:
                    return _ok(f"형식 오류: {flow._info(to) or to}의 files가 비었습니다 — 병렬의 전제는 영역 분리(리스).")
                body = str(it.get("body") or "").strip()
                if not body:
                    return _ok(f"형식 오류: {flow._info(to) or to}의 body(지시)가 비었습니다.")
                paths = [os.path.realpath(os.path.join(ws, f)) for f in files]
                plan.append((to, paths, body))
            tos = [p[0] for p in plan]
            if len(set(tos)) != len(tos):
                return _ok("같은 동료에게 두 영역 동시 배정 — 한 건으로 합치세요.")
            # [토큰 중립 조건 ⓐ — 기계 강제] 영역 상호 배타: 일치/포함이면 거부(겹침은 통합 충돌→Redo→토큰 손실).
            for i in range(len(plan)):
                for j in range(i + 1, len(plan)):
                    for a in plan[i][1]:
                        for b in plan[j][1]:
                            if a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep):
                                return _ok(f"영역 겹침 거부: {flow._info(plan[i][0])} ↔ {flow._info(plan[j][0])} "
                                           f"({os.path.basename(a)}) — 겹치는 작업은 직렬(request)로.")
            notes = getattr(flow.current, "collab_notes", "")
            m2 = {to: (paths, body) for to, paths, body in plan}

            def body_of(m):
                paths, body = m2[m]
                files_txt = ", ".join(os.path.relpath(p, ws) if ws else p for p in paths)
                t = (f"[병렬 Work — 이 영역의 책임자는 당신] 이 Task의 Goal: {goal}\n"
                     f"**당신의 쓰기 영역(리스): {files_txt}** — 이 파일들에만 씁니다. 다른 가지가 다른 "
                     f"영역을 동시 작업 중이므로 영역 밖은 Read 참고만 하고, 필요한 변경은 보고의 "
                     f"[리스크]에 적으세요. 동료 재위임은 불가(병렬 가지) — 막히면 막힌 지점을 보고하면 "
                     f"리더가 직렬로 풉니다. 직군 밖이면 첫 줄 `[직군밖] 필요직군` 반려.\n"
                     f"직접 구현하고 run으로 검증한 뒤, 보고 계약([결과]/[변경]/[검증]/[리스크])으로 간결히.\n"
                     f"[요청 맥락] {body}")
                if notes:
                    t += f"\n[팀 협의 기록(회의·표결) — 준수]\n{_speech_clip(notes, 6000)}"
                return t

            acts0 = {to: flow.act_by.get(to, 0) for to in tos}
            if getattr(flow, "write_lease", None) is None:
                flow.write_lease = {}
            for to, paths, _b in plan:
                flow.write_lease[to] = paths
            if flow.log:
                flow.log("parallel_work", n=len(tos), to=",".join(map(str, tos)), seg=flow.leader_segment)

            async def _run_parallel():
                try:
                    results = await _fork_collect(flow, me_id, tos, body_of, kind=Kind.WORK)
                finally:
                    for to in tos:
                        flow.write_lease.pop(to, None)   # 조인=리스 해제(겹침 게이트는 가지 동안만)
                out = []
                for m, res, note in results:
                    acted = flow.act_by.get(m, 0) - acts0.get(m, 0)
                    if res is not None and flow.current and m in flow.current.team and m != flow.leader:
                        flow.current.participated.add(m)
                    if flow.current:
                        flow.current.work_delegated += 1
                    mark = "" if acted > 0 else " ⚠실작업 0(계획만 — 같은 영역 직렬 재위임 고려)"
                    await _say(m, f"[병렬 보고] {_speech_clip(res or note, 1500)}")
                    out.append(f"[{flow._info(m) or m}]{mark}\n{_speech_clip(res or note, 4000)}")
                if flow.current and not flow.current.owner:
                    flow.current.owner = tos[0]   # 기존 규칙(첫 Work 수신자=owner)과 일관 — 통합 기준점
                    if flow.act_by.get(tos[0], 0) > acts0.get(tos[0], 0) and any(
                            m == tos[0] and r is not None for m, r, _n in results):
                        flow.current.owner_delivered = True
                if flow.log:
                    flow.log("parallel_join", n=len(results), seg=flow.leader_segment)
                _ckpt(flow)
                return _ok(f"[병렬 조인 — {len(results)}건]\n" + "\n\n".join(out)
                           + "\n\n(통합·교차 검증·마감은 직렬로 — 겹치는 후속 작업은 request(Work) 한 명에게.)")

            inner = asyncio.ensure_future(_run_parallel())
            flow.inflight_tasks.add(inner)
            inner.add_done_callback(flow.inflight_tasks.discard)
            try:
                return await asyncio.shield(inner)
            except asyncio.CancelledError:
                if not inner.done():
                    if flow.log:
                        flow.log("delegation_detached", to="parallel", seg=flow.leader_segment)

                    def _hand(t):
                        try:
                            flow.detached_results.append(
                                f"병렬 조인 → {_speech_clip(t.result()['content'][0]['text'], 4000)}")
                        except Exception:
                            pass
                    inner.add_done_callback(_hand)
                raise
        tools.append(parallel_work)


        @tool("deploy",
              "검증을 마친 산출물을 실제로 공개 배포한다(GitHub push + Render 웹서비스 생성/갱신). "
              "name=영문 소문자·하이픈 서비스명(예: slither-multiplayer). 라이브 URL을 반환. "
              "Node 앱이어야 하고 서버는 process.env.PORT를 사용해야 함. run 검증을 끝낸 뒤 마지막에 호출.",
              {"name": str})
        async def deploy(args):
            # [배포 폴링 차단] 재호출은 '점검'이 아니라 새 배포 트리거(git push+빌드 리셋)다 — 빌드가
            # 길어지면 리더가 1분마다 deploy를 다시 불러 빌드를 계속 리셋하는 자기 영속 루프 + 같은 턴
            # 병렬 4연발이 라이브 관측됨([안내][배포] 도배). 흐름당 동시 1회만, 진행 중엔 [대기].
            if getattr(flow, "deploy_inflight", False):
                return _ok("[대기] 배포가 이미 진행 중입니다 — deploy를 다시 부르지 마세요(재호출은 점검이 "
                           "아니라 **새 배포를 또 트리거**해 빌드를 계속 리셋합니다). 진행 중인 배포의 "
                           "성공/실패 결과가 곧 이 도구의 응답으로 돌아옵니다 — 그때 판단하세요.")
            # [런어웨이 배포 차단 — 횟수 상한(2026-06-21 라이브 P-028: 깨진 배포를 코드 바꿔가며 23회 재배포한 루프)]
            # 위의 anti-thrash는 '코드 변경 없는 재배포'만 잡아, '코드를 만지며 무한 재배포'(근본은 배포 구조 문제라 코드
            # 수정으론 안 고쳐짐)는 통과됐다. 흐름당 실배포 N회를 넘으면 *하드 차단*하고 사용자 보고로 에스컬레이트 —
            # 못 고치는 걸 무한 재시도하는 토큰·빌드 낭비를 구조적으로 끊는다(횟수는 품질판정 아닌 안전 백스톱).
            if getattr(flow, "_deploy_count", 0) >= 5:
                if flow.log:
                    flow.log("deploy_cap", count=flow._deploy_count)
                return _ok(
                    "[배포 중단 — 5회 초과(런어웨이 차단)] 이 작업에서 배포를 이미 5번 시도했습니다. 라이브가 아직 "
                    "정상이 아니라면 이건 *앱 코드*가 아니라 **배포 구조/타겟 문제**일 가능성이 큽니다(예: Node 서버가 "
                    "Python을 spawn하는데 Render Node 환경엔 Python이 없음 — 같은 종류의 불일치). **더 재배포하지 마세요** "
                    "— (1) 라이브 URL을 run으로 curl해 실제 상태를 확인하고, (2) 코드로 못 고치는 배포 구조 문제면 "
                    "complete_task에 '배포 구조 문제: <원인>'을 정직하게 적어 사용자에게 보고하세요. 무한 재시도는 금지입니다.")
            # [배포 반-스래싱 — 변경 없는 재배포 차단(2026-06-21 라이브 P-026: 리더가 18회 재배포로 30분 낭비)]
            # Render 무료 빌드는 60s+라 deploy_sync가 *타임아웃*으로 보여도 빌드는 계속 진행된다. 리더가 '실패했나'
            # 싶어 코드 변경 없이 재배포하면 빌드를 처음부터 리셋해 *더 느려진다*(자기영속 thrash — deploy_inflight는
            # *동시*만 막고 *순차* 재배포는 못 막음). 직전 배포 이후 Write/Edit가 0이면(=같은 코드) 차단하고 'URL을
            # curl 확인하라'로 돌린다 — 진짜 결함을 고쳤으면 writes가 늘어 통과(교차검증 cc_held와 같은 정신, deploy판).
            _dwrites = sum((getattr(flow, "writes_by_role", None) or {}).values())
            if getattr(flow, "_deployed_once", False) and _dwrites == getattr(flow, "_deploy_writes", -1):
                if flow.log:
                    flow.log("deploy_thrash", writes=_dwrites)
                return _ok("[재배포 차단 — 직전 배포 후 코드 변경 없음] Render 무료 빌드는 60초+라 deploy가 "
                           "*타임아웃*으로 보여도 **빌드는 계속 진행 중**입니다. 변경 없이 재배포하면 빌드를 처음부터 "
                           "리셋해 *더 느려집니다*(라이브 P-026: 18회 재배포로 30분 낭비). **재배포하지 말고 ~90초 뒤 "
                           "라이브 URL을 `run`으로 curl 확인**하세요(HTTP 200이면 완료 — 그 URL을 그대로 보고). 정말 "
                           "결함을 고쳤다면(Write/Edit) 그 변경 후에 1회만 재배포하세요.")
            name = deploy_service_name(flow, args.get("name", ""))   # 프로젝트별 결정적 서비스명
            if not name:
                return _ok("배포 불가: 미등록 흐름은 배포 슬롯이 없습니다 — 배포는 프로젝트마다"
                           "(P-번호 슬롯, organt-p-00n) 설정됩니다. create_project로 등록한 뒤 "
                           "다시 배포하세요.")
            gh, ghu = os.environ.get("GH_PAT"), os.environ.get("GH_USER")
            rk, owner = os.environ.get("RENDER_KEY"), os.environ.get("RENDER_OWNER")
            if not (gh and ghu and rk and owner):
                return _ok("배포 불가: 배포 자격증명(GH_PAT/GH_USER/RENDER_KEY/RENDER_OWNER)이 설정되지 않았습니다.")
            if not getattr(flow, "workspace", None):
                return _ok("배포 불가: 작업공간이 없습니다.")
            # [배포 타겟 호환 사전검증 — 첫 배포 전에(2026-06-22 P-028)] Render Node 런타임엔 Python이 없다 —
            # 서버가 런타임에 Python을 spawn하면 라이브에서 502로 죽는다. 5회 상한(사후)이 아니라 *지금* 잡아
            # 명확한 처방을 준다(토큰·빌드 낭비 차단). 빌드타임 학습용 Python은 통과 — 런타임 의존만 차단.
            _infeasible = _deploy_infeasibility(flow.workspace)
            if _infeasible:
                if flow.log:
                    flow.log("deploy_infeasible", reason=_infeasible[:80])
                return _ok(
                    f"배포 불가(타겟 호환 — Render Node 전용): {_infeasible} 배포 타겟은 **Render Node 런타임 "
                    f"하나뿐**이라 런타임에 Python이 없습니다 — 이 구조는 라이브에서 502로 죽습니다(P-028: "
                    f"ECONNREFUSED, 모델 고아화). 고치는 법(해당 owner에게 위임): ① **Node로만 서빙** — Python은 "
                    f"*빌드타임 오프라인 학습*에만 쓰고 예측을 미리 계산해 JSON으로 떨궈 Node가 서빙(정적/캐시), "
                    f"또는 ② 모델을 **ONNX/TF.js로 변환**해 Node에서 추론. 런타임 Python spawn/exec를 제거한 뒤 "
                    f"다시 배포하세요(이건 코드 수정으로 안 고쳐지는 *구조* 문제 — 재시도 말고 아키텍처를 바꾸세요).")
            from .deploy import deploy_sync
            flow.deploy_inflight = True
            flow._deploy_writes = _dwrites         # 이 배포 시점의 저작 수 — 다음 배포가 '변경 없음'을 판정
            _dep = {"on": False}

            async def _do_deploy():
                # [논블로킹 배포 — 단일흐름 안정성(2026-06-22)] Render 빌드는 수 분(deploy_sync 폴링 480초)이라
                # 도구 호출 안에서 기다리면 75초 CLI 한도에 잘려 detach→리더가 '실패로 오인'→재배포 thrash
                # (라이브 P-026 18회·P-028 23회)의 뿌리였다. 위임과 동일하게: 즉시 반환하고 deploy_sync는
                # 인플라이트로 돌려 SYS가 호출 *밖*에서(75초 미적용·idle 720초>빌드 480초) 완주시켜 라이브 URL로
                # 리더를 재개한다. 베턴은 안 건드린다(배포는 위임 아님) — 동시 재배포는 deploy_inflight가 단속.
                try:
                    r = await anyio.to_thread.run_sync(deploy_sync, flow.workspace, name, gh, ghu, rk, owner)
                except Exception as e:
                    r = f"배포 처리 오류: {e}"
                flow.deploy_inflight = False
                flow.deployed = r                  # 배포 호출됨 기록(SYS의 배포 강제가 중복 안 하게)
                flow._deployed_once = True
                flow._deploy_count = getattr(flow, "_deploy_count", 0) + 1   # 런어웨이 상한 카운트(실배포만 +1)
                if _dep["on"]:
                    flow.detached_results.append(f"배포 결과 → {_speech_clip(r, 4000)}")
                return _ok(r)

            inner = asyncio.ensure_future(_do_deploy())
            flow.inflight_tasks.add(inner)
            inner.add_done_callback(flow.inflight_tasks.discard)
            if getattr(flow, "_handoff", False):
                _dep["on"] = True
                return _ok("[배포 트리거됨 — SYS가 빌드가 라이브가 될 때까지 확인해 그 결과(라이브 URL)로 당신을 "
                           "재개합니다. **재배포·추가 행동 없이 이 턴을 마치세요** — 재배포는 빌드를 처음부터 "
                           "리셋합니다. 결과가 도착하면 그때 URL을 확인·보고하세요.]")
            try:
                return await asyncio.shield(inner)
            except asyncio.CancelledError:
                if not inner.done():
                    _dep["on"] = True       # 도구 호출만 죽고 배포는 계속 — 결과는 detached로 전달
                raise
        tools.append(deploy)

        @tool("send_file",
              "산출물 파일을 사용자에게 Discord 첨부로 보낸다 — 사용자가 '파일로 받고 싶다'고 했거나 산출물이 "
              "파일 형태(이미지·문서·데이터·코드 번들 등)일 때만(항시 보내지 말 것). path=작업공간 기준 상대경로, "
              "caption=한 줄 설명(선택). 25MB 이하만 — 큰 건 deploy(배포 URL)로.",
              {"path": str, "caption": str})
        async def send_file(args):
            rel = str(args.get("path", "")).strip()
            if not rel:
                return _ok("오류: path가 비었습니다(작업공간 기준 상대경로를 주세요).")
            ws = getattr(flow, "workspace", None)
            if not ws:
                return _ok("전송 불가: 작업공간이 없습니다.")
            # [보안] 작업공간 안의 파일만 — 경로 탈출(../)·시스템 경로 차단
            base = os.path.realpath(str(ws))
            full = os.path.realpath(os.path.join(base, rel))
            if not (full == base or full.startswith(base + os.sep)):
                return _ok("전송 거부: 작업공간 밖 경로는 보낼 수 없습니다 — 작업공간 기준 상대경로를 주세요.")
            if not os.path.isfile(full):
                return _ok(f"전송 거부: 그런 파일이 없습니다 — {rel}(작업공간 기준). run으로 만든 뒤 보내세요.")
            sz = os.path.getsize(full)
            if sz > 25 * 1024 * 1024:
                return _ok(f"전송 거부: {sz // (1024 * 1024)}MB — Discord 첨부 한도(25MB) 초과. 큰 산출물은 "
                           f"deploy(배포 URL)로 전달하세요.")
            try:
                await g.send_file(flow.user_channel, full, sender_id=me_id,
                                  caption=str(args.get("caption", "")))
            except Exception as e:
                return _ok(f"파일 전송 오류: {e}")
            if flow.log:
                flow.log("file_sent_to_user", path=rel, size=sz, seg=getattr(flow, "leader_segment", 0))
            _dbg(f"[FILE→user] {me_id} {rel} ({sz}B)")
            return _ok(f"파일 전송됨 → 사용자: {rel} ({sz // 1024}KB). 사용자가 Discord에서 직접 받습니다.")
        tools.append(send_file)

    return tools


def build_guide_server(flow: Flow, me_id: int, role: str):
    return create_sdk_mcp_server("guide", "1.0.0", make_guide_tools(flow, me_id, role))
