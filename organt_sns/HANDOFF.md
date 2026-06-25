# Organt SNS — 인수인계 (새 세션 시작점)

> **이 문서부터 읽어라.** Organt SNS의 현재 상태·아키텍처·라이브 가동·다음 작업(자체 서버 이전)을
> 한 곳에 모았다. 세부 실행은 [`SELF_HOST.md`](./SELF_HOST.md), 매체 어댑터 계약은
> [`docs/ADAPTER_MAP.md`](./docs/ADAPTER_MAP.md), 제품 개요·요구사항 매핑은 [`README.md`](./README.md).

마지막 갱신: 2026-06-25 · 작업 브랜치: `claude/fervent-dirac-bsx1w3` (PJT·docs 양쪽 동일)

---

## 0. 30초 요약

- **무엇** — AI 직원(에이전트)들이 한 회사처럼 협업하는 **멀티플레이 소셜 메신저**. 디스코드로 돌던
  Organt 협업 두뇌를, 우리만의 웹 SNS를 **매체(medium)** 로 갈아끼운 것.
- **지금** — Render에 라이브(<https://organt-sns.onrender.com>), Postgres 영속, 실제 인증·소유권·
  친구/초대·개인 워크스페이스까지 동작. 협업 두뇌(러너)를 붙이면 채널에서 진짜로 일이 처리된다.
- **불변 원칙** — **SYS·Agent는 매체-중립이라 건드리지 않는다.** 매체가 디스코드든 우리 SNS든 두뇌엔
  아무 차이가 없다. 매체-특화 코드는 **Guide(어댑터) 하나뿐**. (§2)
- **설계 원칙** — 디스코드 제약은 풀렸다. **디스코드 구현 형태를 흉내내지 말 것**(상태를 채팅으로
  내보내기·이모지 마커·합성 채널·프로토콜 텍스트 노출 금지). SNS의 자율성에 맞는 **네이티브·최대효율
  웹**으로 표현한다. (§3)
- **다음** — **자체 서버 이전(task #19)**. 상시 가동·영속·러너 직접 구동. 절차는 §6 + `SELF_HOST.md`.

---

## 1. 3-tier 아키텍처 (외워라)

```
[ Agent ]   AI 직원 = claude CLI 세션(claude_agent_sdk). 실제 코드를 짜고 명령을 실행.
   ▲  guide 도구(MCP) 호출 ─┐
[ SYS  ]   src/sys_core.py — 중앙 협업 두뇌. 흐름(베턴)·위임·검증·성장. ★매체-중립★
   ▲  guide 객체로 OUTPUT ─┐  ▲ route_channel_request 로 INPUT
[ Guide ]  매체 어댑터. ★유일한 매체-특화 지점★
            · DiscordGuide  ↔ 디스코드
            · SnsGuide / HttpSnsGuide ↔ 우리 SNS   ← 우리가 만든 것
   ▲
[ Medium ] 우리 SNS 웹(Django+DRF + Vue) — 또는 디스코드.
```

- **SYS가 매체에 말하는 법**: `guide.post / send_request / send_response / open_task /
  update_status / create_project_channel / send_file / react / …` (전체 표면은 `docs/ADAPTER_MAP.md` (b))
- **매체가 SYS에 말하는 법**: `Sys.route_channel_request(channel_id, Request(...))` 하나.
- **결론**: 우리가 새로 구현한 건 오직 **Guide의 SNS 구현체 + INPUT 리스너(러너)** 뿐. SYS/Agent
  로직은 한 줄도 안 바꿨다. 앞으로도 바꾸지 마라. 매체에서 부족한 게 보이면 **Guide에서 번역**한다.

### Guide 구현체 두 종류

| | `SnsGuide` | `HttpSnsGuide` |
|---|---|---|
| 위치 | `backend/sns/sns_guide.py` | `backend/sns/http_sns_guide.py` |
| 매체 접속 | **로컬 Django ORM 직접** (같은 DB) | **HTTPS**(`guide_bridge`, Bearer 토큰) |
| 쓰는 때 | 러너와 웹이 같은 호스트·같은 DB | 러너와 웹이 분리(현재 Render 데모) |
| 켜는 법 | `run_organt_sns` (옵션 없이) | `run_organt_sns --remote <URL> --token <tok>` |

---

## 2. 매체-중립 불변식 — 절대 어기지 마라

이 세션에서 가장 많이 교정받은 지점. **SYS를 제거/재작성하거나, 디스코드 오케스트레이션을 SNS로
"짜맞추기"로 끌고 오지 마라.** SYS는 디스코드를 *가정하지 않는다*. 디스코드 흔적처럼 보이는 건
대부분 **Guide/표시 레이어가 디스코드 렌더링을 가정한 마크업**일 뿐이고, 고칠 자리는 거기다.

- ❌ "SNS니까 협업 엔진을 새로 만들자" → SYS는 이미 매체-중립. 새로 만들 필요 없음.
- ❌ "디스코드에 있던 상태요약/이모지/합성채널을 SNS에도 옮기자" → 그게 바로 끌고 오면 안 되는 잔재.
- ✅ "SYS 출력에 디스코드 마크업이 섞여 보인다" → **Guide(`guide_format.to_native`)에서 번역**.

---

## 3. 디스코드 패턴 제거 — 이 세션의 핵심 교정 (재발 방지)

> "디스코드 제약에서 벗어났으니, 디스코드로 구현된 형태가 아니라 **SNS의 자율성에 맞는 최대 효율의
> 웹**으로서 되어야 해." — 사용자

디스코드는 평평한 채팅 + 역할 배지 + 편집되는 상태 메시지밖에 못 그린다. 그래서 SYS는 상태를
*채팅 텍스트*로 흘리고 이모지로 마킹했다. 우리 SNS는 **구조화 상태를 직접 렌더**할 수 있으므로,
그 디스코드식 표현을 **표시 레이어에서 걷어내고 네이티브 상태로 재구성**했다.

| 디스코드식 잔재 | 어디서 새던가 | 네이티브 처리 |
|---|---|---|
| `● 작업 중 …` / `✅ 완료 …` 를 **채팅 메시지로** 게시 | `sender_id==0, msg_type=="plain"` GuideMessage | **표시 안 함**. 상태는 요청 payload(`picked`/`done_ts`)에서 `live_status`로 파생 → 채널 상단 네이티브 라이브-스트립 |
| 완료 체크 ✅ 아이콘 = **이모지 패턴** | 상태 요약 텍스트 파싱 | 이모지 파싱 안 함. 구조화 `state`(`working`/`done`)로 라인 아이콘 렌더 |
| `<t:1782…:R>` 디스코드 상대 타임스탬프 | SYS 상태블록 마크업 | `guide_format.to_native()`가 절대 시:분으로 변환 |
| `[Response]\nBody:` / `[Request]…Body:` 프로토콜 접두 | `protocol.format_*` | `to_native()`가 `_PROTO` 정규식으로 제거 |
| `<@id>` `<#id>` `<:emoji:id>` 멘션/채널/커스텀이모지 | 디스코드 마크업 | `to_native()`가 멘션·채널 제거, 이모지 → `:name:` |
| **합성 채널 id** (`create_project_channel`이 새 id 생성) | 협업이 사용자 안 보이는 채널로 샘 | `HttpSnsGuide._origin_channel` 세팅 시 **요청이 들어온 채널로 라우팅** → 흐름이 사용자 채널에 보임 |
| **회의·표결이 채널에 안 뜸**(리더 혼자처럼 보임) | `_say`(회의/표결)가 합성 `thread_id`로 `post()` 호출인데 `post`만 thread→channel 미해석(send_request/response는 해석함) → 토의가 유령 채널로 샘 | `post()`도 `_thread_channel.get`으로 해석(두 Guide 모두). 프로토콜 라벨 `[회의 NR]`/`[표]`는 `guide_format.collab_kind`로 **네이티브 kind(meeting/vote) 승격** + 접두 제거, 대화 줄에 작은 종류 칩 |

**핵심 파일**: `backend/sns/guide_format.py`(`to_native`), `backend/sns/views.py`의 `messages`
액션(plain 상태메시지 skip + `live_status` 파생 + `to_native` 적용), `frontend/src/pages/Channel.vue`
(구조화 `liveStatus` → 라이브-스트립 배너).

> **새 디스코드 잔재가 보이면** — 채팅으로 새는 상태/이모지/마크업이 있으면, SYS를 고치지 말고
> (a) 표시 메시지면 `views.py messages`에서 분류·skip, (b) 마크업이면 `to_native`에 규칙 추가,
> (c) 라우팅이면 Guide에서 origin 앵커링. 패턴은 이미 §3 표에 다 있다.

---

## 4. 이 세션에 구현한 것 (현재 상태 인벤토리)

전부 라이브 검증·배포 완료. **다시 만들지 마라.**

### 멀티플레이 소셜 / 인증
- **실제 인증** — 회원가입/로그인(비밀번호 해시 `make_password`/`check_password`), 토큰(`Person.token`,
  `Authorization: Token <tok>`), 게스트(둘러보기), 로그아웃. **모든 기능이 로그인 전제.**
  (`backend/sns/social.py`, `frontend/src/{user.js,pages/Auth.vue}`)
- **개인 워크스페이스** — 내가 속한(active) 채널/Organt만 보이는 홈. (`social.py:workspace`)
- **친구 / 초대 = 요청→수락** (강제 추가 아님) — `Friendship.status`(pending/accepted, a=보낸사람
  b=받은사람), `Membership.status`(invited/active). 역방향 pending이면 자동 수락. 요청 작성자는
  **실명** 표시. (`social.py`, `frontend/src/pages/Friends.vue`)

### 소유권 / 프라이버시 / 접근제어
- **채널(Project) 소유권 + 공개/비공개** — `Project.owner`/`visibility`(기본 `public`로 시드는 공개
  유지, 사용자 생성은 `private`). 게스트 생성은 강제 private.
- **나만의 Organt(에이전트) 소유권** — `Agent.owner`/`visibility`. 내 직원 vs 공개 직원 분리, 공유 토글.
- **접근제어** — `social.py`의 `is_member`(active only)/`is_owner`/`can_read`. 목록 쿼리는
  `Q(visibility=public) | Q(members__person=cur, members__status="active")`. (`views.py`)
- 멤버-only 초대, 소유자-only 관리/편집/공유.

### 봇/직원 정체성
- **한국식 3글자 풀네임** 40개 풀(`backend/sns/names.py`), `name_agents` 커맨드가 멱등 배정(직군≠이름).
- 핸들(@고유번호) 유일성, 인격·직무기준·성장 등 상세 정보.

### 라이브 협업(러너) 연결
- **HttpSnsGuide + guide_bridge** — 두뇌(러너)가 HTTPS로 매체에 말한다.
  엔드포인트(`backend/sns/guide_bridge.py`, `urls.py` `guide/*`, Bearer `ORGANT_GUIDE_TOKEN`):
  - `POST guide/ingest/` — 러너 → 매체: GuideMessage 기록(채널에 표시).
  - `GET  guide/pending/` — 매체 → 러너: 처리 대기 요청 큐.
  - `POST guide/pick/` — 요청 픽(처리중 표시). `{"unpick": true}`로 **재큐**(되돌리기) 지원.
  - `POST guide/thread/` — 스레드 히스토리.
- **러너 커맨드** `backend/sns/management/commands/run_organt_sns.py` — pending 폴링 →
  요청 들어온 채널을 `guide._origin_channel`로 세팅 → `SYS.route_channel_request` → 담당 봇이
  실제 작업 → 출력이 ingest로 흘러 채널에 표시. 슈퍼바이저: `run_sns_brain.sh`.
- **네이티브 상태** — §3 참고.

### 영속 / 배포 / 성능
- **Postgres 영속**(Render free) — `settings.py`: `dj_database_url.parse(url, conn_max_age=600,
  conn_health_checks=True, ssl_require=False)`. (⚠️ `conn_max_age=0`는 요청마다 TLS 핸드셰이크로
  느려진다 — 절대 0으로 두지 마라.) `seed_if_empty`로 영속 DB는 시드 안 덮어씀.
- **SPA 캐시(about:blank 방지)** — `config/urls.py` spa_index에 `Cache-Control: no-cache, no-store,
  must-revalidate` + Pragma. 콘텐츠 해시 에셋만 캐시. axios 401 인터셉터 → `/login` 리다이렉트.
- **프로젝트 목록 쿼리** — `Count(distinct)` 다중 어노테이션 크로스조인 폭발 → **상관 서브쿼리**로 교체.
- **append-only 폴링** — 채널 새로고침이 DOM/텍스트선택 보존(복사 가능).
- **gunicorn** — `--workers 2 --timeout 60 --graceful-timeout 30` (`render.yaml`).
- **쇼케이스 정리** — `prune_showcase`가 P-002/P-030/P-031 3개만 유지(멱등, 사용자 채널·직원 보존).

### 데이터 모델 추가분 (마이그레이션 0004–0008)
- 0004 Person + Membership + Friendship
- 0005 Person.password / token / is_guest
- 0006 Agent.owner / Project.owner / Project.visibility
- 0007 Agent.visibility
- 0008 Friendship.status / Membership.status

---

## 5. 라이브로 돌리는 법 (현재 = 분리 배치 B: 매체는 Render, 두뇌는 로컬)

```bash
# 두뇌(claude CLI 있는 호스트)에서:
export ORGANT_PJT=/home/user/PJT
export ORGANT_SNS_URL=https://organt-sns.onrender.com
export ORGANT_GUIDE_TOKEN=<Render env와 동일 토큰>
bash organt_sns/run_sns_brain.sh        # 슈퍼바이저(죽으면 재기동), 로그: organt_sns_state/brain.log
```
- 일회성 테스트: `python backend/manage.py run_organt_sns --remote "$ORGANT_SNS_URL" --token "$ORGANT_GUIDE_TOKEN" --once`
- claude_agent_sdk가 필요하므로 venv는 **`/home/user/PJT/.venv`** 사용(DRF+django+claude_agent_sdk 포함).
  SNS 자체 .venv엔 sdk가 없을 수 있다.
- 러너 상태(작업공간·모델)는 라이브 디스코드 SYS와 **분리**: `organt_sns_state/`, 모델 `claude-sonnet-4-6`.

> 주의: 첫 위임이 느릴 수 있다(에이전트 부팅 ~수분). 채널 상단 라이브-스트립이 "○○ 작업 중"으로
> 도는지로 확인. 샌드박스에서 `curl`을 연타하면 Render 프록시가 레이트리밋한다(브라우저는 정상) —
> 서비스 "먹통" 오진 주의.

---

## 6. 다음 작업 — 자체 서버 이전 (task #19, PENDING)

**목표**: SYS+Agent+SNS+Postgres를 사용자 자체 서버에 올려 **상시 가동 + 영속 + 러너 직접 구동**.
컨테이너 회수가 없어 봇·채널·요청이 안 날아간다. 디스코드로 안정적으로 돌던 것과 동일한 상시성.

**절차는 [`SELF_HOST.md`](./SELF_HOST.md)에 그대로 있다.** 요점:
1. **매체(웹)** — `gunicorn config.wsgi`, `DATABASE_URL`(Postgres) 또는 영속 SQLite, `migrate` +
   `seed_if_empty`, env: `DJANGO_SECRET_KEY`/`ALLOWED_HOSTS`/`ORGANT_GUIDE_TOKEN`.
2. **두뇌(러너)** — `claude` CLI 있는 호스트에서 `run_sns_brain.sh`. `ORGANT_GUIDE_TOKEN`은 매체와 **동일**.
3. **배치 선택**:
   - **A. 한 서버 전부**(웹+러너+DB) → 러너를 `--remote` 없이 **로컬 ORM(`SnsGuide`)** 로 같은 DB 직결
     (HTTP 우회, 더 빠르고 단순). `python manage.py run_organt_sns`.
   - **B. 분리**(현재) → 매체는 Render 등, 두뇌만 자체 호스트. `--remote <URL>`(HttpSnsGuide).
4. **상시화** — systemd/pm2로 웹과 `run_sns_brain.sh`를 데몬화.

**시작점 추천**: 배치 A(한 서버 전부, 로컬 ORM)가 가장 단순하고 빠르다. 사용자 서버 사양/도메인/
Postgres 가용 여부를 먼저 확인하고 `SELF_HOST.md`의 "1) 매체 → 2) 두뇌 → 3) 운영" 순으로 진행.

---

## 7. 파일 지도 (이 세션 변경분 위주)

```
organt_sns/
├─ HANDOFF.md            ← 이 문서(새 세션 시작점)
├─ SELF_HOST.md          ← 자체 서버 이전 실행 가이드(task #19)
├─ README.md             ← 제품 개요·요구사항(F1301–F1305)·ERD·추천 알고리즘
├─ render.yaml · build.sh · run_sns_brain.sh
├─ docs/
│  ├─ ADAPTER_MAP.md     ← Brain↔Medium 어댑터 계약(매체 구현 시 필독)
│  ├─ ARCHITECTURE.md · RULE_SPEC.md · CONVERGENCE_REDESIGN.md
├─ backend/sns/
│  ├─ models.py          Person/Project/Agent/Friendship/Membership(소유권·visibility·status)
│  ├─ social.py          인증·친구·초대·워크스페이스·접근제어(current_person/can_read/is_member)
│  ├─ views.py           visibility-aware 쿼리 + messages 액션(네이티브 상태·to_native) + StatsView
│  ├─ guide_format.py    to_native — 디스코드 마크업·프로토콜 접두 → SNS 네이티브
│  ├─ guide_bridge.py    ingest/pending/pick(+unpick)/thread — 러너 HTTPS 입출구
│  ├─ sns_guide.py       SnsGuide(로컬 ORM Guide)
│  ├─ http_sns_guide.py  HttpSnsGuide(_origin_channel 라우팅)
│  ├─ names.py           한국 3글자 이름 풀
│  └─ management/commands/  run_organt_sns · seed_if_empty · name_agents · prune_showcase · ingest
└─ frontend/src/
   ├─ user.js · api.js   토큰 스토어 · 인증 인터셉터(+401 자동 로그아웃)
   ├─ router.js          전 라우트 인증 가드 + /login 공개
   └─ pages/{Auth,Channel,Friends,Agents,AgentDetail}.vue · components/{NewChannel,SignIn}.vue
```

---

## 8. 운영 제약 / 하지 말 것

- **SYS·Agent 코드 수정 금지** — 매체-중립. 부족하면 Guide에서 번역.(§2)
- **디스코드 구현 형태 복제 금지** — 상태를 채팅으로, 이모지 마커, 합성 채널, 프로토콜 텍스트 노출.(§3)
- **`conn_max_age=0` 금지** — Postgres 매 요청 TLS 핸드셰이크로 느려짐. `600` + health_checks 유지.
- **`ORGANT_GUIDE_TOKEN`** 미설정이면 guide_bridge fail-closed(아무도 못 씀). 매체·러너 동일 값 필수.
- 브랜치 `claude/fervent-dirac-bsx1w3`에만 push. PR은 사용자가 명시 요청할 때만.
- 샌드박스 curl 연타로 인한 Render 레이트리밋을 서비스 장애로 오진하지 말 것(브라우저는 정상).

---

## 9. 라이브 상태 (이 세션 종료 시점)

- 배포: Render `organt-sns` 라이브(`/api/stats/` 200, ~0.7s). Postgres 영속 검증됨(persist_test 계정이
  재배포 후에도 생존).
- 쇼케이스: P-002/P-030/P-031 3개만 유지(나머지 P- 삭제 확인, 예: P-020 → 404).
- 협업 엔진: FPS 요청("1대1 1인칭 fps 게임 만들어줘", ch=31)을 처리 중 — 라이브-스트립 "게임 기획자
  작업 중". 끝까지 돌릴지/흐름만 보고 멈출지는 사용자 판단. (러너는 외부 supervisor로 띄운 것이라
  이 세션이 끝나도 호스트가 살아있으면 계속 돈다. 자체 서버 이전 후엔 systemd로 상시화.)

---

## 10. 검수 백로그 (2026-06-25, 우선순위순) — 새 세션 작업거리

핸드오프 전 3축(서버 안정성·디자인·사용성) 검수에서 나온 항목. **회의 표시 픽스는 검증 완료**
(`sns/tests.py::MeetingVisibilityTest` 통과 — 라우팅 + messages 액션 end-to-end). 아래는 미착수.

### A. 서버 안정성 — 자체 서버 이전 **전에** 권장 (production 안전)
- **[CRIT] DEBUG/SECRET_KEY/ALLOWED_HOSTS** — `config/settings.py:17-22`. 현재 `.env`가 `DEBUG=1`·
  `ALLOWED_HOSTS=*`·SECRET_KEY 미설정(→ 하드코딩 insecure 키 사용). 자체서버는 `DEBUG=0`, 구체
  ALLOWED_HOSTS, SECRET_KEY 미설정 시 **기동 실패**로. `CORS_ALLOW_ALL_ORIGINS=DEBUG` 결합도 분리(`:116`).
  → 상당부분 `SELF_HOST.md` 1)에 이미 명시. 코드 가드만 추가하면 됨.
- **[HIGH] 무한 쿼리** — `views.py:246`(messages가 채널 GuideMessage 전량 로드, 3회 순회), `:183-184`
  (collab도 동일 + 전체 Agent dict). `.order_by("-msg_id")[:limit]`로 캡 + pending/responded는 DB 집계로.
- **[HIGH] AI 작업 무제한 큐잉 = 토큰 폭주** — `views.py:321-347` make_request에 throttle 없음, 게스트
  무제한 생성(`social.py`). DRF throttling(유저·IP) + 게스트 제한.
- **[HIGH] pick 레이스** — `guide_bridge.py:94-105` payload read-modify-write 비원자 → 중복 처리(이중 과금).
  `filter(...).exclude(payload__picked=True).update(...)` 조건부 + `transaction.atomic`.
- **[HIGH] 러너 무타임아웃** — `run_organt_sns.py:172` `route_channel_request`에 `asyncio.wait_for` 없음 →
  멎은 에이전트 1개가 러너 전체 정지. 타임아웃 + 실패 시 unpick.
- **[MED] guide_bridge 입력 한계** — ingest body 무제한·thread limit 무제한(`guide_bridge.py:58-63,120`).
  body 길이 캡, limit clamp + ORM 슬라이스. 토큰 비교 `hmac.compare_digest`(`:32`).
- **[MED] `_thread_channel` 무한 증가** — `http_sns_guide.py:39`/`sns_guide.py:43` open_task마다 적재, 축출 없음.
  장수 러너 메모리 누수. LRU/배치별 clear. (단일 러너 가정도 문서화/강제.)

### B. 사용성 — **운영자→사용자** 기능화 (사용자가 직접 하게)
- **[작업 중지]** — *지금은 SNS에서 멈출 방법 없음.* 채팅 "멈춰"는 Comment만 생성(두뇌 안 봄,
  `views.py:349-363`), "부탁"은 오히려 일 추가(개입/큐잉, `sys_core.py:1674-1687,1854-1875`). 필요한 것:
  사용자 트리거(버튼→세션인증 엔드포인트)→`flow.cancelled` 플래그를 흐름 루프가 협조적으로 체크 +
  inflight `task.cancel()`. 취소 인프라(watchdog)는 있음(`sys_core.py:1073-1093`), 사용자 경로만 없음.
- **[Work/Info 자동분류]** — 지금은 사용자가 토글 수동 선택(기본 Work, `Channel.vue:28,492`). 구조적
  의미는 있음(Info=자문·구현금지 `permissions.py:136-153` / Work=위임·구현). 단일 choke point
  `views.py:333`에서 본문 보고 자동 분류, 토글은 override로. (오분류는 게이트가 흡수 — 저위험.)
- **[에이전트 모델 per-agent]** — 지금 `ORGANT_MODEL` 전역 1개(`run_organt_sns.py:48`→`organt.py:104`).
  Agent에 `model` 필드 없음. 추가(마이그레이션) → 로스터 dict + 에이전트 편집 UI(`AgentViewSet.edit` 존재) →
  `build_options(cfg, model=…)`(이미 override 받음). 일부 Opus 지정 가능해짐.
- **[멎은 요청 재시도]** *(저비용·고가치)* — 러너가 pick 후 죽으면 영영 picked. `unpick` 로직 있고
  (`guide_bridge.py:98`) UI가 멎은 수까지 계산(`views.py:289`)하나 프론트 호출자 없음. 소유자/멤버용
  세션인증 "다시 맡기기" 액션 추가.
- **[엔진 on/off 표시]** — 지금 정적 안내문만. 러너 heartbeat(폴마다 ingest) → 라이브 상태로.

### C. 디자인 — 타임라인 **구조화**(단순 나열 탈피)
- **[회의/표결 블록]** *(최우선)* — 방금 픽스로 회의가 채널에 뜨지만 **N개 개별 버블**로 나열됨
  (`Channel.vue` groups 63-103은 연속 동일발화자만 묶음 → 회의는 발화자가 매번 달라 안 묶임).
  `kind==='meeting'` 연속을 **한 블록**(참여자 스택+주제 헤더, 표결은 집계 푸터)으로. 칩은 블록 헤더로.
- **[수명주기 = 페이즈 구분선]** — 위임/목표/완료/배포가 일반 버블과 동일(`TAG_KINDS`가 meeting/vote만
  칩). day-sep식 중앙 구분선으로 흐름을 페이즈로 분절. 정의돼 있으나 미사용인 kinds.js 색 활용.
- **[지속 페이즈 레일]** — live-strip은 단일 줄·자동만료(`Channel.vue:133-139`). `목표→작업→검증→완료`
  상시 레일 + CollabPanel(토글 뒤 숨음) 요약 1줄 인라인.
- **[목록 섹셔닝]** — Channels(`Channels.vue:49` 하드코딩 정렬, 내/공개 미분리), Friends(5개 평면 리스트),
  AgentDetail 활동피드(raw summary·날짜 그룹 없음). Agents.vue/Recommend.vue가 좋은 템플릿.
