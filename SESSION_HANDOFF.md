# 세션 이어가기 가이드 (SESSION_HANDOFF)

> 이 파일 하나로 새 세션에서 작업을 이어갈 수 있습니다.
> 첫 마디 예: "PJT SESSION_HANDOFF.md 읽고 이어서 작업해."

## 현재 상태
- 브랜치: `claude/exciting-volta-b06xmh` (origin과 일치)
- 테스트: `python -m pytest -q` → 149 통과
- 리스너: 정지 상태. 세션 시작 시 자동 기동은 비활성화됨.
  필요 시 `.env`(비밀값)를 갖춘 뒤 `bash scripts/run_listener.sh`로 수동 실행.
- `.env`는 레포에 없음(gitignore). 봇 가동이 필요할 때만 별도로 마련.

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
