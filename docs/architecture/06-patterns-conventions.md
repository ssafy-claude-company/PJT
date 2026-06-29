# 06 · 패턴 · 컨벤션 평가 — ✅ 좋음 / ⚠️ 개선 / 🏗️ 체계필요

이 문서는 리팩토링의 직접 입력이다. 각 항목은 `file:line` 근거와 **왜 그런가**의 논리를 단다. 분류:

- ✅ **좋음** — 유지·확산할 패턴
- ⚠️ **개선** — 안티패턴·버그유발·결합·중복 (고쳐야 함)
- 🏗️ **체계필요** — 동작하나 체계(추상화·타입·계약·분류)가 없어 흔들리는 것

> **검증 상태**: spine 모듈(`sys_core`·`communication`·`protocol`·`organt`·`main`·`permissions`·`audit`·`config`)은 전수 정독으로, `guide_tools`·`discord_guide`·`channels`·`deploy`는 병렬 분석 + 핵심 인용 직접 spot-검증으로 근거를 확인했다. 모든 `file:line`은 [ref/REFERENCES.md](ref/REFERENCES.md)에 색인된다. 적대적 검증(인용 정확성 + 주장 방어가능성) 요약은 §6.4.

---

## 6.1 ✅ 좋음 — 유지·확산

| # | 패턴 | 왜 좋은가 | 근거 |
|---|------|-----------|------|
| G1 | **순수 규칙 코어** — 베턴 로직이 네트워크/IO 없이 분리, 불변식이 단위 테스트됨 | 가장 위험한 동시성·순서 규칙을 결정론적으로 검증 가능. 부수효과 없는 재사용 | `communication.py:1`, `channels.py:16`, `protocol.py:100` |
| G2 | **단일 메시지 계약** — `Kind`/`Request`/`Response`/`TaskStatus` + 인코딩/파싱이 한 모듈에 dataclass로 응집 | 와이어 포맷 변경 지점이 하나. `Kind(str,Enum)`로 문자열·enum 비교 자연스러움 | `protocol.py:17-78` |
| G3 | **Default-deny + Deny-with-redirect** — 미허용 도구는 차단하되 "대신 이걸 써라"로 유도 | 봇이 본능적으로 집는 Bash 등을 *받아넘겨* 올바른 도구로 복귀시킴(프롬프트 강화보다 실효) | `permissions.py:103`, `:106` |
| G4 | **심층방어 비밀 처리** — run 셸 env-scrub + 비루트 강등, deploy는 키를 파라미터로(서브프로세스 아님) | 봇이 배포는 하되 키는 못 읽음. 권한 자체로 비밀 파일/프로세스 읽기 차단 | `guide_tools.py:82`, `deploy.py:245` |
| G5 | **크래시-세이프 영속** — 원자적 쓰기(tmp+fsync+replace), 전이마다 체크포인트, SIGTERM flush | 컨테이너 회수가 잦은 환경에서 '반쪽 JSON'·미체크포인트 유실 방지 | `sys_core.py:148`, `:553`, `main.py:402` |
| G6 | **리클레임 내구성(3계층)** — 디스크 > 채널 토픽 > 시드, reconcile로 토픽이 시드를 이김 | 디스크가 사라져도 Discord에서 등록·리더·직군 복원 | `sys_core.py:320` |
| G7 | **활동 기반 워치독** — 고정 타임아웃이 아니라 '무활동' 기준으로만 끊음 | 오래 걸리는 정상 빌드를 안 자르고 진짜 행만 해소(좀비·미완의 근본 교정) | `sys_core.py:1540`, `:1156` |
| G8 | **결정론적 스테일 세션 판정** — 에러 텍스트가 아니라 세션 파일 존재로 | CLI 메시지 변화에 안 흔들림, 'No conversation found' 영구 헛돌이 차단 | `organt.py:173` |
| G9 | **실패 vs 빈값 구분** — `get_guild_bot_nicks` None/{}, 불리언 env 함정 명시 방어 | 조회 실패를 '전원 무명'으로 오인한 전면 개명 사고 차단 | `discord_guide.py:360`, `main.py:533` |
| G10 | **협업의 구조적 강제** — 협의→합의→위임→구현 순서, 독식·흡수·대리구현을 *훅*으로 차단 | 프롬프트 의존을 점차 구조로 대체(LLM이 잊어도 강제됨) | `permissions.py:142-443`, `guide_tools.py:2060` |
| G11 | **배포 실 검증** — 바이트 단위 stale-serving 검사 후에만 성공 선언 + 비-일시 에러 분류로 무한재시도 차단 | '거짓 성공'이 아니라 진짜 라이브 URL 확인. deploy는 *에러 분류 체계*를 갖춘 모범 사례 | `deploy.py:102`, `:191` |
| G12 | **봇별 연결 격리** — 한 토큰 오류가 리스너 전체를 안 죽임 | 예비 토큰 점진 확장의 안전장치 | `main.py:309` |

> **메타 관찰(좋은 방향성)**: 코드 전반에 "프롬프트로 유도하던 것을 구조(게이트·자동 이어가기/위임/조율)로 옮긴" 진화가 보인다(`sys_core.py:1354-1538`, `permissions.py`). 이는 LLM 비결정성을 다루는 올바른 방향이며, 리팩토링은 이 진화를 *체계화*(아래)하는 방향이어야 한다.

---

## 6.2 ⚠️ 개선 — 고쳐야 할 것

### B1 · God-function / God-object 〔high〕
한 함수/클래스에 과도한 책임이 응집돼 변경·추적이 어렵다.

| 대상 | 규모 | 근거 |
|------|------|------|
| `make_guide_tools` | ~2200줄(중첩 도구 정의) | `guide_tools.py:791` |
| `request()` 도구 | ~590줄 단일 핸들러 | `guide_tools.py:805` |
| `Sys` 클래스 | 2334줄(라우팅·영속·복구·배포·프롬프트·증류) | `sys_core.py:37` |
| `run()` | ~480줄 + 공유 가변상태 클로저 다수 | `main.py:282` |
| PreToolUse 훅 | ~410줄, 10개 정책 분기 | `permissions.py:90` |
| `deploy_sync` | ~120줄 다책임 | `deploy.py:245` |

**왜**: 테스트·리뷰·변경의 단위가 함수 전체가 돼 회귀 위험이 크다. → [07](07-refactoring-targets.md) P0/P1.

### B2 · 데드코드 〔med〕
- `permissions.py:310-409` — `if False and …`로 비활성화된 옛 키워드 게이트 ~100줄 + 데이터 테이블. 주석도 "데드코드는 후속 정리"라 명시. `[R-B2-1]`
- `organt_allowed_tools`(`permissions.py:9`) — 실제 허용목록과 **다이버전스**한 데드 함수. `[R-B2-2]`
- `build_options` 기본값(`organt.py:109`) — `max_turns=16`·`allowed_tools=[…,"Bash"]`가 실제 호출부에서 전부 override돼 *오해를 주는* 죽은 기본값. `[R-B2-3]`
- `channels.py`(`:40`) — 프로덕션 미사용(테스트만 import). `[R-B2-4]`

### B3 · 마커/매직 문자열에 의존하는 제어 흐름 〔high〕
- 게이트 제어가 **LLM이 정확한 마커 문자열을 출력**하는 데 의존(`[직군밖]` 등 정규식 매칭). `guide_tools.py:1178` `[R-B3-1]`
- Task 상태·완료를 **한국어 리터럴**로 비교/대입(`status != "완료"`, `respond` result `"accept"`, deny 사유 자유 텍스트). `guide_tools.py:1697`, `communication.py:205`, `permissions.py:105`
**왜**: 출력 문구·상태 어휘가 코드 곳곳에 흩어져, 모델 출력 드리프트·오타에 취약하고 리팩토링이 위험.

### B4 · 침묵하는 실패 (광범위 `except Exception: pass`) 〔med〕
영속·IO·기록 실패가 신호 없이 삼켜진다 — `guide_tools.py:1475`, `permissions.py:495`, `main.py:359`, `deploy.py:50`(매직 status 0 반환). 의도(리스너를 안 죽임)는 타당하나 **무엇이 실패했는지 관측 신호가 없다**. `[R-B4-1]`

### B5 · 보안 민감 누수/갭 〔med〕
- **getattr fail-OPEN**: 게이트 술어가 `getattr(flow,"…",None)`로 읽어, 필드 rename 시 조용히 *통과*(보안 게이트가 무력화). `permissions.py:160` `[R-B5-1]`
- **PAT 유출**: deploy 에러 문자열에 토큰 박힌 remote URL이 실릴 수 있음. `deploy.py:280` `[R-B5-2]`
- **감사에 파일 내용 전체 기록**: PostToolUse가 `tool_input`(Write 내용 포함)을 그대로 audit에 남김. `audit.py:45` `[R-B5-3]`
- **run 미-confinement**: 작업공간 경계 검사가 Write/Edit에만 적용되고 run엔 없음(별도 deny-list·강등에 의존). `permissions.py:110` `[R-B5-4]`

### B6 · 위험 로직이 조용히 붕괴 〔high〕
- **orphan-reaper 무음 붕괴**: Render 고아 서비스 수거의 안전 가드가 읽기 실패 시 조용히 무너져 *엉뚱한 서비스 수거* 위험. `deploy.py:153` `[R-B6-1]`
- **restore_chain 점유 누락**: 끊긴 체인 복원이 스택만 세우고 참여자를 `engage`하지 않아 전역 점유 불변식에 갭(직후 `attach_engagement` 호출에 의존). `communication.py:340` `[R-B6-2]`

### B7 · DRY 위반 〔low~med〕
- 좀비/파킹 복구 가드가 **3곳 중복**(`main.py:566`·`:149`·`sys_core.py` 복원). `[R-B7-1]`
- 원자적-쓰기 패턴 3벌(`sys_core.py:148`·`:181`·`:775`). `[R-B7-2]`
- `·` 직군 라벨 구분자가 `discord_guide`·`guide_tools`에 주석 결합으로 중복(`discord_guide.py:320`). `[R-B7-3]`
- `config.ROOT` 함수-로컬 재import 반복(`main.py:378`). `[R-B7-4]`

### B8 · 일관성 결여 — Kind 파싱 3종 〔med〕
같은 'Work 판정'이 세 곳에서 다르게 구현된다: `startswith("w")`(`guide_tools.py:809`) vs `startswith("work")`(`protocol.py:126`) vs 소문자 동등(`permissions.py:40`). `protocol`은 비-work를 **조용히 INFO로 기본**(`:126`)해, 오타 kind가 의도와 다르게 흐른다. `[R-B8-1]`

---

## 6.3 🏗️ 체계필요 — 구조를 세워야 할 곳

### S1 · `Flow`/`TaskRef` 타입 계약 부재 〔high〕
`Flow`(`guide_tools.py:590`)·`TaskRef`(`:541`)가 **덕타이핑 god-object**다: SYS가 `wake`·`checkpoint_task`·`persist_role` 등 콜러블을 런타임에 주입(`sys_core.py:1874-1880`), 훅·도구가 `getattr(flow,"…",default)`로 ~40개 옵셔널 필드를 방어적으로 읽는다(`guide_tools.py:619`). **계약이 타입으로 표현되지 않아** rename·오타가 런타임에야(또는 §B5처럼 조용히) 드러난다. → 명시적 인터페이스(Protocol/dataclass + 주입 포트). `[R-S1-1]`

### S2 · 에러/상태 분류 체계 부재 〔high〕
시스템 전반이 **문자열 부분일치**로 분기한다:
- 일시/스테일 에러 판정 = 매직 문자열 grab-bag(`organt.py:85`)
- deny 사유 = 자유 텍스트 한국어(`permissions.py:105`)
- deploy 결과 = 한국어 산문(`deploy.py:236`)
- 상태/결과 어휘 = 리터럴(`communication.py:205`, `guide_tools.py:1697`)

모범 반례는 `deploy.py:191`의 명시적 비-일시 에러 분류. **에러/상태를 enum·예외 타입으로 승격**하면 분기·로깅·테스트가 견고해진다. `[R-S2-1]`

### S3 · 권한 정책의 규칙 체계 부재 〔high〕
권한 전체가 **하나의 410줄 함수에 번호 주석으로 박힌 10개 분기**(`permissions.py:90`). 각 게이트의 우선순위·면제·교착방지·자가치유가 중첩돼 상호작용 추적이 어렵다. → "규칙(rule) 객체 + 명시적 우선순위 + 게이트별 단위 테스트" 레지스트리. `[R-S3-1]`

### S4 · 완료-게이트 상태 sprawl 〔high〕
완료 판정 상태가 **~30개 필드로 손수 직렬화/역직렬화**(`_task_snapshot` ↔ `_restore_open_task`, `sys_core.py:393` ↔ `624`)되며, 두 함수의 1:1 대응이 코드로 강제되지 않는다(필드 추가 시 한쪽 누락 = 복구 결함, 주석이 과거 사고 명시). 게다가 게이트 상태가 **이중 메커니즘**(`_gate_pass` 집합 + 병렬 boolean 플래그, `guide_tools.py:2080`). → 단일 dataclass + (de)serialize 자동화. `[R-S4-1]`

### S5 · 설정(config) 분산 〔med〕
~15개 `ORGANT_*` 환경변수가 모듈 곳곳에서 ad-hoc하게 읽힌다(`sys_core`의 `turn_timeout`·`idle_timeout`·`max_flows`·`max_continue`…, `main`의 canary/sleep period, `organt`의 worker CLI, `guide_tools`/`deploy`). frozen `Config`(`config.py:10`)가 *진실원이 아니다*(`deploy.py:23`이 env를 직접 읽음). → 모든 튜닝 노브를 `Config`에 모으고 1곳에서 검증. `[R-S5-1]`

### S6 · 도메인 휴리스틱의 분산·무검증 〔med〕
직군·도메인 판정이 **흩어진 substring 튜플**(`_CAPS` `guide_tools.py:306`, `_FILE_CAP_KW` `permissions.py:69`, 직군 파싱 `_jobs_of`/`_norm_job`)로, 분류 체계·테스트가 없다(거짓양성 이력 다수 — §B의 비활성 게이트가 그 산물). → 도메인 모델 + 테스트. `[R-S6-1]`

### S7 · 공유 헬퍼의 집 없음(잠재 순환의존) 〔med〕
`_jobs_of`/`_norm_job`/`_speech_clip`/`_CAPS`가 `guide_tools`에 살며 `permissions`·`sys_core`·`discord_guide`가 **함수-로컬 import**로 끌어쓴다(`permissions.py:294`, `sys_core.py:593`) — 모듈 최상단 import 시 순환의존이 생길 신호. → 공용 유틸 모듈로 추출. `[R-S7-1]`

### S8 · 지식이 코드 주석에만 산다 + 스테일 문서 참조 〔med〕
라이브 인시던트의 *왜*가 코드 인라인 주석에 방대하게 축적돼 있다(귀중하나 탐색·유지 어려움). 동시에 docstring들이 **존재하지 않는 문서**를 가리킨다: `docs/Rule/Communication.md`·`Other/Guide/Discord.md`·`Request.md`/`Response.md`(repo에 없음, `communication.py:3`·`protocol.py:1`·`discord_guide.py:4`). 또 README/docstring과 코드가 어긋난다(§6.5). → 이 아키텍처 문서가 캐논, 스테일 참조 정리. `[R-S8-1]`

---

## 6.4 검증 요약

- spine 8개 모듈은 **전수 정독**으로 1차 확인.
- `guide_tools`의 적재 인용(`:791`·`:805`·`:809`·`:1178`·`:2060`·`:2080`·`:619`·`:541`·`:590`)과 `communication`/`protocol`의 핵심 인용(`:340`·`:264`·`:93`·`:205`, `protocol:126`·`:127`·`:94`)은 **직접 spot-검증**으로 일치 확인.
- 특히 **Kind 파싱 불일치**(`guide_tools:809` `startswith("w")` ↔ `protocol:126` `startswith("work")`)와 **`restore_chain` 점유 누락**(`communication:340`이 `_engage_frame` 미호출)은 코드로 확정.
- 병렬 분석 에이전트가 본 문서 초안 자체도 교차검토했다(자기참조 정합성 확인).
- 전체 발견의 적대적 검증(인용 정확성 + 주장 방어가능성) 최종 집계는 PR 본문에 첨부한다.

## 6.5 문서↔코드 불일치 (정리 대상)

| 주장(문서) | 실제(코드) | 근거 |
|------------|------------|------|
| README: `DEPLOY_NAME`이 고정 서비스명 | `deploy_sync`는 `DEPLOY_NAME`을 읽지 않음(서비스명은 `deploy_service_name`가 프로젝트별 결정) | `README.md:54` vs `guide_tools.py:739` |
| `parallel_work` 활성 도구로 문서화 | 런타임에서 하드 비활성 | `guide_tools.py:2708-2713` |
| README: `ORGANT_MODEL` 기본 'Opus' | `config`는 None 전달(SDK가 결정) | `README.md` vs `config.py:49` |
| main docstring 로스터 콤마 구분 | 실제는 세미콜론 우선 | `main.py:10` vs `:51` |
| docstring들이 `docs/Rule/*`·`Other/Guide/*` 참조 | 그 파일들이 repo에 없음 | `communication.py:3`, `protocol.py:1`, `discord_guide.py:4` |

---

### 다음
- 이 평가를 우선순위 백로그로 → [07 리팩토링 타깃](07-refactoring-targets.md)
- 항목별 file:line 근거 → [ref/REFERENCES.md](ref/REFERENCES.md)
