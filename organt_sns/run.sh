#!/bin/bash
# Organt SNS 백엔드 기동 (Phase 1 — read-only 라이브 대시보드).
# 두뇌의 logs/flow.jsonl·audit.jsonl을 읽기만 한다(무위험). 포트 기본 8800.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/backend" || exit 1
export ORGANT_PJT="${ORGANT_PJT:-/home/user/PJT}"
exec python -m uvicorn app:app --host 0.0.0.0 --port "${ORGANT_SNS_PORT:-8800}" "$@"
