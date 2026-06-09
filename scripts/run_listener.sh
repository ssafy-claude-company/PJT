#!/bin/bash
# Organt 인터랙티브 리스너 자동재시작 래퍼 (durable — repo 내 보관, /tmp 휘발 대비).
# 죽어도 되살리고(while-true), 죽는 원인은 listener.log에 남는다. ORGANT_SKIP_RECOVERY=1로
# 깨끗한 슬레이트 시작(이전 미응답 요청 자동 재실행 안 함 — 사용자가 보내는 게 첫 처리).
# 토큰(ORGANT_BOT_*/SYSTEM_BOT)은 gitignore된 .env에서 python-dotenv가 로드한다(여기엔 비밀 없음).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo 루트(scripts/의 상위) — 클론 위치 무관
cd "$HERE" || exit 1
[ -d .venv ] && source .venv/bin/activate
export PYTHONUNBUFFERED=1
export ORGANT_SKIP_RECOVERY="${ORGANT_SKIP_RECOVERY:-1}"
export CHANNEL_ID="${CHANNEL_ID:-1510828120490643517}"
export DEPLOY_NAME="${DEPLOY_NAME:-todo-organt-demo}"
# 로스터(직군만 — 담당자는 [Request]의 To로 런타임 결정): 2~7 시드 직군, 8~100·TEST_BOT_1 예비.
# 토큰이 든 슬롯만 활성화된다(빈 슬롯 자동 제외). 환경변수로 ORGANT_ROSTER를 주면 그게 우선.
if [ -z "${ORGANT_ROSTER:-}" ]; then
  R="ORGANT_BOT_2:백엔드;ORGANT_BOT_3:백엔드;ORGANT_BOT_4:프론트엔드;ORGANT_BOT_5:프론트엔드;ORGANT_BOT_6:디자이너;ORGANT_BOT_7:QA"
  for i in $(seq 8 100); do R="$R;ORGANT_BOT_$i:예비"; done
  export ORGANT_ROSTER="$R;TEST_BOT_1:예비"
fi
# 프로젝트 레지스트리 복원: logs/projects.json은 gitignore(/logs/*)라 컨테이너 리클레임(재클론) 때 사라진다.
# 커밋된 시드(organt/projects.seed.json)가 있는데 레지스트리가 없으면(=reclaim으로 유실) 시드에서 복원 →
# 등록된 프로젝트 채널이 reclaim을 넘어 살아남는다('채널 사라짐' 방지). 레지스트리가 이미 있으면(런타임이
# 최신) 건드리지 않는다.
if [ -f "$HERE/organt/projects.seed.json" ] && [ ! -f "$HERE/logs/projects.json" ]; then
  mkdir -p "$HERE/logs" && cp "$HERE/organt/projects.seed.json" "$HERE/logs/projects.json"
  echo "[restore] logs/projects.json 없음 → 시드에서 복원(프로젝트 등록 유지)"
fi
while true; do
  echo "===== [$(date +%H:%M:%S)] 리스너 시작 ====="
  python -m src.main
  echo "===== [$(date +%H:%M:%S)] 리스너 종료(exit=$?) — 3초 후 재시작 ====="
  sleep 3
done
