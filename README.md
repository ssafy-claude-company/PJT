# PJT — Organt Core

Discord 위에서 사는 AI 직원(**Organt**)들이 협업하는 시스템의 Core 구현체.

## 구조

```
User  ↔  SMS(Discord)  ↔  SYS  ↔  Organt(들)
```

- **SMS** = Discord. 사람과 Organt가 만나는 창구(채널/스레드).
- **SYS** = 얇은 컨트롤러. System 봇(관리자)으로 채널을 감시하다가 User의 `[Request]`가
  오면 담당(리더) Organt를 깨워 흐름을 시작한다. 베턴(단일흐름)·권한·라우팅만 강제하고,
  판단·작업은 Organt(LLM)가 한다.
- **Organt** = 인격을 가진 AI 직원(LLM). `request` 도구로 *필요한 동료 한 명*을 부르고
  (Info=질문 / Work=작업), 파일 도구로 직접 일한다. 반환값이 곧 요청자에게 가는 응답이다.

## 워크플로우 (docs 규약)

1. User가 `[Request]`(To: @담당)를 채널에 올린다 → SYS가 담당(리더)을 깨운다.
2. 리더가 '단순 질문'인지 '실작업 Project'인지 판단한다.
   - Project면 `create_project`(채널) → `create_task`(스레드+상태블록) 후, 자기 몫은 직접
     하고 나머지는 동료에게 `request(Work)`로 위임한다.
3. 깨어난 Organt는 필요한 정보를 동료에게 `request(Info)`로 물어 규격을 맞춘다(P2P).
4. **항상 1명만 활성(베턴)** — 요청 시 보낸 쪽은 자고 받은 쪽이 깬다. 응답은 역순(LIFO)으로
   닫히고, 모든 요청이 닫히면 흐름이 시작점(User)으로 복귀하며 종료된다.
5. 리더의 최종 반환값이 `[Response]`로 User에게 게시된다(= 보고).

## 환경 설정

**사전 준비**
- Python 3.11+
- **Claude 인증** — Organt(LLM)는 Claude Agent SDK(번들 Claude Code CLI)로 호출됩니다.
  `ANTHROPIC_API_KEY` 환경변수 또는 `claude` CLI 로그인(구독)이 있어야 동작합니다.
- Discord 봇 토큰(System 봇 + 워커들) — 봇은 대상 서버에 미리 초대돼 있어야 합니다.

**설치**
```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
```

**`.env` 작성** — `.env.example`를 복사해 채웁니다.

| 변수 | 필수 | 설명 |
|------|:--:|------|
| `SYSTEM_BOT` | ✅ | System 봇(관리자) 토큰 — 채널 감시·상태블록 게시·라우팅 |
| `CHANNEL_ID` | ✅ | User `[Request]`를 받을 Discord 채널 ID |
| `ORGANT_ROSTER` | ✅ | `토큰환경변수명:직군`을 `;`로 나열. **직군엔 도메인만**(백엔드/프론트엔드/디자이너/QA/예비) — '담당자'는 라벨에 박지 않음(담당자는 그 흐름의 To 수신자). 첫 항목이 기본 담당자 |
| `ORGANT_BOT_2…N` | ✅ | 로스터가 참조하는 워커 토큰들(빈 슬롯은 자동 제외) |
| `ORGANT_MODEL` | | 비우면 SDK 기본(Opus). `opus`/`sonnet`/`haiku` |
| `ORGANT_WORKSPACE` | | Organt 작업공간 경로(기본 `../organt_workspace`, repo 밖에 격리) |
| `ORGANT_SKIP_RECOVERY` | | `1`이면 시작 시 이전 미응답 자동 재실행 안 함(수동 테스트용) |
| `DEPLOY_NAME` | | `deploy` 시 고정 서비스명(엉뚱한 새 서비스로 배포 방지) |
| `GH_PAT`·`GH_USER`·`RENDER_KEY`·`RENDER_OWNER` | | `deploy` 도구를 쓸 때만(GitHub push + Render). **gitignore된 .env에만**, 커밋 금지 |

로스터 예:
```
ORGANT_ROSTER=ORGANT_BOT_2:백엔드; ORGANT_BOT_3:프론트엔드; ORGANT_BOT_4:디자이너; ORGANT_BOT_5:QA; ORGANT_BOT_6:예비
ORGANT_BOT_2=...     # 각 토큰
```

> 워커 봇 토큰 대량 생성: `scripts/create_discord_bots.py` (로컬 PC에서 실행 — 클라우드에선 불가).

## 실행

```bash
python -m src.config     # 설정 헬스체크(토큰 값은 출력 안 함)
python -m src.main       # SYS 가동 → #채널에서 User [Request] 대기
pytest -q                # 단위 테스트
```

죽어도 자동 재시작하려면 while-true 래퍼로 감쌉니다(`scripts/run_listener.sh` — 원격 세션에선
`.claude/hooks/session-start.sh`가 세션 시작마다 자동 기동):
```bash
while true; do python -m src.main; echo "재시작…"; sleep 3; done
```

> **리클레임 내구성**: 컨테이너가 회수되면 gitignore된 `logs/`·`.env`가 사라진다. 직군은 Discord
> **역할**, 이름은 **닉네임**, 프로젝트 등록(식별번호·리더·워크스페이스)은 **채널 토픽**에서 부팅 시
> 복원된다(우선순위: 런타임 디스크 > Discord > 커밋 시드 `organt/projects.seed.json`). 봇 토큰만은
> 복원 불가 — 실행환경의 환경변수(또는 `.env` 재작성)로 공급해야 한다.

**사용** — 디스코드 채널에서 `[Request] To: @담당 …` 형식으로 보내면 그 담당(To)이 흐름을 엽니다
(그냥 말 걸면 로스터 첫 항목이 기본 담당자로 받음). 리더의 최종 반환이 `[Response]`로 게시됩니다.

## 모듈

| 모듈 | 역할 |
|------|------|
| `protocol` | Discord 구조화 메시지 계약(`[Request]`/`[Response]`/`[Task-XXX]`) |
| `communication` | 단일흐름 '베턴' Rule(요청 스택·LIFO·busy 가드·상신) |
| `task_rule` | Task 진행 Rule(목표·분배·판정) |
| `discord_guide` | 소통 Rule의 Discord 구현체(전송기) |
| `guide_tools` | Organt 도구셋(`request` / `create_project` / `create_task`) |
| `sys_core` | 얇은 SYS(깨우기·단일흐름 lock·라우팅) |
| `organt` | Organt(LLM) 본체(세션 resume로 State 보존) |
| `permissions` · `audit` | 권한 훅 + 감사 로그(JSONL) |
| `main` | 엔트리포인트 |
