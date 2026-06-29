#!/usr/bin/env bash
# 작업공간 복구 — '해당 문제(작업공간 유실로 봇이 원본 접근 불가)' 재발 방지 도구.
#
# 왜 필요한가: 봇 작업공간은 로컬 파일(기본 $ORGANT_PJT/organt_sns_workspace/<pid>)이라
# 컨테이너 교체·서버 이전 시 통째로 사라질 수 있다(2026-06-29 VPS 이전 때 실제 발생).
# 그러나 **배포된 프로젝트의 소스는 모노레포의 '<서비스명>' 브랜치에 영속 백업**된다
# (deploy.py가 거기로 push하고 Render가 그 브랜치에서 배포 — 항상 최신). 따라서 작업공간이
# 비어도 그 브랜치에서 언제든 복원할 수 있다.
#
# 사용법:
#   목록(배포된=복원 가능한 프로젝트):  scripts/recover_workspace.sh --list      (RENDER_KEY 필요)
#   복원:                              scripts/recover_workspace.sh <서비스명> <대상디렉터리>
#     예) scripts/recover_workspace.sh fps-game-1v1 /home/user/PJT/organt_sns_workspace/fps
#
# 이전/마이그레이션 후 한 번에 복원하려면: --list 로 이름 확인 → 각 프로젝트별로 복원 1회.
set -euo pipefail
REPO="${ORGANT_DEPLOY_REPO:-https://github.com/ssafy-claude-company/PJT}"

if [ "${1:-}" = "--list" ]; then
  : "${RENDER_KEY:?RENDER_KEY 환경변수가 필요합니다(목록 조회용)}"
  curl -fsS -H "Authorization: Bearer $RENDER_KEY" "https://api.render.com/v1/services?limit=100" \
    | python3 -c "import sys,json
for it in json.load(sys.stdin):
    s=it.get('service',it)
    print(f\"{s.get('name'):28} ← branch {s.get('branch')}  {(s.get('serviceDetails') or {}).get('url','')}\")"
  exit 0
fi

SVC="${1:?서비스명(=배포 브랜치명)을 주세요. 목록은 --list}"
DEST="${2:?복원할 대상 디렉터리를 주세요}"
echo "복원: '$SVC' 브랜치 → $DEST"
rm -rf "$DEST"
git clone --depth 1 --branch "$SVC" "$REPO" "$DEST"
echo "✅ 완료 — $DEST 에 $(ls "$DEST" | wc -l)개 항목 복원됨. (소스는 배포 브랜치에 영속 백업되어 있으니 언제든 재실행 가능.)"
