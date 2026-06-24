#!/usr/bin/env bash
# Render 빌드 — Vue dist는 미리 빌드해 레포에 포함(Render python 런타임엔 Node 없음).
# 여기선 백엔드 의존성 설치 + DB 마이그레이트 + (비어있을 때만) 데모 시드 적재.
set -o errexit
pip install -r backend/requirements.txt
python backend/manage.py migrate --noinput
# 멱등 시드 — 영속 DB(자체 서버)에선 기존 데이터를 덮어쓰지 않는다. Render(매 배포 새 FS)는 매번 시드.
python backend/manage.py seed_if_empty
# 이름 없는 봇에 고유 이름 배정(직군≠이름). 멱등.
python backend/manage.py name_agents
