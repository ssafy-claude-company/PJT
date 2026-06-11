#!/bin/bash
# Organt 인터랙티브 리스너 자동재시작 래퍼 (durable — repo 내 보관, /tmp 휘발 대비).
# 죽어도 되살리고(while-true), 죽는 원인은 listener.log에 남는다. ORGANT_SKIP_RECOVERY=1로
# 깨끗한 슬레이트 시작(이전 미응답 요청 자동 재실행 안 함 — 사용자가 보내는 게 첫 처리).
# 토큰(ORGANT_BOT_*/SYSTEM_BOT)은 gitignore된 .env에서 python-dotenv가 로드한다(여기엔 비밀 없음).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo 루트(scripts/의 상위) — 클론 위치 무관
cd "$HERE" || exit 1
# [중복 기동 잠금] 같은 토큰으로 리스너가 2개 뜨면 게이트웨이 세션이 복제돼 '같은 요청을 두 프로세스가
# 각각 처리'(흐름·채널·레지스트리 이중화)한다 — 라이브 관측. flock으로 단일 인스턴스를 구조적으로 보장.
exec 9>/tmp/organt_listener.lock
flock -n 9 || { echo "이미 실행 중인 리스너가 있습니다 — 중복 기동 거부"; exit 1; }
[ -d .venv ] && source .venv/bin/activate
export PYTHONUNBUFFERED=1
# 부팅 복구 기본 = 실행(0): 재시작 틈새·수신 좀비로 유실된 미응답 요청을 자동으로 구한다(라이브에서
# 2회 실제 유실→복구 성공). [Response]가 달린 요청은 건너뛰므로 중복 실행 위험 없음. 깨끗한
# 슬레이트가 필요하면 ORGANT_SKIP_RECOVERY=1을 명시.
export ORGANT_SKIP_RECOVERY="${ORGANT_SKIP_RECOVERY-0}"
# [필수] CLI의 MCP 도구 호출 '하드 월클럭 타임아웃' 해제(사실상): 이 시스템의 request 도구는 동료의
# 중첩 작업이 끝날 때까지 수십 분 블록되는 게 정상 설계인데, CLI 기본 한도가 이를 몇 분에 끊으면
# 리더는 '타임아웃' 에러를 받고 파이썬 핸들러는 베턴을 쥔 고아로 남아 흐름 전체가 헛돈다(라이브 관측).
# 행 감지는 CLI가 아니라 SYS의 침묵 워치독(turn_timeout/idle_timeout, 활동 기반)이 담당한다.
export MCP_TOOL_TIMEOUT="${MCP_TOOL_TIMEOUT:-14400000}"   # 4h(ms) — 정상 긴 위임을 안 끊게
export MCP_TIMEOUT="${MCP_TIMEOUT:-120000}"               # MCP 서버 시작 대기 2m(ms)
# 카나리아 주기 단축(300→120s): 컨테이너 동면(세션 비활성 → 박스째 정지) 후 해동되면 시계점프
# 감지로 자가 재시작하는데, 그 '박제 시간'(해동→감지)을 최대 5분→2분으로 줄인다(라이브 관측:
# 2시간 동면 후 사용자가 박제된 '진행' 상태를 먼저 목격). 동면 자체는 환경 본성이라 막을 수 없고,
# 피해(작업물·요청)는 영속+부팅복구가 0으로 만든다 — 이 값은 '깨어난 뒤 어색한 시간'만 줄인다.
export ORGANT_CANARY_PERIOD="${ORGANT_CANARY_PERIOD:-120}"
# 이어가기 예산: 낭비(폴링·churn)는 구조적으로 차단돼 있어, 한도는 '큰 작업을 자르는 일'만 없게 넉넉히.
# (라이브 관측: 결함수정 개입이 생산적 세그먼트 7개 필요 — 기본 6으로 마감 직전에 끊김)
export ORGANT_MAX_CONTINUE="${ORGANT_MAX_CONTINUE:-12}"
export CHANNEL_ID="${CHANNEL_ID:-1510828120490643517}"
export DEPLOY_NAME="${DEPLOY_NAME:-todo-organt-demo}"
# 로스터(직군만 — 담당자는 [Request]의 To로 런타임 결정): 2~7 시드 직군, 8~100·TEST_BOT_1 예비.
# 토큰이 든 슬롯만 활성화된다(빈 슬롯 자동 제외). 환경변수로 ORGANT_ROSTER를 주면 그게 우선.
# TEST_OBT_2/TEST_OBT_3은 실행환경(env)에 영속 설정된 구세대 토큰 — ORGANT_BOT_*가 든 .env는
# gitignore라 컨테이너 리클레임 때 사라지므로, 이 폴백 슬롯 덕에 리클레임 직후에도 팀이 선다.
if [ -z "${ORGANT_ROSTER:-}" ]; then
  R="ORGANT_BOT_2:백엔드;ORGANT_BOT_3:백엔드;ORGANT_BOT_4:프론트엔드;ORGANT_BOT_5:프론트엔드;ORGANT_BOT_6:디자이너;ORGANT_BOT_7:QA"
  for i in $(seq 8 100); do R="$R;ORGANT_BOT_$i:예비"; done
  export ORGANT_ROSTER="$R;TEST_OBT_2:프론트엔드;TEST_OBT_3:디자이너;TEST_BOT_1:예비"
fi
# 프로젝트 레지스트리의 시드 복원은 파이썬이 한다(sys_core._load_projects): logs/projects.json이
# 없으면 커밋 시드(organt/projects.seed.json)를 'seeded' 마커와 함께 적재하고, 부팅 reconcile이
# Discord 채널 토픽(런타임마다 갱신되는 영속 진실원)으로 최신화한다 — 셸에서 cp 하면 마커가 없어
# '토픽 > 시드' 우선순위가 깨지므로 여기선 복사하지 않는다.
while true; do
  echo "===== [$(date +%H:%M:%S)] 리스너 시작 ====="
  python -m src.main
  echo "===== [$(date +%H:%M:%S)] 리스너 종료(exit=$?) — 3초 후 재시작 ====="
  sleep 3
done
