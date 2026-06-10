#!/bin/bash
# SessionStart 훅 (사용자 승인됨): (1) 의존성(.venv) 보장 → 웹 세션에서 테스트/실행 가능,
# (2) Organt 리스너 자동 기동 → 컨테이너 리클레임/세션 재시작으로 죽어도 자동 복구(수동 재시작 불필요).
set -uo pipefail

# 원격(Claude Code on the web) 세션에서만 — 로컬 작업엔 영향 없음.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0

DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$DIR" || exit 0

# 1) 의존성 보장(.venv) — 멱등(컨테이너 캐시 활용; 이미 있으면 빠르게 통과).
if [ ! -x .venv/bin/python ]; then python -m venv .venv 2>/dev/null || python3 -m venv .venv; fi
.venv/bin/python -m pip install -q -r requirements.txt -r requirements-dev.txt 2>/dev/null || true

# 2) Organt 리스너 자동 기동은 '비활성화'됨.
#    이유: 자율 봇 군을 운영자 감독 없이(차용 세션 자격증명으로) 상시 가동하지 않기 위함.
#    리스너가 필요하면 .env(비밀값)를 갖춘 상태에서 수동으로 `bash scripts/run_listener.sh`를 직접 실행한다.
echo "[session-start] 의존성만 보장. 리스너 자동 기동은 비활성화됨(필요 시 수동 실행)."
