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

# 2) Organt 리스너 자동 기동 — 이미 떠 있으면 스킵, 아니면 백그라운드 detached 실행(세션 시작을 막지 않음).
if pgrep -f "python -m src.main" >/dev/null 2>&1; then
  echo "[session-start] Organt 리스너 이미 실행 중 — 스킵"
else
  nohup bash "$DIR/scripts/run_listener.sh" > /tmp/listener.log 2>&1 &
  echo "[session-start] Organt 리스너 백그라운드 기동(자동 복구)"
fi
