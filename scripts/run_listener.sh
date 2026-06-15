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
# 이어가기 예산 = '연속 무진행 한도'(활동 기반 — 진행 시 카운터 리셋, 핸드오프 40). 진행하는 한
# 무제한이고, 연속 N회 헛돌 때만 정체로 종결한다. 총량 한도 시절 '큰 작업이 마감 직전 절단'
# 사고 2회(기본 6 시절 P-002 / 12 시절 P-010) — 의미 교정으로 재발 불가.
export ORGANT_MAX_CONTINUE="${ORGANT_MAX_CONTINUE:-6}"
export CHANNEL_ID="${CHANNEL_ID:-1510828120490643517}"
# DEPLOY_NAME 주입은 제거됨(2026-06-12) — 배포 슬롯은 프로젝트 신원(P-번호)으로만 정해지며,
# 미등록 흐름은 슬롯이 없다(공유 슬롯 폴백이 P-002 라이브를 덮어쓸 수 있던 위험 종결).
# 로스터(직군만 — 담당자는 [Request]의 To로 런타임 결정): **첫 항목이 리더**(BOT_2). 직군의 진실원은
# 시드가 아니라 recruit→jobs.json/Discord 역할(영속, main.py:276)이다 — 시드의 직군 하드코딩은 첫 부팅
# 폴백일 뿐인데, 오히려 봇을 직군에 묶어 '예비 환원'을 막는 군더더기였다(2026-06-15 교정: 양산 봇을 예비로
# 못 되돌리던 원인). 그래서 시드는 **린**하게 둔다 — 리더만 직군, 나머지는 예비. 컨테이너 리클레임 후에도
# Discord 역할이 직군을 복원하므로(영속 진실원) 팀은 그대로 선다. 직군 구성은 전적으로 런타임 recruit가 결정.
# TEST_BOT_1이 워커 계정 시리즈의 1번(username 'testtest')이다 — 별도 ORGANT_BOT_1 변수는 없다.
# 토큰이 든 슬롯만 활성화된다(빈 슬롯 자동 제외). 환경변수로 ORGANT_ROSTER를 주면 그게 우선.
if [ -z "${ORGANT_ROSTER:-}" ]; then
  R="ORGANT_BOT_2:게임 기획자;ORGANT_BOT_3:예비;ORGANT_BOT_4:예비;ORGANT_BOT_5:예비;ORGANT_BOT_6:예비;ORGANT_BOT_7:예비"
  for i in $(seq 8 100); do R="$R;ORGANT_BOT_$i:예비"; done
  export ORGANT_ROSTER="$R;TEST_OBT_2:예비;TEST_OBT_3:예비;TEST_BOT_1:예비"
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
