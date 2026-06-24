#!/usr/bin/env bash
# run_sns_brain.sh — SNS 두뇌(SnsGuide 러너) 슈퍼바이저 (Phase 2 라이브).
#
#   두뇌(클로드 CLI가 있는 호스트)에서 돌며, 배포된 SNS(Render)에 guide_bridge로 말한다.
#   러너가 죽으면(턴 한도·예외·OOM) 자동 재기동 — 라이브 디스코드 Organt의 supervisor와 같은 원리.
#   컨테이너 자체가 회수되면 외부에서 이 스크립트를 다시 띄워야 한다(디스코드 두뇌와 동일 제약).
#
#   필요 env:
#     ORGANT_SNS_URL     원격 SNS base URL (기본 https://organt-sns.onrender.com)
#     ORGANT_GUIDE_TOKEN guide_bridge 토큰 (Render env와 동일해야 함)
#     ORGANT_PJT         두뇌 소스 루트 (기본 /home/user/PJT)
#   사용:  ORGANT_GUIDE_TOKEN=... bash organt_sns/run_sns_brain.sh
set -u

PJT="${ORGANT_PJT:-/home/user/PJT}"
URL="${ORGANT_SNS_URL:-https://organt-sns.onrender.com}"
PY="${ORGANT_PY:-$PJT/.venv/bin/python}"
BACKEND="$PJT/organt_sns/backend"
LOG="${ORGANT_SNS_LOG:-$PJT/organt_sns_state/brain.log}"
mkdir -p "$(dirname "$LOG")"

if [ -z "${ORGANT_GUIDE_TOKEN:-}" ]; then
  echo "ORGANT_GUIDE_TOKEN 미설정 — guide_bridge 인증 불가. 종료." >&2
  exit 1
fi

# 일부 샌드박스에서 foreground sleep이 막힌다 — timeout 폴백으로 안전하게 쉰다.
_nap() { timeout "$1" tail -f /dev/null 2>/dev/null || sleep "$1"; }

echo "[supervisor] SNS 두뇌 시작 — 매체=$URL  (Ctrl+C 종료)" | tee -a "$LOG"
n=0
while true; do
  n=$((n+1))
  echo "[supervisor] 러너 기동 #$n $(date -u +%FT%TZ)" | tee -a "$LOG"
  ORGANT_PJT="$PJT" "$PY" "$BACKEND/manage.py" run_organt_sns \
      --remote "$URL" --token "$ORGANT_GUIDE_TOKEN" --poll 3 >>"$LOG" 2>&1
  code=$?
  echo "[supervisor] 러너 종료(code=$code) — 4초 후 재기동" | tee -a "$LOG"
  _nap 4
done
