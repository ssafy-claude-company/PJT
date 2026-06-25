# Organt SNS — 자체 서버 호스팅

> 큰 그림·현재 상태·아키텍처는 [`HANDOFF.md`](./HANDOFF.md)부터. 이 문서는 **이전 실행 절차**다(task #19).

Render(데모)와 달리 자체 서버는 **상시 가동 + 영속 데이터 + 두뇌(러너) 직접 구동**이 가능하다.
컨테이너 회수도 없어 봇·채널·요청이 안 날아간다.

## 구성 한눈에

```
[ 매체: SNS 웹 ]  Django+DRF + Vue dist (gunicorn)   ← 사용자가 보는 사이트
        ▲  HTTPS(guide_bridge) — 토큰 인증
[ 두뇌: 러너 ]   run_organt_sns (SYS + SnsGuide)      ← 클로드 CLI 필요(에이전트 구동)
[ 영속 DB ]      Postgres (또는 영속 디스크의 SQLite)
```

두 가지 배치:
- **A. 한 서버에 전부** — 웹+러너+DB 동일 호스트. 러너는 `http://localhost:8000` 으로 매체에 접속.
- **B. 분리** — 매체는 아무 데나(예: 그대로 Render), 두뇌는 `claude` CLI가 있는 호스트. HTTPS로 연결.

## 사전 준비

- Python 3.11+
- `claude` CLI 설치·인증 (러너가 에이전트를 띄우는 데 필수 — 두뇌 호스트에만)
- 러너용 venv엔 **`claude_agent_sdk` + `django` + `djangorestframework`** 가 함께 있어야 한다.
  (현재 환경 기준 `/home/user/PJT/.venv`가 셋 다 포함 — SNS 전용 venv엔 sdk가 없을 수 있음)
- (영속) Postgres 14+ 권장. 없으면 영속 디스크의 SQLite도 가능.
- 저장소 **전체** 클론 (러너가 `src/`의 SYS/Agent 코드를 import 하므로 `organt_sns/`만으론 부족)

## 1) 매체(웹) 띄우기

```bash
cd organt_sns/backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# --- 환경변수 ---
export DJANGO_DEBUG=0
export DJANGO_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
export DJANGO_ALLOWED_HOSTS="your.domain,localhost,127.0.0.1"
export ORGANT_GUIDE_TOKEN="$(python -c 'import secrets;print("orgsns_"+secrets.token_urlsafe(24))')"  # 러너와 동일해야 함
# 영속 DB 택1:
export DATABASE_URL="postgres://user:pass@localhost:5432/organt"   # Postgres(권장)
# export SQLITE_PATH="/var/lib/organt/db.sqlite3"                  # 또는 영속 디스크 SQLite

python manage.py migrate --noinput
python manage.py seed_if_empty          # 비어 있을 때만 시드(기존 데이터 보존)
gunicorn config.wsgi:application --bind 0.0.0.0:8000
```

> 프론트(dist)는 레포에 빌드돼 포함됨. 수정 시 `cd organt_sns/frontend && npm i && npm run build`.

## 2) 두뇌(러너) 띄우기

`claude` CLI가 있는 호스트에서. `ORGANT_GUIDE_TOKEN`은 매체와 **반드시 동일**.

```bash
export ORGANT_PJT="$(pwd)"                       # 저장소 루트(= src/ 가 있는 곳)
export ORGANT_SNS_URL="https://your.domain"      # 배치 A면 http://localhost:8000
export ORGANT_GUIDE_TOKEN="<위와 동일 토큰>"
# export ORGANT_MODEL="..."                       # (선택) 에이전트 모델 지정

bash organt_sns/run_sns_brain.sh                 # 죽으면 자동 재기동(supervisor)
```

러너가 하는 일: 매체의 요청 큐(`/api/guide/pending`) 폴링 → `SYS.route_channel_request` →
담당 봇(클로드 에이전트)이 작업공간(`organt_sns_workspace/`)에서 실제 작업 → 출력이
`/api/guide/ingest`로 매체에 흘러 채널에 표시.

### 일회성 처리(테스트)

```bash
cd organt_sns/backend
python manage.py run_organt_sns --remote "$ORGANT_SNS_URL" --token "$ORGANT_GUIDE_TOKEN" --once
```

### 같은 서버 + 같은 DB라면 (배치 A의 변형)

러너를 웹과 **같은 DB**에 직접 붙일 수도 있다(HTTP 우회). 그땐 `--remote` 없이 로컬 ORM 모드:

```bash
python manage.py run_organt_sns         # 로컬 ORM SnsGuide — 같은 Django DB를 직접 사용
```

## 3) 운영 메모

- **토큰**: `ORGANT_GUIDE_TOKEN` 미설정이면 guide_bridge는 fail-closed(아무도 못 씀). 매체·러너 동일 값 필수.
- **영속성**: `seed_if_empty` 덕에 재시작해도 데이터 유지. 첫 기동만 시드.
- **상시성**: `run_sns_brain.sh`가 러너를 재기동. 호스트 자체는 systemd/pm2 등으로 상시화 권장.
- **분리 배치(B)**: 매체는 그대로 Render에 두고 두뇌만 자체 서버 → `ORGANT_SNS_URL`을 Render URL로.
- **Postgres 튜닝**: `settings.py`는 `conn_max_age=600` + `conn_health_checks=True`로 커넥션을 재사용한다.
  ⚠️ **`conn_max_age=0`로 두지 마라** — 요청마다 새 TLS 핸드셰이크가 생겨 전체가 느려진다(이 세션에서
  실제로 겪은 오진). 자체 Postgres가 같은 호스트면 `ssl_require=False`로 두어도 된다.
- **gunicorn**: `--workers 2 --timeout 60 --graceful-timeout 30` — 워커 하나가 막혀도 서비스 지속 +
  먹통 워커 자동 재시작.
- **SPA 캐시**: `config/urls.py`가 `index.html`에 `no-cache` 헤더를 붙인다(콘텐츠-해시 에셋만 캐시).
  이게 없으면 새 배포 후 옛 `index.html`이 삭제된 JS 해시를 가리켜 `about:blank`가 뜬다 — 끄지 마라.

## 4) guide_bridge 엔드포인트 (분리 배치 B에서 러너↔매체 계약)

모두 Bearer `ORGANT_GUIDE_TOKEN` 필요. (`backend/sns/guide_bridge.py`, `urls.py`의 `guide/*`)

| 엔드포인트 | 방향 | 역할 |
|---|---|---|
| `GET  /api/guide/pending/` | 매체→러너 | 처리 대기 요청 큐(폴링) |
| `POST /api/guide/pick/`    | 러너 | 요청 픽(처리중). `{"unpick": true}`로 **재큐**(되돌리기) |
| `POST /api/guide/ingest/`  | 러너→매체 | 봇 출력(메시지/상태)을 채널에 기록 |
| `POST /api/guide/thread/`  | 러너 | 스레드 히스토리 조회 |

- **origin 라우팅**: `HttpSnsGuide`는 요청이 들어온 채널을 `_origin_channel`로 받아
  `create_project_channel`이 **합성 채널 대신 그 채널 id를 반환** → 협업 흐름이 사용자가 보는 채널에
  그대로 표시된다(합성 채널로 새지 않음).
- **네이티브 상태**: 매체는 SYS가 흘리는 디스코드식 상태 텍스트를 채팅으로 보여주지 않는다.
  `views.py`의 `messages` 액션이 상태-plain 메시지를 걸러내고 요청 payload에서 `live_status`를 파생,
  `guide_format.to_native()`가 디스코드 마크업(`<t:..>`/`<@..>`/`[Response] Body:` 등)을 번역한다.
  자체 서버에서도 이 동작은 동일 — 별도 설정 불필요. (배경: [`HANDOFF.md`](./HANDOFF.md) §3)
