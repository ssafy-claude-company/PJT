# Organt — 3계층 아키텍처 (계층 분리)

> Organt는 **SYS가 Rule을 강제하고, 외부 주체(User/매체·Organt)가 모두 Rule을 통해서만 통신**하는
> 멀티에이전트 협업 시스템이다. 이 문서는 그 계층이 코드에서 어떻게 *물리적으로 분리*돼 있는지를 명시한다.

## 계층 (한 방향 의존: 매체 → Core, 역의존 0)

```
┌──────────────────────────────────────────────────────────────────┐
│  organt_core/   =  SYS + Rule  (매체-중립 두뇌)                       │
│    · sys_core.py       SYS: 흐름·베턴·복구·라우팅                      │
│    · communication.py  Rule: 단일활성 베턴(LIFO) + Engagement 장부     │
│    · protocol.py       Rule: 메시지 계약(Request/Response/Task)        │
│    · guide_tools.py    Rule: Organt 도구계약(request/run/complete…) + Flow│
│    · permissions.py    Rule: Organt 행동 강제(PreToolUse 게이트)        │
│    · organt.py         Organt 런타임(LLM 실행, claude CLI)             │
│    · builder.py        Organt 빌더(매체 공유)                          │
│    · audit·config·deploy                                            │
│    Guide 계약(추상 인터페이스)을 정의 — 매체는 이걸 구현한다.            │
└───────────────┬──────────────────────────────┬────────────────────┘
     Rule의 Guide 구현 (매체별)      │  ↑ from organt_core.X (계약만 의존)
   ┌───────────────────────────┐    │    ┌───────────────────────────┐
   │ organt_discord/            │    │    │ organt_sns/                │
   │  = Discord 매체            │    │    │  = SNS 매체 (웹앱)          │
   │  · discord_guide.py        │    │    │  · backend/ (Django+DRF)   │
   │    (DiscordGuide=Guide구현) │    │    │    sns_guide/http_sns_guide│
   │  · main.py (리스너/진입)    │    │    │    (SnsGuide=Guide구현)     │
   │  · channels.py             │    │    │    guide_bridge · runner    │
   │  진입: python -m           │    │    │  · frontend/ (Vue 3)        │
   │    organt_discord.main     │    │    │  라이브: Render             │
   └───────────────────────────┘    │    └───────────────────────────┘
          Discord ↕                 │              SNS 웹 ↕
```

## 핵심 원칙
- **단방향 의존**: `organt_discord/`·`organt_sns/`가 `from organt_core.X`로 **Core 계약만** 의존한다.
  `organt_core/`는 두 매체를 *모른다*(역의존 0 — `grep organt_discord organt_core/` = 0).
- **대칭**: Discord와 SNS는 같은 `Rule/Guide` 계약의 *두 구현*일 뿐. 한쪽을 고쳐도 다른 쪽·Core 무영향.
- **Organt도 외부**: LLM 직원은 SYS가 아니라 외부 주체 — `guide_tools`(도구계약)+`permissions`(강제)라는
  Rule을 통해서만 행동한다(`organt.py`는 그 런타임). 매체 Guide와 대칭인 또 하나의 Rule 경계.

## 수정 격리 (이 분리의 목적)
- **SNS만 고칠 때**: `organt_sns/`만 건드림 → Core·Discord 무영향.
- **Discord만 고칠 때**: `organt_discord/`만 → Core·SNS 무영향.
- **Rule/협업 로직 고칠 때**: `organt_core/`만 → 계약 지키면 두 매체 무영향.

## 실행
- **SNS 매체**: 웹은 Render(`organt-sns.onrender.com`), 두뇌 러너는 `python manage.py run_organt_sns --remote …`
  (systemd `organt-runner`, `organt_core`를 PYTHONPATH로).
- **Discord 매체**: `python -m organt_discord.main` (`.env`에 Discord 토큰 필요).

## 남은 것 (원격 다중 레포)
현재는 *한 레포 안 명확한 3계층*. 완전한 3-원격-레포(각 레포 독립 배포)는 **Render 소스 repo 재연결**
(대시보드 작업)이 필요해 별도 협동 단계다 — 계약 인터페이스는 이미 이 분리로 안정화됨.
