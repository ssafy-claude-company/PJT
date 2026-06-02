# Organt Core — 설계 정정 & 재구현 설계서

> 근거: `ssafy-claude-company/docs` 전수 정독 (Architecture/Core, RFC-002, Communication/Request/Response, Project/Task, Guide/Discord, RFC-004, 기획/Iter 1).
> 사유: 초기 구현은 핸드오프 요약만 보고 진행 → 아래의 구조적 오류 발생. docs + Iter1 기준으로 근본 재정렬한다.

---

## 1. 올바른 개념 모델 (docs 근거)

**계층:** `User ↔ SMS ↔ SYS ↔ Organt` — *모든 흐름은 SYS가 제어* (Architecture/Core.md)

- **SMS** = 사람-시스템 창구. Discord. 입력을 받고 Organt 작업을 가시화. **봇은 I/O 인터페이스일 뿐.**
- **SYS** = 흐름 제어자.
  - **Rule** (추상 규칙): 소통(Communication), 파일 저장, + 작업단위(Project/Task).
  - **Guide** (Rule의 외부 구현체): Discord 소통, 로컬 파일 저장.
  - 권한 관리 / 흐름 모니터링도 SYS (Feature.md).
- **Organt** = Agent + 인격: Skill / Hook(권한 차단) / Agent(메인 수신·서브 위임) / State / CLAUDE.md.
- **Tool** = FileSystem, Discord. 주체는 Tool을 **Guide대로만** 사용 (RFC-002).

**결정적 구분:** **SYS ≠ system bot.** system bot은 SYS가 쓰는 *Discord Guide(전송기)* 다. SYS는 추상 Rule을 들고 흐름을 제어하고, Guide(=봇)는 그걸 Discord로 실어 나를 뿐.

---

## 2. Rule (추상)

### Communication (Communication/Request/Response.md)
- 흐름은 **위(User)에서 SMS를 통해 시작**된다. Organt는 스스로 시작하지 않는다.
- 살아있는 Organt가 **Request** → 보낸 쪽 멈춤, 받은 쪽 살아남. **한 번에 1명만 활성.**
- Kind가 **Work**인 Request는 미완 Work 보유 Organt에 금지(겹침·순환 방지).
- **Response**로 Request가 닫히고 보낸 쪽이 다시 살아남. **역순(LIFO)으로 닫힘.**
- Work Response → 보낸 쪽이 **Accept/Redo** 판정. Redo 한계 초과 → 위로 상신.
- 모든 Request 닫히면 흐름은 **시작점으로 복귀 후 종료.**
- Request 속성: From, To, **Kind(Work|Info)**, Body. Response 속성: From, To, RepliesTo, Body.

### Task (Task.md)
- Task = Goal 완수를 위한 작업 단위. 속성: **Purpose**(문제, 시작시 부여), **Goal**(측정가능, Team이 Purpose로 정함, Leader가 완수 판정), **Team**, **Leader**(drive, 의사결정, Work보다 Leading).
- Flow: ①생성(Leader·Purpose 부여) ②Leader가 Team 모집 ③Goal 확정 ④Todo 생성(Leader 관리) ⑤Leader가 Team에 분배 ⑥완료시 Leader 확인 ⑦Goal 완수판정(미달시 ④로) ⑧Leader가 성과 정리·보고.

### Project (Project.md)
- Project = 도메인 단위 공간, Task들로 점진 진행. 속성: Leader, **Workspace**, **Context**(맥락·핵심결정), **Archive**(spec·이전 Task 기록).

---

## 3. Guide — Discord (Discord.md)

- Organt 1봇, System 1봇(관리자).
- **Project = 1 Channel** (참여 Organt만, Leader가 초대).
- **Task = 1 Thread.** `/Task`로 **System Bot이 생성.**
- **Task 상태블록** (System Bot이 수시 갱신) — **채널 레벨**:
  ```
  [Task-XXX]
  Purpose: ---
  Status: ---
  Goal: ---
  Group:
  - @XXX: 봇 정보
  (if end)
  - result: ---
  ```
- **하나의 Communication 흐름 = 하나의 Task = 하나의 Thread 안에서** 일어난다.
- Thread 안에서 Organt들이 **구조화 메시지**로 Request/Response 교환:
  ```
  [Request]                 [Response]
  To: @XXX                  Body: ---
  Kind: Work|Info
  Body: ---
  ```
- Discord가 주는 정보는 블록에 안 씀: **From=보낸 봇, RepliesTo=답글(reply), 식별=메시지 ID.**
- 사람도 읽고 System Bot도 파싱한다. Work Response의 Accept/Redo도 Thread 메시지로 남음.

**핵심 배치:** **Thread = 작업 대화(Request/Response). 채널 = [Task-XXX] 상태블록.**

---

## 4. 통합 end-to-end 흐름 (정합)

1. **User**가 Project 채널에서 Task 요청(또는 `/Task`) → **SMS(Discord)** → **SYS**.
2. SYS: Task 생성 → System Bot이 **채널에 [Task-XXX] 상태블록** + **Thread** 생성. Leader·Purpose 부여.
3. SYS가 **Leader Organt를 깨움**(Communication 시작: Thread 내 첫 Request).
4. Leader가 Task flow 구동: Team 모집 → Goal 확정 → Todo → **Work Request로 분배**(Thread 내 베턴) → 완료 확인 → Goal 판정(미달시 loop) → 보고.
5. 매 Request/Response = **Thread 내 구조화 메시지**. SYS가 베턴(단일 활성·역순 close·busy·redo·상신) enforce.
6. System Bot이 **채널 [Task-XXX]** 의 Status/Goal/Group/result를 단계마다 갱신.
7. Task 종료 → result 상태블록 반영 + **Archive 기록**(FileGuide).

---

## 5. Iter 1 과의 관계 (기획/Iter 1.md)

Iter1 기능 = SMS 연결/가이드 · FileSystem 가이드 · **Sys 설계 및 Organt 연결** · 대화구조(Rule) 설계/가이드 · 작업구조(Rule) 설계/가이드 · log · Organt 설계.
**KPI** = Organt에게 **"ToDo앱 제작"** Task 부여 후 결과(코드/기획/업무) 점수.

→ 즉 Iter1의 척추는 **SYS + Communication Rule/Guide + Task Rule/Guide** 인데, 현재 구현엔 **SYS 추상이 없고** 포맷·통합이 어긋나 KPI(ToDo앱 Task flow)를 태울 수 없다.

---

## 6. 현재 코드 전수 진단

| 모듈 | 현재 | 진단 (docs 대비) | 조치 |
|---|---|---|---|
| `gateway.py` | 봇2개 + on_message 수집·라우팅 | SMS 전송과 SYS 흐름제어 **혼재** | DiscordGuide(전송) ↔ SYS(흐름) 분리 |
| `router.py` | mention→route 판정 | Communication Rule이 아닌 단순 멘션체크 | SYS의 "흐름 시작 판정"으로 흡수 |
| `app.py` | 전체 조립(사실상 SYS인데 Discord에 결박) | **SYS 추상 부재**, 자유텍스트 프롬프트 | `sys_core`로 재설계 |
| `communication.py` | Req/Resp 인코딩 + 베턴 | 베턴 로직은 **양호**, **포맷 오류**(`[REQ:work]`) | 포맷 `protocol`로 분리·교정, 베턴은 Rule로 |
| `orchestrator.py` | CommGateway(베턴+전송) | Task와 **분리**됨, 포맷 오류 | SYS로 흡수, **Thread 내** 구동 |
| `task.py` | TaskBoard lifecycle | Purpose/Goal(측정)/Team/판정loop **부족**, 블록 포맷 상이 | docs Task flow대로 재작성 |
| `task_gateway.py` | Thread 생성 + 상태판을 **Thread 안**에 | **위치 오류**(상태블록=채널이어야), 포맷 상이 | 상태블록=채널, Thread=대화 |
| `organt.py` | Organt LLM 본체 + State | 본체는 양호, **자유텍스트 발화** | **구조화([Request]/[Response]) 발화**, SYS가 깨움 |
| `discord_tools.py` | read/send/reply MCP + DiscordIO | 전송 OK, 비구조화 | DiscordGuide로 편입, 구조화 송수신 |
| `permissions.py` | PreToolUse allow/deny + 경로 | OK | SYS 권한관리/Organt.Hook로 명시 |
| `audit.py` | JSONL + PostToolUse | OK (log) | SYS 흐름 모니터링과 연계 |
| `archive.py` | Context/Archive 파일 | OK | FileGuide로 정리 |
| `channels.py` | 길드→채널 해석 | OK(CHANNEL_ID 우회) | DiscordGuide util |
| `config.py` | env/workspace/log | OK | Project.Workspace 개념 반영 |

**근본 원인 3가지:** (a) SYS 추상의 부재(=Discord 전송과 흐름제어 결박), (b) Discord 구조화 프로토콜 미준수, (c) Communication↔Task↔Project 미통합(별개 데모).

---

## 7. 목표 모듈 구조 (재구현)

```
src/
├─ protocol.py          # 구조화 메시지: Request/Response/TaskStatus 포맷·파싱 (Guide 계약)
├─ rule/
│  ├─ communication.py  # 베턴 Rule (현 CommunicationManager 정리)
│  ├─ task.py           # Task Rule (Purpose/Goal/Team/Leader, flow 8단계)
│  └─ project.py        # Project Rule (Channel/Workspace/Context/Archive)
├─ guide/
│  ├─ discord_guide.py  # Discord 전송기: 봇 관리, 채널/스레드/상태블록, 구조화 송수신
│  └─ file_guide.py     # 로컬 파일 저장 (Workspace/Context/Archive)
├─ sys_core.py          # SYS: Rule들 + Guide들 조율, 권한, 흐름 모니터링
├─ organt.py            # Organt: 구조화로 발화, SYS가 깨움 (인격/State/Hook/Agent)
├─ permissions.py       # SYS 권한관리 (PreToolUse)
├─ audit.py             # log (흐름 모니터링)
├─ config.py            # 설정
└─ main.py              # 런타임 진입 (SYS 구동)
```

(기존 gateway/router/orchestrator/task_gateway/discord_tools는 위로 흡수·재배치)

---

## 8. 재구현 순서 — 각 단계 **실제 Discord 실검증**

1. **protocol.py** — `[Request]/[Response]/[Task-XXX]` 포맷·파싱, Kind(Work/Info). (단위 테스트)
2. **DiscordGuide** — 실제 채널/스레드 생성, 상태블록(채널) 게시·갱신, 구조화 송수신. (실검증: 실제 Thread + 채널 상태블록)
3. **CommunicationRule** — 베턴 정리 + protocol 연동. (단위 테스트)
4. **TaskRule** — docs 8단계 flow. (단위 테스트)
5. **SYS** — Rule+Guide 조율: User 입력→Task 생성→Leader 깨움→베턴 구동→상태블록 갱신. (실검증)
6. **Organt 구조화 발화** 통합 — Organt가 [Request]/[Response]로 말함.
7. **end-to-end 실검증** — 실제 Thread 안 구조화 Request/Response 왕복 + 채널 [Task-XXX] 갱신, **"ToDo앱 제작" Task flow** (Iter1 KPI 시나리오)대로.

> 각 단계는 docs의 해당 규칙을 기준으로 실측 검증한다. 검증 없는 진행 금지.
