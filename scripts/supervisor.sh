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
# [생존 판정 = flock (2026-06-19 교정)] pgrep -f "python -m organt_discord.main"은 그 리터럴을 argv에 가진
# *다른 셸*(진단·대기 루프 등)을 리스너로 오인(self-match)해, 리스너가 죽었는데도 '살아있다'고 판정→
# 재기동을 건너뛰는 버그가 있었다(라이브: 동면 해동 후 리스너 부재인데 감독자가 respawn 안 함 — 사용자
# '리스너 깨워줘'에서 규명). 리스너만 잡는 유일·확실한 신호는 그가 쥔 flock이다 — lock이 *획득되면*
# (-c "exit 0" 성공=non-busy) 리스너 부재. ready 판정도 tail-8 grep(채널 토픽실패 noise가 밀어내 오탐)
# 대신 'sleep 100 뒤에도 lock held'(=기동 후 생존)로 본다.
while true; do
  if flock -n /tmp/organt_listener.lock -c "exit 0" 2>/dev/null; then
    echo "[감독자] 리스너 부재(lock free) → 자동 재기동 ($(date '+%m-%d %H:%M:%S'))"
    setsid nohup bash scripts/run_listener.sh >> logs/listener.log 2>&1 < /dev/null &
    sleep 100
    if ! flock -n /tmp/organt_listener.lock -c "exit 0" 2>/dev/null; then
      echo "[감독자] 재기동 성공 — 프로세스 가동(lock held) ($(date '+%m-%d %H:%M:%S'))"
    else
      echo "[감독자] 재기동 후에도 lock free — 기동 실패, 점검 필요 ($(date '+%m-%d %H:%M:%S'))"
    fi
  fi
  sleep 20
done
