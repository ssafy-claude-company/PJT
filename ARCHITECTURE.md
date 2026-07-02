# Organt — 계층 아키텍처

> 캐논: **SYS가 Rule을 강제하고, 외부 주체(User/매체 · Organt)가 *모두 Rule을 통해서만* 통신**한다.
> SYS는 가운데, 양쪽(매체·Organt)은 외부. 이 문서는 그 계층이 코드에서 *물리적으로* 어떻게 분리돼 있는지를 명시한다.

## 4계층 (한 방향 의존: 외부 구현 → Core, 역의존 0)

```
                     ┌───────────────────────────────────────────────┐
                     │  organt_core/   =  SYS + Rule  (강제자·중립)      │
                     │    · sys_core.py   ★SYS★ 흐름·베턴·복구·라우팅     │
                     │    · communication.py  Rule: 단일활성 베턴(LIFO)   │
                     │    · protocol.py       Rule: 메시지 계약           │
                     │    · guide_tools.py    Rule: Organt *도구 계약*     │
                     │    · permissions.py    Rule: Organt *행동 강제*     │
                     │    · audit · config · deploy                     │
                     │    Guide 계약(매체) + 도구/권한 계약(Organt) 정의   │
                     └───┬─────────────┬─────────────┬─────────────────┘
        Rule을 구현·소비   │             │             │  ↑ from organt_core.X (계약만)
       ┌──────────────────┘   ┌─────────┘   ┌─────────┘
┌──────▼────────┐   ┌─────────▼───────┐   ┌─▼──────────────┐
│ organt_runtime/│   │ organt_discord/ │   │ organt_sns/     │
│ = Organt 구현  │   │ = Discord 매체  │   │ = SNS 매체(웹앱) │
│  · organt.py   │   │  · discord_guide│   │  · SnsGuide      │
│   (LLM 런타임) │   │   (DiscordGuide) │   │   /http_sns_guide│
│  · builder.py  │   │  · main(리스너)  │   │  · guide_bridge  │
│  인격:organt/  │   │  · channels      │   │  · runner·frontend│
│  CLAUDE.md     │   │                 │   │                 │
└──────┬────────┘   └────────┬────────┘   └────────┬────────┘
   LLM(외부) ↕            Discord ↕                SNS 웹 ↕
```

## organt_core 내부 — 원래 §7 설계로 해체 (Rule 로직 분리)

잘못된 구현이 `guide_tools`(3096줄) 한 파일에 병합했던 Communication/Task/Project **Rule 로직**을
원래 REWORK_DESIGN §7 설계인 `rule/` 패키지로 되돌렸다. 이제 **도구=얇은 인터페이스, rule=규칙**:

```
organt_core/
├─ rule/                     ← Rule 로직(광역 규칙)
│  ├─ communication.py (1716)  베턴 소통 + 팀·역량 라우팅 + request·vote·meet·parallel_work·recruit 로직
│  ├─ task.py          (1053)  완료·인수 게이트 + TaskRef + create_task·set_goal·complete_task 로직
│  └─ project.py       (297)   배포 신원·적합성 + create_project·deploy·send_file 로직
├─ guide_tools.py      (523)   *얇은* @tool 래퍼 12개(로직은 rule/) + Flow 상태 + build_guide_server + run
├─ _util.py            (39)    공유 표현·디버그 util(_ok·_dbg·_speech_clip·_react·_looks_transient)
├─ tool_names.py       (15)    도구명 상수(organt_runtime 결합 차단)
├─ sys_core.py(SYS) · permissions.py · protocol.py · audit · config · deploy
```
- **12개 도구 전부 `return await _rule_X(flow, …)` 얇은 래퍼** — 규칙은 rule/가 소유.
- 남은 정련(선택): `run`을 organt_runtime으로(등록구조 재설계 필요 — 지금 옮기면 organt_core→organt_runtime 역의존), `Flow`를 상태 모듈로.

## 캐논 매핑 — SYS · Organt · 매체가 각각 어디
| 캐논 개념 | 코드 위치 |
|---|---|
| **SYS** (오케스트레이터·강제자) | `organt_core/sys_core.py` (`class Sys`) |
| **Rule** (추상: 소통·Task·도구계약·강제) | `organt_core/` (communication·protocol·guide_tools·permissions) |
| **Organt** (외부 주체 — LLM 직원) | `organt_runtime/` (organt.py 런타임 + builder) + 인격 `organt/CLAUDE.md` |
| **매체 Guide 구현** | `organt_discord/`(DiscordGuide) · `organt_sns/`(SnsGuide) |

## 핵심 원칙
- **SYS는 Core에 하나** — 매체가 `Sys(guide, …)`로 Guide를, `organt_builder`로 Organt 런타임을 *주입*한다. SYS는 둘 다 계약으로만 안다.
- **Organt은 외부** — SYS가 아니다. `organt_runtime`이 Core의 *도구계약(guide_tools) + 권한(permissions)* 이라는 Rule을 소비할 뿐. 매체 Guide와 *대칭인* 또 하나의 외부 구현.
- **단방향 의존** — `organt_runtime`·`organt_discord`·`organt_sns` 셋 다 `from organt_core.X`로 Core만 의존. **Core는 이 셋을 모른다**(역의존 0).

## 수정 격리 (분리의 목적)
- **LLM 런타임 교체** → `organt_runtime/`만 (Core·매체 무영향)
- **Discord만 / SNS만 수정** → 그 매체 폴더만
- **Rule/협업 로직 수정** → `organt_core/`만 (계약 지키면 외부 셋 무영향)

## 실행
- **SNS 매체**: 웹 Render(`organt-sns.onrender.com`), 러너 `python manage.py run_organt_sns --remote …`(systemd `organt-runner`)
- **Discord 매체**: `python -m organt_discord.main` (`.env` Discord 토큰 필요)

## 현재 상태 / 남은 것
- ✅ **코드 4계층 분리** (한 레포 안, 단방향, 수정 격리) — 완료
- ❌ **git 레포 분리** (다중 레포) — 아직 단일 레포(`ssafy-claude-company/PJT`)
- ❌ **배포 분리** — 러너·웹 둘 다 monorepo 기반
- → 원격 다중레포는 **Render 소스 repo 재연결(대시보드=사용자 작업)**이 낀 협동 단계.
