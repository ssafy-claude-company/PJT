#!/usr/bin/env bash
# E2E 실행 — 격리 SQLite로 migrate→seed→서버 기동→Playwright 어서션→정리.
# 사전: 러너 venv(django+DRF) 파이썬, node + playwright(전역 또는 npm i), 크로미움.
#   E2E_PY    : django 가진 파이썬 (기본 python). 예: /home/user/PJT/.venv/bin/python
#   E2E_PORT  : 서버 포트 (기본 8099)
#   PW_CHROMIUM: 사전설치 크로미움 실행경로(선택). 예: /opt/pw-browsers/chromium
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$HERE/../backend"
PORT="${E2E_PORT:-8099}"
PY="${E2E_PY:-python}"
DB="$HERE/.e2e.sqlite3"

export SQLITE_PATH="$DB"            # settings.py가 읽는 격리 DB 경로
export DJANGO_DEBUG="${DJANGO_DEBUG:-1}"
rm -f "$DB"

cd "$BACKEND"
echo "[e2e] migrate + seed (db=$DB)"
"$PY" manage.py migrate --noinput >/dev/null
"$PY" manage.py shell < "$HERE/seed_e2e.py"

echo "[e2e] start server :$PORT"
"$PY" manage.py runserver "127.0.0.1:$PORT" --noreload >"$HERE/.e2e-server.log" 2>&1 &
SRV=$!
cleanup() { kill "$SRV" 2>/dev/null || true; rm -f "$DB"; }
trap cleanup EXIT

for i in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$PORT/api/stats/" >/dev/null 2>&1 && break
  sleep 0.5
done

export BASE="http://127.0.0.1:$PORT"
export NODE_PATH="${NODE_PATH:-$(npm root -g)}"
echo "[e2e] run playwright (BASE=$BASE)"
node "$HERE/timeline.cjs"
