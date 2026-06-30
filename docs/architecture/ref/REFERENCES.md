# 근거 색인 (REFERENCES)

이 문서는 아키텍처 문서의 모든 **굵은 주장**에 대한 근거를 한곳에 모은다. 형식: `[ID] 주장 — file:line — 코드 근거 — 논리`.

> 모든 `file:line`은 분석 시점(브랜치 `docs/architecture-core`)의 `src/` 기준이다. **검증 등급**:
> - **(read)** spine 8개 모듈 전수 정독으로 확인
> - **(spot)** 해당 라인을 직접 열어 일치 확인
> - **(wf)** 병렬 분석 에이전트 발견 — 적대적 검증 대상(§ 검증 방법론)

## A. 제어 흐름 (03)

| ID | 주장 | 근거 | 등급 |
|----|------|------|:----:|
| R-CF-1 | 베턴: request 시 receiver wake / sender sleep | `communication.py:191` (`self.alive = to_id`) | read |
| R-CF-2 | LIFO close | `communication.py:202` (`frame = self._stack.pop()`) | read |
| R-CF-3 | 모든 프레임 닫히면 origin 복귀·종료 | `communication.py:209-211` | read |
| R-CF-4 | 활성만 request/respond | `communication.py:165-166`, `:200-201` | read |
| R-CF-5 | busy-guard(흐름 내 Work 재요청 금지) | `communication.py:183-184` | read |
| R-CF-6 | 재진입 금지(멈춘 상위 동료) | `communication.py:178-182` | read |
| R-CF-7 | 점유 배타성(흐름 간) | `communication.py:171-177` | read |
| R-CF-8 | 진입→라우팅 경로 | `main.py:459` → `sys_core.py:2328` → `:1757` | read |
| R-CF-9 | 라우팅 게이트(스코프·max_flows·리더 점유) → 큐 | `sys_core.py:1771-1788` | read |
| R-CF-10 | 선점(active_flows·engage·attach_engagement, await 없음) | `sys_core.py:1837-1842` | read |
| R-CF-11 | 핸드오프: request 즉시 `[위임됨]`, 인플라이트 완주 | `sys_core.py:1388-1395`, `flow.wake=run_turn` `:2003` | read |
| R-CF-12 | 이어가기 루프: 자동 이어가기/위임/조율 | `sys_core.py:1354-1538`, 호출 `:2108-2115` | read |
| R-CF-13 | 종료: ensure_deploy → [Response] → close → 스냅샷 → 큐 드레인 | `sys_core.py:2187-2257` | read |
| R-CF-14 | 무진행 워치독(워커 turn_timeout / 리더 idle_timeout) | `sys_core.py:1540-1570`, `:1156-1179` | read |
| R-CF-15 | 활동 신호 = 도구훅 + 메시지 수신 + stderr | `organt.py:200-235` | read |
| R-CF-16 | restore_chain 깊은 워커 재개(평탄화 폴백 아님) | `communication.py:329-349`, `sys_core.py:1410-1465` | read+spot |

## B. 상태·영속·복구 (04)

| ID | 주장 | 근거 | 등급 |
|----|------|------|:----:|
| R-ST-1 | 3계층 우선순위(디스크>토픽>시드) | `sys_core.py:112-136`, `:320-365` | read |
| R-ST-2 | 원자적 쓰기(tmp+fsync+replace) | `sys_core.py:148-155` | read |
| R-ST-3 | 세션 스코프 분리 + pinned_cwd | `sys_core.py:240-242`, `organt.py:60-71` | read |
| R-ST-4 | 결정론적 스테일 세션 판정 | `organt.py:165-178`, `:270-271` | read |
| R-ST-5 | Task 스냅샷: 사실 영속 / verified 리셋 | `sys_core.py:393-482` | read |
| R-ST-6 | 체크포인트(전이마다) | `sys_core.py:553-567` | read |
| R-ST-7 | 정밀 복구(active_chain·deepest worker) | `sys_core.py:697-727`, `:1410-1465` | read |
| R-ST-8 | 좀비 가드·수렴 파킹 | `main.py:133-152`, `:615-633` | read |
| R-ST-9 | SIGTERM flush | `main.py:395-423` | read |
| R-ST-10 | 게이트웨이 카나리아 | `main.py:433-451`, `:701-729` | read |

## C. 권한·감사·계약 (05)

| ID | 주장 | 근거 | 등급 |
|----|------|------|:----:|
| R-PM-1 | 메시지 계약 dataclass | `protocol.py:17-78` | read |
| R-PM-2 | 멀티라인 Body 파싱(트렁케이션 버그 교정) | `protocol.py:100-131` | read+spot |
| R-PM-3 | 10-게이트 PreToolUse 훅 | `permissions.py:90-498` | read |
| R-PM-4 | Tool redirect | `permissions.py:43-61`, `:106-107` | read |
| R-PM-5 | 작업공간 confinement(`_within` realpath) | `permissions.py:19-27`, `:110-116` | read |
| R-PM-6 | 감사 append-only JSONL + Post 훅 | `audit.py:17-48` | read |
| R-PM-7 | journald 노출(_log) | `sys_core.py:786-801` | read |

## D. 평가 근거 (06) — ✅ / ⚠️ / 🏗️

### 좋음(G)
| ID | file:line | 등급 |
|----|-----------|:----:|
| G1 순수 규칙 코어 | `communication.py:1`, `channels.py:16`, `protocol.py:100` | read/spot |
| G3 default-deny+redirect | `permissions.py:103`, `:106` | read |
| G4 심층방어 비밀 | `guide_tools.py:82`, `deploy.py:245` | read/spot |
| G5 크래시-세이프 | `sys_core.py:148`, `:553`, `main.py:402` | read |
| G7 활동 워치독 | `sys_core.py:1540`, `:1156` | read |
| G8 결정론 스테일 | `organt.py:173` | read |
| G11 배포 실검증+에러분류 | `deploy.py:102`, `:191` | wf |
| G12 봇별 연결 격리 | `main.py:309` | read |

### 개선(B)
| ID | 주장 | file:line | 등급 |
|----|------|-----------|:----:|
| R-B1 | god-function/object | `guide_tools.py:791`·`:805`, `sys_core.py:37`, `main.py:282`, `permissions.py:90`, `deploy.py:245` | read/spot |
| R-B2-1 | 비활성 게이트 ~100줄 `if False` | `permissions.py:310-409` | read |
| R-B2-2 | `organt_allowed_tools` 프로덕션 목록과 다이버전스(테스트만 사용 — '데드' 아님; 검증 교정) | `permissions.py:9`, 사용처 `tests/test_permissions.py:9`·`tests/test_sys.py:301` | read+verify |
| R-B2-3 | `build_options` 오해성 기본값(Bash 포함) | `organt.py:109` | read |
| R-B2-4 | `channels.py` 고아(테스트만 import) | `channels.py:40` | wf |
| R-B3-1 | 게이트가 LLM 마커 문자열 의존 | `guide_tools.py:1178` | spot |
| R-B4-1 | 광범위 `except: pass` 침묵 실패 | `guide_tools.py:1475`, `permissions.py:495`, `main.py:359`, `deploy.py:50` | read/wf |
| R-B5-1 | getattr fail-OPEN(게이트 무력화) | `permissions.py:160` | read |
| R-B5-2 | PAT 유출(에러 문자열) | `deploy.py:280` | wf |
| R-B5-3 | 감사에 파일 내용 전체 기록 | `audit.py:45` | read |
| R-B5-4 | run 미-confinement | `permissions.py:110` | read |
| R-B6-1 | orphan-reaper 무음 붕괴 | `deploy.py:153` | wf |
| R-B6-2 | restore_chain 점유 누락(`_engage_frame` 미호출) | `communication.py:340` | spot |
| R-B7-1 | 좀비/파킹 가드 3중복 | `main.py:566`·`:149` | read |
| R-B7-2 | 원자적-쓰기 3벌 | `sys_core.py:148`·`:181`·`:775` | read |
| R-B8-1 | Kind 파싱 불일치(유효입력은 양쪽 정상매핑 — 저위험 일관성 냄새; 검증이 '위험' 과장 판정→강등) | `guide_tools.py:809` ↔ `protocol.py:126` ↔ `permissions.py:40` | spot+verify |

### 체계필요(S)
| ID | 주장 | file:line | 등급 |
|----|------|-----------|:----:|
| R-S1-1 | Flow/TaskRef 타입계약 부재 | `guide_tools.py:590`·`:541`·`:619`, `sys_core.py:1874-1880` | read/spot |
| R-S2-1 | 에러/상태 분류 체계 부재 | `organt.py:85`, `permissions.py:105`, `deploy.py:236`, `communication.py:205` | read/spot |
| R-S3-1 | 권한 정책 rule registry 부재(10분기 1함수) | `permissions.py:90` | read |
| R-S4-1 | 완료-게이트 상태 sprawl(~30필드 수동 직렬화 + 이중 메커니즘) | `sys_core.py:393↔624`, `guide_tools.py:2080` | read/spot |
| R-S5-1 | Config 분산(env ad-hoc) | `deploy.py:23`, `sys_core.py:57-76` 등 | read/wf |
| R-S6-1 | 도메인 휴리스틱 분산·무검증 | `guide_tools.py:306`, `permissions.py:69` | spot/read |
| R-S7-1 | 공유 헬퍼 집 없음(역방향 reach-in; **순환 아님** — `guide_tools`가 되-import 안 함, 검증 확인) | `permissions.py:294`, `sys_core.py:593` | read+verify |
| R-S8-1 | 스테일 문서 참조(존재하지 않는 docs/ 파일) | `communication.py:3`, `protocol.py:1`, `discord_guide.py:4` | spot/wf |

## E. 의존성 (02)
| ID | 주장 | file:line | 등급 |
|----|------|-----------|:----:|
| R-DEP-1 | 공유 헬퍼 역방향 reach-in 결합(순환 아님) | `permissions.py:294`, `sys_core.py:593` | read+verify |
| R-DEP-2 | `channels.py` 고아 | `channels.py:40` | wf |

## F. 문서↔코드 불일치 (06.5)
| 주장(문서) | 실제 | 근거 |
|------------|------|------|
| README `DEPLOY_NAME` 고정 서비스명 | `deploy_sync` 미사용; `deploy_service_name`가 결정 | `README.md:54` ↔ `guide_tools.py:739` |
| `parallel_work` 활성 | 런타임 하드 비활성 | `guide_tools.py:2708-2713` |
| README `ORGANT_MODEL` 기본 Opus | `config`는 None 전달 | `config.py:49` |
| main docstring 로스터 콤마 | 세미콜론 우선 | `main.py:10` ↔ `:51` |
| `docs/Rule/*`·`Other/Guide/*` 참조 | repo에 없음 | `communication.py:3` 등 |

---

## 검증 방법론

1. **전수 정독(read)**: `sys_core`(2334)·`communication`·`protocol`·`organt`·`main`·`permissions`·`audit`·`config`를 라인 단위로 읽어 직접 확인.
2. **Spot 검증(spot)**: `guide_tools`/`communication`/`protocol`의 적재 인용을 `sed`로 해당 라인을 열어 일치 확인(예: `guide_tools:809`·`:1178`·`:2060`·`:2080`·`:619`·`:541`·`:590`, `communication:340`·`:93`·`:205`, `protocol:126`·`:127`).
3. **병렬 분석(wf)**: 13개 모듈을 독립 에이전트가 분석 → 본 색인의 `discord_guide`·`deploy`·`channels` 등 비-spine 모듈 근거 제공.
4. **적대적 검증(verify)**: 154 에이전트·~2.69M 토큰 규모 다중에이전트가 **140개 발견을 전부 소스 재대조** — **129개(92%) 유지**, 11개 과장/오류, 인용 4건 보정. 본 문서에 반영됐던 4건(`R-B2-2`·`R-B8-1`·`R-S7-1`/`R-DEP-1` + 07 deploy 테스트)은 그 판정에 따라 **교정 반영**(등급 `read+verify`/`spot+verify`로 표기).
5. **동시성 드리프트 재확인**: 세션 중 동시 커밋이 `src/sys_core.py`만 수정 → 본 색인의 모든 인용 anchor(함수 def 라인)를 **현재 커밋 파일과 재대조해 라인 드리프트 0 확인**.

> 등급이 `wf`(미-검증)인 항목 중 보안·정확성 핵심(R-B5·R-B6)은 [07](../07-refactoring-targets.md) P0에서 우선 재확인·교정 대상으로 둔다.
