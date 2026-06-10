# 세션 이어가기 가이드 (SESSION_HANDOFF)

> 이 파일 하나로 새 세션에서 작업을 이어갈 수 있습니다.
> 첫 마디 예: "PJT SESSION_HANDOFF.md 읽고 이어서 작업해."

## 현재 상태
- 브랜치: `claude/exciting-volta-b06xmh` (origin과 일치)
- 테스트: `python -m pytest -q` → 149 통과
- 리스너: 정지 상태. 세션 시작 시 자동 기동은 비활성화됨.
  필요 시 `bash scripts/run_listener.sh`로 수동 실행.

## 자격증명 / 실행 (중요 — 실제로 어떻게 도는가)
- **영속 환경변수**(컨테이너 바뀌어도 유지)에는 봇 토큰이 4개만 있다:
  `SYSTEM_BOT`(라우터) + 워커용 `TEST_BOT_1`·`TEST_OBT_2`·`TEST_OBT_3`.
  → 이대로 `run_listener.sh`를 돌리면 **워커 3명(프론트엔드·디자이너·예비) 기준으로 정상 동작**한다.
  (`CHANNEL_ID`·`DEPLOY_NAME`은 `run_listener.sh`가 기본값을 주입하므로 별도 설정 불필요.)
- **풀팀(워커 ~20명)**을 돌리려면 `ORGANT_BOT_2~20` 토큰이 필요하다. 이 토큰들은 영속 env에
  등록된 적이 없고 과거 `.env`에만 있었다(현재 없음). 풀팀 운영 = 이 토큰들을
  **환경설정(웹 UI)의 환경변수에 등록**해야 컨테이너 교체에도 유지된다.
  로스터(`run_listener.sh`)는 "토큰이 있으면 그만큼, 없으면 건너뜀"으로 자동 적응한다 —
  토큰을 더 등록할수록 워커가 더 붙는다(코드 변경 불필요).
- **LLM 인증**: `ANTHROPIC_API_KEY`는 env에 없다. 별도 호스트에서 돌리려면 주입 필요.


## 시스템 한 줄 요약
Discord에서 여러 봇이 "AI 직원"처럼 협업하는 멀티에이전트 시스템. 사용자가 `[Request] To: @봇`
또는 등록 프로젝트 채널에 평문으로 일을 시키면, 담당(리더) 봇이 팀을 꾸려 Task를 만들고 동료에게
나눠 맡겨 산출물을 만든다. 핵심 가치 = 탈중앙 협업(리더 독식 금지) + 결과 품질.

## 직전 작업: 타임아웃 결함 3건 (검증 대상)
라이브 실행(P-002 게임 개선)에서 드러난 문제를 교정함. 코드+테스트로 재확인할 것.
1. **오래 걸리는 담당자가 중간에 끊김** → 고정 시간이 아니라 "활동이 일정 시간 전혀 없을 때만"
   멈춘 것으로 판정(활동 기반). 작업 중이면 얼마가 걸리든 안 끊음. 구현: `sys_core._run_until_silent`.
   (커밋 `e3a648c`)
2. **끊긴 뒤 정리** → 1번으로 발생 조건 자체가 사라짐(작업 중인 담당자를 안 끊음).
3. **끝났는데 "중단"으로 오판** → 진행분 보존 + 같은 담당자 "이어서" 마무리, 정상 완료 시 상태
   자동 복원. 구현: `guide_tools` request 핸들러 + `complete_task` 게이트.
- 관련 테스트: tests/test_sys.py 의 하트비트/이어가기 테스트 3종.

## 다음 할 일: 수정이 실제로 들었는지 검증
- 코드 읽기: `src/sys_core.py`(_run_until_silent, run_turn) · `src/guide_tools.py`(request·complete_task)
  · `src/organt.py`(_run_once).
- 테스트: `python -m pytest -q` 전체, 또는 `-k "타임아웃 or 하트비트 or 이어가기"`.
- (선택) 라이브 검증: `.env`와 봇 가동이 필요하므로 별도 결정 사항. 하려면 `logs/flow.jsonl`을 보고
  담당자가 안 끊기고 정상 완료(`flow_done`의 `comm_done=true`)로 닫히는지 확인.

## 작업 방식 (권장)
- 코드를 읽고 이해한 뒤 테스트로 검증한다 — 시스템을 블랙박스로 두지 않는다.
- 변경은 작게, 커밋 메시지로 의도를 남긴다.
- 진단이 필요하면 로그·코드·테스트를 직접 확인한다.

## 핵심 파일
- `src/sys_core.py` — 흐름 제어(SYS): 깨우기·단일활성·활동기반 대기·라우팅·레지스트리
- `src/guide_tools.py` — 도구셋(request/recruit/run/create_task/set_goal/complete_task/deploy)
- `src/communication.py` — 단일활성(베턴) 규칙
- `src/organt.py` — Organt(LLM) 본체
- `src/discord_guide.py` — Discord 전송기(채널/스레드/상태블록/역할/닉네임)
- `src/main.py` — 진입점(연결·로스터·이름/직군 복원·on_message·부팅 복구)
- `scripts/run_listener.sh` — 리스너 래퍼(수동 실행)
- `.claude/hooks/session-start.sh` — 의존성 보장(리스너 자동 기동은 비활성화됨)

## 외부 자원 (봇 토큰 외에 살아있는 것)
- **Render(free 플랜)**: 현 배포 타깃 `todo-organt-demo` (https://todo-organt-demo.onrender.com) 외에
  과거 서비스 4개(`todo-list-organt`·`slither-multiplayer-organt`·`slither-multiplayer`·`todo-app`)가
  떠 있음. 안 쓰면 Render 대시보드에서 정리. `RENDER_OWNER=tea-d8ffkd42m8qs73e7vqeg`.
- **GitHub**: deploy가 `github.com/<GH_USER>/<DEPLOY_NAME>`(=thisiscount01/todo-organt-demo) repo에
  force-push로 산출물을 영속한다(`src/deploy.py`). 배포마다 그 repo를 덮어씀.

## 봇 생성 / 토큰 재발급 도구 (로컬 PC 전용)
- `scripts/create_discord_bots.py` + `scripts/start_chrome_debug.bat`. playwright로 '진짜 크롬'(CDP
  `localhost:9222`)에 붙어 개발자 포털에서 봇 생성·토큰 수확(캡차는 사람이 처리). 환경변수
  `CHROME_CDP`·`CHROME_PROFILE`. 결과는 `created_bots.env`(gitignore)로 떨어진다.
  **클라우드 불가 — 로컬에서 실행.** 토큰 재발급/봇 증설 시 사용.

## 리클레임 시 사라지는 것 (주의)
`logs/`(gitignore: `/logs/*`)는 컨테이너 회수 때 통째 사라진다:
- `organt_state_<botid>.json` = 봇별 세션 State(SDK resume) → **복구 불가**. 잃으면 봇이 직전 맥락을
  잊고 새로 시작(치명적이진 않음 — 산출물은 작업공간/배포 repo에 남음).
- `jobs.json` = 직군 기억 → Discord '역할'에서 복원됨.
- `projects.json` = 프로젝트 등록 → 커밋 시드 + 채널 '토픽'에서 복원됨.
- `audit.jsonl`·`flow.jsonl` = 관측 로그 → 잃어도 무방.
- `.env`·`created_bots.env`(비밀)도 gitignore → 영속은 웹 UI 환경변수로 해야 한다.

## Discord 서버
- Guild `1509794645327216640`, 메인 채널 `#test`=`1510828120490643517`.
- 봇 24개. **직군=Discord 역할, 이름=닉네임**이 영속 진실원(리클레임 후 복원 근거).
- 등록 프로젝트: P-001 아이론 배틀 `1513463020649578508` / P-002 던전 탈출 `1513804695896850542`.

## 설계 문서 (별도 레포)
- `ssafy-claude-company/docs` (로컬 `/home/user/docs`): ADR·RFC·Architecture·기획 — 시스템 방향성과
  결정 근거. "왜 이렇게 설계됐나"는 여기 참조.
