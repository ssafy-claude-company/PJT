"""Organt Core 런타임 패키지.

구조: User ↔ SMS(Discord) ↔ SYS ↔ Organt.
- protocol: Discord 구조화 메시지 계약([Request]/[Response]/[Task-XXX]).
- communication: 단일흐름 '베턴' Rule(요청 스택·LIFO 응답·busy 가드·상신).
- discord_guide: 소통 Rule의 Discord 구현체(전송기).
- guide_tools: Organt 도구셋(request·recruit·run + 리더 create_project·create_task·set_goal·
  complete_task·deploy·vote·meet·parallel_work) + 협업·품질 게이트. Task 상태도 여기 정의.
- sys_core: SYS(깨우기·단일흐름 lock·라우팅·흐름 수명·복구·배포).
- organt: Organt(LLM) 본체(세션 resume로 State 보존).
- permissions / audit: 권한 훅 + 감사 로그.
- main: 엔트리포인트(System 봇으로 채널 감시 → 팀 가동).
"""
