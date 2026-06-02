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

## 실행

`.env`(또는 환경변수)에 봇 토큰·채널을 설정한다 (`.env.example` 참고):

```bash
SYSTEM_BOT=...            # System 봇(관리자) 토큰
CHANNEL_ID=...            # User 입력을 받을 채널 ID
# Organt 로스터: "토큰_환경변수명:역할" 쉼표 구분, 첫 항목이 리더
ORGANT_ROSTER=TEST_BOT_1:담당자,TEST_OBT_2:프론트엔드,TEST_OBT_3:디자인
```

```bash
python -m src.config     # 설정 헬스체크(토큰 값은 출력 안 함)
python -m src.main       # SYS 가동 → #채널에서 User [Request] 대기
pytest -q                # 단위 테스트
```

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
