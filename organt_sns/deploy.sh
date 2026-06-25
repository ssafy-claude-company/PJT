#!/usr/bin/env bash
# 라이브(Render organt-sns) 배포 트리거 — 최소권한 Deploy Hook 사용.
#
# ⚠️ 풀 Render API 키는 쓰지 않는다(계정 전체 권한·모든 env 시크릿 읽기·서비스 삭제라 과함).
#    Render → organt-sns → Settings → Deploy Hook 의 URL을 env RENDER_DEPLOY_HOOK 에만 둔다(커밋 금지).
#
# 사용: main 에 push 한 뒤  `bash organt_sns/deploy.sh`
#   → Render가 최신 커밋으로 build.sh(migrate 0009~0012 + seed_if_empty) 재배포. 2~3분 후 반영.
# Hook 미설정이면 Render 대시보드 → Manual Deploy → "Deploy latest commit" 로도 동일.
set -euo pipefail

if [ -z "${RENDER_DEPLOY_HOOK:-}" ]; then
  echo "RENDER_DEPLOY_HOOK 미설정 — Render Settings의 Deploy Hook URL을 env에 넣어주세요(풀 API 키 금지)." >&2
  echo "또는: Render 대시보드 → organt-sns → Manual Deploy → 'Deploy latest commit'." >&2
  exit 1
fi

case "$RENDER_DEPLOY_HOOK" in
  https://api.render.com/deploy/*) : ;;
  *) echo "경고: RENDER_DEPLOY_HOOK 형식이 Render deploy hook(https://api.render.com/deploy/…)이 아닙니다." >&2 ;;
esac

echo "▶ Render 배포 트리거(deploy hook)…"
curl -fsS -X POST "$RENDER_DEPLOY_HOOK"
echo ""
echo "✓ 트리거됨 — 2~3분 후 라이브 반영(build.sh: migrate + seed). https://organt-sns.onrender.com"
