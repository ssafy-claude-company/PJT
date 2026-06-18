#!/bin/bash
# SessionStart 훅 (사용자 승인됨): (1) 의존성(.venv) 보장 → 웹 세션에서 테스트/실행 가능,
# (2) Organt 리스너 자동 기동 → 컨테이너 리클레임/세션 재시작으로 죽어도 자동 복구(수동 재시작 불필요).
set -uo pipefail

# 원격(Claude Code on the web) 세션에서만 — 로컬 작업엔 영향 없음.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0

DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$DIR" || exit 0

# 1) 의존성 보장(.venv) — 멱등(컨테이너 캐시 활용; 이미 있으면 빠르게 통과).
#    설치 실패를 삼키지 않는다: 종전 `2>/dev/null || true`가 playwright 누락을 가려 비전검증(M2)이
#    런타임에 거짓("run 도구 설명: playwright 설치됨")이 되던 것(2026-06-15 발견). 침묵 금지 원칙.
if [ ! -x .venv/bin/python ]; then python -m venv .venv 2>/dev/null || python3 -m venv .venv; fi
mkdir -p logs
if ! .venv/bin/python -m pip install -q -r requirements.txt -r requirements-dev.txt > logs/setup_deps.log 2>&1; then
  echo "[session-start] ⚠ 의존성 설치 실패 — logs/setup_deps.log 확인(비전검증 등 영향)"
fi

# 2) 브라우저 검증 인프라(비전검증 M2의 런타임 전제) — 결과를 로그로 남긴다(백그라운드, 5분 예산 비점유).
( .venv/bin/playwright install chromium > logs/setup_chromium.log 2>&1 \
  && .venv/bin/python -c "from playwright.sync_api import sync_playwright" 2>>logs/setup_chromium.log \
  && echo "[playwright] chromium 검증 인프라 준비됨" >> logs/setup_chromium.log \
  || echo "[playwright] ⚠ chromium 설치/검증 실패 — 비전검증 불가" >> logs/setup_chromium.log ) &

# 3) Organt 리스너 자동 기동(임시 ON — 사용자 요청 '하트비트', 2026-06-18): 세션 시작/해동마다
#    감독자(supervisor.sh)를 띄워 리스너가 죽어 있으면 되살린다 → 컨테이너 동면/세션 재시작으로
#    멈춰도 다음 세션 활동에 자동 복구(수동 재시작 불필요). 감독자·리스너는 각자 flock으로 단일
#    인스턴스를 보장하므로 중복 기동은 구조적으로 거부된다(idempotent). .env(비밀)가 있어야 워커가 붙는다.
#    [임시] 차용 세션 자격증명으로 상시 가동하는 형태라, 외부 환경 이전 시 이 블록을 되돌린다.
if [ -f .env ] && ! pgrep -f "scripts/supervisor.sh" >/dev/null 2>&1; then
  setsid nohup bash scripts/supervisor.sh >> logs/supervisor.log 2>&1 < /dev/null &
  echo "[session-start] 의존성 보장 + Organt 감독자 자동 기동(하트비트 임시 ON) — 리스너 자동 복구."
elif [ ! -f .env ]; then
  echo "[session-start] 의존성 보장. .env 없음 — 리스너 자동 기동 건너뜀."
else
  echo "[session-start] 의존성 보장. Organt 감독자 이미 가동 중(하트비트 ON)."
fi
