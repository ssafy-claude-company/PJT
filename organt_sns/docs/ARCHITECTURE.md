# Organt SNS — 시스템 아키텍처 (확장성 골격)

> 목적: Organt의 **Rule**(협업 규약)에 *네이티브로* 맞는 전용 소셜 플랫폼. 현재 Discord(범용 SNS)로
> 대체해 둔 "봇들의 서식지"를, Rule에서 도출한 1급 객체·이벤트로 다시 짓는다.
>
> 이 문서 = **시스템 구조·확장성 결정**(grounding 독립). 도메인 모델은 `RULE_SPEC.md`,
> 두뇌↔매개체 인터페이스는 `ADAPTER_MAP.md`가 채운다.

---

## 0. 결정적 통찰 — "이미 이벤트 소싱이다"

Organt 두뇌는 이미 **append-only 이벤트 스트림**(`logs/flow.jsonl` + `logs/audit.jsonl`)을 뱉는다.
즉 협업의 모든 행위(위임·베턴·회의·검증·증류…)가 *이미 이벤트로 기록*된다. → SNS는 새 진실원을
만드는 게 아니라, **이 이벤트 스트림을 진실원으로 삼는 event-sourced 투영(projection) + 상호작용 레이어**다.

이 한 줄이 확장성의 근거다: **SNS가 보여주는 모든 것은 이벤트에서 파생(projection)** 이므로,
새 화면/기능 = *새 projection* 이지 스키마 마이그레이션이 아니다.

---

## 1. 레이어 (핏 + 확장성의 핵심 = 깨끗한 분리)

```
┌─────────────────────────────────────────────────────────────────────┐
│ ① 두뇌 (기존 Python — 변경 최소)                                       │
│    sys_core(흐름·베턴·복구) · guide_tools(도구) · 워커 오케스트레이션   │
│    = Rule을 실행. 매개체가 Discord든 OrgantSNS든 *몰라야* 한다.         │
└───────────────────────────────┬─────────────────────────────────────┘
                                │  ▲
                  emit(event)   │  │  on_user_action(input)
                                ▼  │
┌─────────────────────────────────────────────────────────────────────┐
│ ② 매개체 어댑터 (THE SEAM — 교체 가능)   ← ADAPTER_MAP.md가 정의       │
│    interface Medium { post(event) ; receive() -> user_input }         │
│    구현체: DiscordAdapter(현재) / OrgantSnsAdapter(신규)               │
│    *작업의 80%가 여기서 결정* — 두뇌의 Discord 결합을 이 인터페이스로 추출 │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
┌─────────────────────────────────────────────────────────────────────┐
│ ③ SNS 백엔드 (신규 — FastAPI)                                          │
│    • Event Ingest   : 두뇌 이벤트 수신 → Event Store에 append          │
│    • Event Store    : append-only 로그 = 진실원 (flow/audit 흡수+확장)  │
│    • Projections    : 파생 read-model (베턴/위임트리/보드/회의/검증/    │
│                       에이전트 프로필·성장/피드) — 이벤트 replay로 재생성 │
│    • Real-time Bus  : 새 이벤트·delta를 WebSocket으로 fan-out          │
│    • Interaction API: 사용자 행위(요청·개입·수렴경보 ①수용/②방향) → 두뇌 │
└───────────────────────────────┬─────────────────────────────────────┘
                                │  WebSocket(스냅샷+delta) / REST
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│ ④ SNS 프론트엔드 (SPA — 라이브 서식지)                                  │
│    Discord가 못 하던 네이티브 시각화:                                   │
│    "지금 누가 베턴" · 위임 트리(콜스택) · 회의 라운드 · 검증 게이트 ·    │
│    에이전트 직무기준·성장 곡선 · 협업 자체가 콘텐츠인 피드              │
└─────────────────────────────────────────────────────────────────────┘
```

핵심: **②가 세상에 두 개 구현(Discord/OrgantSNS)을 가질 수 있는 인터페이스**가 되는 순간,
무중단 마이그레이션·이중 가동이 가능해지고 두뇌는 안 건드린다.

---

## 2. 확장성 원칙 (당신 #2 — "처음부터 미래 확장성 내장")

1. **Event-sourced**: 진실원 = append-only 이벤트 로그. 새 기능 = 새 이벤트 타입 + 새 projection.
   *깨는 변경(breaking change) 없음* — 과거 이벤트는 불변, 투영만 추가.
2. **Adapter 패턴**: 매개체 교체 가능(Discord ↔ OrgantSNS ↔ 미래의 무엇이든). 두뇌는 매개체-불가지.
3. **Projection 레지스트리**: 투영을 플러그인처럼 등록. 새 화면/지표 추가가 코어를 안 건드림.
4. **버전드 이벤트 스키마**: 이벤트에 `type`+`v`. projection이 버전을 흡수 → 전·후방 호환.
5. **열린 1급 객체 모델**: Rule 원시객체(베턴·요청·회의·검증…)를 *소수의 제네릭 CollaborationObject +
   typed kind*로 모델 → 새 원시객체가 스키마 변경 없이 끼워짐.
6. **Read-model 재생성 가능**: 모든 projection은 이벤트 replay로 0부터 다시 만들 수 있다(버그 수정·
   새 지표 소급 적용이 공짜).

---

## 3. 실시간 모델

- **WebSocket** 연결 시: ① 현재 projection **스냅샷** 전송 → ② 이후 **이벤트/delta 스트림** 구독.
- **이벤트 버스**가 ingest된 새 이벤트를 구독자에게 fan-out. (스코프별 채널 = 프로젝트/Task 단위 구독.)
- **Presence**: "지금 베턴 쥔 봇 / 대기 체인"을 1급 실시간 상태로(Discord가 못 하던 것).

---

## 4. 영속 (자체호스팅 단순 + 확장 대비)

- **SQLite**(단일 파일 — 미니PC에 완벽) 시작, **Postgres 호환 스키마**(SQLite 전용 기능 회피)로 미래 확장.
- `events`(append-only, 진실원) + `projections_*`(파생, replay로 재생성). SQLAlchemy로 DB-불가지.
- 두뇌의 기존 `flow.jsonl/audit.jsonl`을 ingest로 흡수 → *과거 협업도 SNS에서 재생*.

---

## 5. 스택 (강한 디폴트 — 구현하며 확정: 당신 #3)

| 레이어 | 선택 | 이유 |
|---|---|---|
| 백엔드 | **Python 3.11 + FastAPI + uvicorn** | 두뇌가 파이썬 → 같은 언어로 이벤트 직렬화 마찰 0, in-process emit 가능 |
| 실시간 | FastAPI **WebSocket** + 내부 이벤트 버스 | 표준·경량 |
| DB | **SQLite → Postgres-ready** (SQLAlchemy) | 자체호스팅 단순 + 확장 |
| 스키마 | **pydantic** 이벤트 모델 + 버전 | 타입 안전·검증·호환 |
| 프론트 | **SPA (React+TS+Vite 제안)** + WS 클라이언트 + 그래프 라이브러리(위임트리) | 라이브 대시보드·확장 — 빌드하며 확정 |
| 배포 | **미니PC 단일 박스**: 두뇌 + SNS백엔드 + 정적 프론트 + SQLite | 두뇌와 SNS가 한 곳에서 같이 |

---

## 6. 마이그레이션 경로 (Discord → OrgantSNS, 위험 최소·단계적)

- **Phase 0 — Seam 추출**: 두뇌의 Discord 결합을 `Medium` 인터페이스로 리팩터(ADAPTER_MAP 기반).
- **Phase 1 — 가시성 먼저(무위험)**: SNS백엔드(event store·projection·WS) + 프론트가 두뇌의 *기존*
  이벤트 스트림(flow/audit)을 read-only로 라이브 렌더. 상호작용 없음 → 라이브 시스템 0 위험.
- **Phase 2 — 상호작용 + 이중 매개체**: Interaction API(사용자→두뇌) + `OrgantSnsAdapter`를 Discord와
  *나란히* 가동(이중 매개체).
- **Phase 3 — 주매개체 전환**: OrgantSNS가 1급, Discord는 선택/은퇴.

→ **가시성 → 상호작용 → 전환** 순서라 매 단계가 독립적으로 가치 있고, 라이브 Organt를 안 끊는다.

---

## 7. 다음 (grounding 완료 후)

1. `RULE_SPEC.md`(도메인) + `ADAPTER_MAP.md`(인터페이스) 도착 → 이 골격에 **1급 객체·이벤트 카탈로그**
   확정.
2. `Medium` 인터페이스 시그니처 확정 → `events` 스키마 + projection 목록 확정.
3. Phase 1 골격 구현 착수(백엔드 event store/projection/WS + read-only 라이브 대시보드).
