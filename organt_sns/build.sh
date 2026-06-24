#!/usr/bin/env bash
# Render 빌드 — Vue dist는 미리 빌드해 레포에 포함(Render python 런타임엔 Node 없음).
# 여기선 백엔드 의존성 설치 + DB 마이그레이트 + 데모 시드 적재만.
set -o errexit
pip install -r backend/requirements.txt
python backend/manage.py migrate --noinput
python backend/manage.py loaddata seed
