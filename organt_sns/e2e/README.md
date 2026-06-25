# Organt SNS — E2E (Playwright)

구조화 타임라인 고도화(회의/표결 블록·라운드 구분·수명주기 페이즈 구분선)와 사용성
기능(멎은 요청 재시도·채널 내/공개 섹셔닝·협업 엔진 표시)을 **실제 브라우저로** 굳히는
회귀 가드. 격리 SQLite에 결정적 시드를 넣고 Django를 띄운 뒤 Playwright로 DOM을 검사한다.

## 무엇을 검사하나 (`timeline.cjs`)
- **회의 블록**: 발화자 무관 한 블록 + 참여자 스택 + 1·2 **라운드 구분선**.
- **표결 블록**: 발언이 한 블록으로(집계는 만들지 않음 — Status Rule).
- **페이즈 구분선**: 목표·완료·배포 마디.
- **멎은 요청**: 복구 바 노출 → `다시 맡기기` 클릭 → 재큐되어 바 사라짐.
- **홈**: 협업 엔진 `가동 중` 표시(heartbeat) + 채널 **내/공개 섹셔닝**.

## 사전 준비
- Django+DRF 가진 파이썬(러너 venv). 프론트 빌드(`dist/`)는 레포에 포함 — 수정 시
  `cd ../frontend && npm run build` 후 재실행.
- Node + `playwright`(전역 또는 `npm i playwright`) + 크로미움.
  이 저장소 환경: 크로미움은 `/opt/pw-browsers`에 사전설치(= `playwright install` 불필요).

## 실행
```bash
cd organt_sns/e2e
E2E_PY=/home/user/PJT/.venv/bin/python \
PW_CHROMIUM=/opt/pw-browsers/chromium \
bash run-e2e.sh
```
- 끝에 `ALL PASS`면 통과. 실패 항목은 `FAILED:` 목록으로 출력하고 종료코드 1.
- 격리 DB(`.e2e.sqlite3`)·서버 로그(`.e2e-server.log`)는 실행 중에만 쓰이고 정리된다(gitignore).

## 변수
| 변수 | 기본 | 설명 |
|---|---|---|
| `E2E_PY` | `python` | django 가진 파이썬 경로 |
| `E2E_PORT` | `8099` | 임시 서버 포트 |
| `PW_CHROMIUM` | (자동) | 사전설치 크로미움 실행경로 |
| `NODE_PATH` | `npm root -g` | 전역 playwright 위치 |
