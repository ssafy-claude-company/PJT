#!/bin/bash
# Organt 리스너 감독자 (분리 프로세스 — durable, repo 보관).
# 왜 세션 Monitor가 아니라 분리 프로세스인가(2026-06-12 전환):
#   ① 세션 Monitor는 persistent 지정에도 30분 캡으로 죽는다(라이브 2회 관측) — 30분마다
#      재무장 = 항목 25가 폐기한 '타임아웃마다 모델 깨움' 비용의 부활.
#   ② 분리 프로세스는 Claude 세션이 끝나도 컨테이너가 사는 한 생존(감시 공백 제거), 토큰 0.
#   ③ argv가 "bash scripts/supervisor.sh"뿐이라 외부 pkill 리터럴 매칭(동반 사망) 면역 —
#      파일 내용은 pkill -f 매칭 대상이 아니므로 아래 리터럴들은 안전하다.
# 파일명에 'run_listener'를 안 쓰는 것도 의도: 리스너 정지용 pkill 패턴과 겹치지 않게.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE" || exit 1
# [단일 감독자] '딱 1개만' 규칙을 flock으로 구조 보장 — 중복 무장은 기동 거부.
exec 9>/tmp/organt_supervisor.lock
flock -n 9 || { echo "이미 실행 중인 감독자가 있습니다 — 중복 무장 거부"; exit 1; }
echo "[감독자] 무장 ($(date '+%m-%d %H:%M:%S')) pid=$$"
while true; do
  if ! pgrep -f "python -m src.main" >/dev/null 2>&1; then
    echo "[감독자] 리스너 부재 → 자동 재기동 ($(date '+%m-%d %H:%M:%S'))"
    setsid nohup bash scripts/run_listener.sh >> logs/listener.log 2>&1 < /dev/null &
    sleep 100
    if pgrep -f "python -m src.main" >/dev/null 2>&1 \
       && tail -8 logs/listener.log | grep -q "User 입력 대기 중"; then
      echo "[감독자] 재기동 성공 — ready 확인 ($(date '+%m-%d %H:%M:%S'))"
    else
      echo "[감독자] 재기동 후 ready 미확인 — 점검 필요 ($(date '+%m-%d %H:%M:%S'))"
    fi
  fi
  sleep 20
done
