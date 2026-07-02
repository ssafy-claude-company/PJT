# Organt / murmur — 계층 아키텍처

> **캐논**: `User ⇄ 매체(murmur/Discord) ⇄ SYS ⇄ Organt`. SYS가 **추상 Rule**을 들고 흐름을 강제하고,
> 외부 주체(매체·Organt)는 **Rule의 구현체**를 통해서만 통신한다. **추상 ↔ 구현체**가 이 설계의 축이다.

## 4계층 — 의존은 전부 SYS(코어)를 향함 (역의존 0)
```
                         system/  =  SYS + Rule (추상)     ← 아무것도 의존 안 함
                         ▲            ▲            ▲
          ┌──────────────┘            │            └──────────────┐
    organt/ = Organt(봇)        guide/ = Guide(구현체)       organt_sns/ = murmur(SNS 플랫폼)
    · organt.py(LLM 런타임)     · discord_guide(→Discord)    · Django 웹·DB·guide_bridge API
    · builder.py                · http_sns_guide(→murmur)    · SnsGuide(ORM 구현체)·frontend
    · CLAUDE.md(인격)           · channels                    · 독립 서비스(Discord 같은 존재)
       → SYS                       → SYS (봇도 매체서비스도 모름)   Guide가 이 API로 대화
```
- **SYS는 구현체를 import 안 함** — `Sys(guide, builder)`로 *주입*받음. rule/도 매체 import 0.
- **Guide·Organt는 형제** — 둘 다 SYS에만 의존. Guide는 봇을 모르고, Organt는 전송기를 모름.
- **murmur = 독립 플랫폼** — Guide(HttpSnsGuide)가 그 API(guide_bridge)로 대화. 아무도 murmur를 의존 안 함.

## system 내부 — Rule을 §7 설계대로 (guide_tools 3096→417 해체)
```
system/
├─ sys_core.py     ★SYS★ 오케스트레이션 + SYS.run(매체 무관 실행 루프)
├─ rule/           Rule 로직(추상)
│  ├─ communication.py  베턴 소통 + 팀·역량 라우팅 + request·vote·meet·parallel_work·recruit
│  ├─ task.py           완료·인수 게이트 + TaskRef + create_task·set_goal·complete_task
│  └─ project.py        배포 신원·적합성 + create_project·deploy·send_file
├─ guide_tools.py  얇은 @tool 래퍼 12개 + build_guide_server + run(실행도구)
├─ flow.py         Flow 공유 상태  · permissions.py  Hook  · protocol.py  메시지계약
├─ deploy.py · audit.py · config.py · _util.py · tool_names.py
```
12개 도구는 전부 `return await _rule_X(flow, …)` 얇은 래퍼 — **규칙은 rule/가 소유**.

## 두 가지 계약 (추상 ↔ 구현체)
**① Guide 전송 계약** — post·send_request·read_thread·open_task… : Rule이 이걸로 매체에 발화.
**② Guide 배달 계약** — SYS.run이 쓰는 실행 인터페이스:
```
Guide.get_pending() · pick(done/touch/unpick) · heartbeat() · check_stop/all_stops/mark_stopped
     · check_interject() · set_origin()
   구현체: HttpSnsGuide(HTTP→guide_bridge) · SnsGuide(ORM) · DiscordGuide(예정: on_message 큐)
```

## 실행 모델 — SYS.run (매체 무관)
```python
class Sys:
    async def run(self, guide, leader, cap, poll, stall_timeout, max_age):
        # "무엇"(정체판단·재개·컷·spawn)은 SYS가 *결정*, "어떻게"(배달)는 guide가 *구현*
        async loop: get_pending → pick → set_origin → route_channel_request  (+ heartbeat·정체컷·재개)
# 진입(러너/리스너) = guide·builder 조립 후 sysm.run(...) 호출 — 얇음
```
- **결정(신뢰성: 하트비트·정체컷·재개) = SYS** / **실행(pick·heartbeat write) = Guide**.
- SNS 러너는 이 SYS.run을 씀(라이브 검증). Discord 리스너(main.py)는 아직 자체 on_message — 이행 예정.

## 상태 (라이브 무중단, ~40커밋)
- ✅ guide_tools 해체(rule/) · 4계층+의존방향 · Guide 통합+배달계약 · SYS.run(SNS) · 산출물 레포화
- ⏭ **Discord 진입 이행**: DiscordGuide 배달계약(on_message→큐) + SYS.run — 라이브 Discord 환경서 검증 필요
- ⏭ **다중 레포**: 4계층이 그대로 4레포(organt-core/organt/guide/murmur) — 배포(Render/systemd) 재배선 낀 인프라 단계
