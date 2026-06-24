# Organt "Rule" — Complete Collaboration-Primitive Specification

> **Purpose.** This document is the authoritative, structured map of every first-class
> collaboration primitive the Organt **Rule** defines, so each can be modeled as a native
> object/event in the purpose-built SNS that replaces the current Discord implementation.
>
> **Sources of truth.**
> - **Spec (the "Rule"):** `/home/user/docs/Other/Rule/**` (Communication, Request, Response,
>   Status, Task, Project), plus `RFC/`, `Architecture/`, `Feature/`, `ADR/`, `기획/`.
> - **Behavior (ground truth):** `/home/user/PJT/src/` — chiefly `communication.py`
>   (LIFO baton + Engagement ledger), `protocol.py` (the `[Request]`/`[Response]`/`[Task-XXX]`
>   wire format), `guide_tools.py` (the tool surface: `request`, `recruit`, `run`,
>   `create_project`, `create_task`, `set_goal`, `complete_task`, `vote`, `meet`,
>   `parallel_work`, `deploy`, `send_file`), `sys_core.py` (orchestration loop, checkpoint /
>   recovery, sleep-distillation / role_profiles), and `permissions.py` (the PreToolUse
>   ownership/absorption-block hooks).
>
> **The central thesis.** The docs and code are explicit that **Discord is a misfit**
> (`ADR-001`: "기존 SMS 플랫폼 모두 우리의 프로젝트 핏과 일치하지 않으며, 자체 SMS 플랫폼
> 제작이 언급되었다"). Almost every Rule concept is *simulated* on Discord through text-block
> conventions, channel/thread overloading, and a single system bot editing one status message.
> The native SNS should make these concepts **first-class typed objects and events** rather
> than parsed text. Each primitive below ends with its **Discord misfit** and feeds the final
> **Native-SNS implications** section.
>
> **Vocabulary.** *Organt* = a bot "employee" (an LLM agent + persona/CLAUDE.md + skills +
> permission hooks + state). *User* = the human. *SMS/SNS* = the social platform (the window).
> *SYS* = the orchestrator. *Guide* = the concrete tool implementation of an abstract Rule.

---

## 0. The architecture frame (context for every primitive)

`RFC-002` / `Architecture/Core.md`:

- Subjects are **`Organt`** and **`User`**. All behavior runs through the **`Rule`**; a
  **`System` (SYS)** controls/enforces the Rule. Subjects use **`Tool`s**; each Tool must be
  used per a **`Guide`** written to match the Rule.
- Control path: **`User ⇄ SMS ⇄ SYS ⇄ Organt`** ("모든 흐름을 SYS에서 제어"). The flow never
  originates from an Organt — it starts from the User via the SMS.
- A `Rule` is an *abstract capability* (communication, file storage). A `Guide` is its
  *implementation* (Discord communication, local file storage). **The SNS replaces the Guide
  layer**, so the Rule stays identical while the substrate changes.

Two **work units** (`작업단위`) and four **communication units** (`소통 단위`) are the Rule's
own first-class nouns:

- Work units: **Project**, **Task**.
- Communication units: **Request**, **Response**, **Status** (+ the **Communication** flow
  that sequences Requests/Responses).

Everything else (baton, goal-consensus gate, meet/vote, cross-check, circuit-breaker,
ownership/absorption hooks, recruit/직군, sleep-distillation/role_profiles, deploy, recovery)
is *runtime machinery* that enforces or extends those nouns. They are documented here as
primitives because the native SNS must model them too.

---

## 1. The Single-Flow LIFO Baton (Communication flow)

**Spec:** `Other/Rule/소통 단위/Communication.md`. **Code:** `communication.py`
(`CommunicationManager`), driven by `sys_core.py`.

### (1) What it IS
A **Communication** is the act of exchanging information, realized as a strictly-ordered
exchange of **Request**/**Response** messages. At any instant **exactly one Organt is "alive"
(active)** — the *baton*. The flow is a **call stack of open Requests**; responses close them
in **LIFO** order. Rationale (code docstring): "항상 1명만 활성(단일흐름) = 토큰 절약·사이드
이펙트 감소." It models a single coherent thread of execution across many agents (no two bots
"typing" at once within a flow).

### (2) Lifecycle / states
Canonical flow (`Communication.md`):
1. The flow **starts from the User** via the SMS. *Organts never start a flow themselves.*
2. The alive Organt sends a **Request** → sender **sleeps**, receiver **wakes** (`alive = to`).
   Only one is alive. A **Work** Request **cannot** go to an Organt already holding unfinished
   Work (overlap & cycle prevention).
3. The receiver works and may send further Requests (recurse from step 2 — the **stack grows**).
4. A **Response** closes its Request; the original sender **wakes again**. Requests close in
   **reverse (LIFO)** order.
5. On a **Work** Response, the delegator **Accepts** or **Redoes**. Redo beyond a limit
   **escalates up**.
6. When **all Requests are closed**, the flow returns to the **origin** (start point) and **ends**.

`CommunicationManager` state: `origin`, `alive`, `_stack: List[Frame]`, `done`, `history`,
`redo_limit`/`_redo_counts`, `_delivered` (delegator→owner Work pairs that closed with Accept).
A `Frame` = `{from_id, to_id, request_id, kind, body}` (body kept for precise recovery).

### (3) Events / transitions (state machine on the stack)
- `check_request(from,to,kind)` — pure validation, raises `CommError` if illegal.
- `request(...)` → push Frame, `alive = to`, engage both bots in the ledger. **Guards:**
  must be the alive Organt; no self-request; not to an *ancestor* (a bot waiting on your
  response — re-entry forbidden); Work not to a current *participant* (busy-guard); not to a
  bot busy in another flow (`BusyInOtherFlow`).
- `respond(from,result,text)` → pop top Frame (LIFO), `alive = frame.from_id`; if
  `result=="accept"` and Work, record `_delivered[(from,to)]`; if stack empty → `done=True`,
  `alive=origin`; release any bot no longer in the stack back to the company pool.
- `redo(...)` → re-`request` same target, increments `_redo_counts`; over `redo_limit` raises
  `RedoLimitExceeded` (→ escalate).
- `escalate(reason)` → force-close top Frame, hand baton up; if empties stack → `done`,
  `escalated_to_origin=True`. Used for timeouts/dead workers (deadlock break).
- `report_up_to(reporter, owner)` — **partial upstream rewind**: if a deep worker needs an
  *upstream owner's* prerequisite, LIFO-unwinds to that owner (intermediate bots **relay only**,
  do not absorb), returns the closed sub-chain so it can be **re-descended/replayed** after the
  owner resolves. Arbitrary depth/target, no hardcoding.
- `restore_chain(frames)` — **state restoration after a crash** (not chat replay): rebuilds the
  stack and sets `alive` to the **deepest worker** so unwinding naturally preserves each bot's
  scope (C does C, B integrates C, A integrates B). See §15.

### (4) Actors / roles
- **origin** = the User/SMS entry point (not a bot; never engaged in the ledger).
- **alive** = the single currently-active Organt.
- **ancestors** = bots on the stack waiting for a response (frozen, re-entry forbidden).
- **direct_delegator** = the requester of the top frame; the one bot to whom the alive Organt
  may bounce a *clarify* question (see §3).

### (5) Discord misfit
"한 번에 하나만 alive"는 Discord에 **존재하지 않는 개념**이다 — Discord lets every bot post
anytime. The single-flow invariant is enforced *entirely in `sys_core`/`communication.py`*,
invisible to Discord. The flow's call stack is reconstructed by parsing `[Request]`/`[Response]`
text and reply-edges (`protocol.py`). `Communication.md` itself flags the mismatch: "지금 상황이
현실에 Meet을 잘 표현할 수 있는지 모르겠어요. Peer to Peer에 가까운 소통인 것 같아요." The
baton, sleep/wake, and LIFO unwinding are pure runtime state with **no Discord representation**.

---

## 2. Request (Work vs Info) — the non-blocking handoff

**Spec:** `Other/Rule/소통 단위/Request.md`. **Code:** `protocol.Request`, `Kind`;
`guide_tools.request` (lines 737–1327); `communication.request/redo`.

### (1) What it IS
A **Request** is a message that demands something; the sender **blocks** (sleeps) until the
paired Response returns. Attributes: **From** (one Organt), **To** (one Organt), **Kind**
(`Work`=작업 / `Info`=정보), **Body** (Work → a *goal*; Info → a *question*).
The `request` tool is the single richest primitive — it carries the baton handoff plus a stack
of structural gates.

### (2) Lifecycle / states (of one Request)
`pending` → (worker wakes, possibly recurses) → closed by Response with a **result marker**:
`accept` | `redo` | `incomplete` | `premature` | `refused` | `failed` | `clarify`. These
markers (set in `_deliver`) drive whether the Task can later complete, whether a re-send is a
**Redo** vs a fresh delegation vs a **continuation**, and whether ownership releases.

### (3) Events / transitions & gates (in dispatch order — `guide_tools.request`)
1. **No Task** → reject ("create_task 먼저").
2. **Target is `예비` (unassigned)** → reject; must `recruit(role=…)` first ("말로 직군을
   정하지 말고"). *You cannot give work to a bot with no 직군.*
3. **Info to your direct delegator** → not an error; **bounce the baton back as a `clarify`**
   (the delegator answers and re-assigns). End the turn immediately.
4. **Target not in Task team** → auto-join if a project-team member; else informative reject
   listing same-domain teammates / pool.
5. **Serialize on the baton** (wait up to 3600s until `alive==me`) — *not* a rejection.
   **Idempotent merge:** identical same-segment `(me,to,kind,body)` reuses the cached response
   (no double-wake). If a prior delegation is still in-flight (detached), return `[대기]` and
   end the turn.
6. **`check_request`** (the §1 protocol guards), incl. `BusyInOtherFlow` → return available
   same-domain alternatives, forbid polling.
7. **Goal-before-Work gate:** `kind==WORK and not goal` → reject ("Work 위임 거부: … 먼저 …
   set_goal로 확정한 뒤"). Info is always allowed (so consensus discussion is never blocked).
8. **Circuit-breaker hold:** if `loop_escalated` and a *new verification* Work to a verifier →
   block ("[수렴 경보 — 검증 보류] … 사람 판정 대기 중"). (See §8.)
9. **Reverify-dedup:** same verifier (`to in cross_checkers`) + code unchanged
   (`writes == last_verify_writes`) → block ("재검증 보류(… 변경 0)"). (See §8.)
10. **Non-leader cross-domain Work gate:** a non-leader opening *new* work in another domain
    where a capable expert exists (`_offdomain_capability_hit`) → **enqueue to the leader
    coordination queue** (`flow.pending_coordination`) and tell the worker to keep doing their
    own domain. (See §10.) Verification & Info advice are never blocked.
11. **Leader off-domain redirect:** leader Work to a target whose 직군 lacks the required
    capability → reject and name the right expert(s) ("위임 거부(직군밖 — 능력 미스매치)").
12. **Redo vs fresh:** if this `(me,to)` Work already closed with Accept, it's a **Redo**
    (`communication.redo`, counted; over limit → reject). Otherwise a fresh `request`. The
    owner body is rewritten as a **delegation contract** (see (4)).
13. **Owner assignment by receipt:** first Work-receiver becomes `flow.current.owner` (no
    pre-assignment — "수신=소유"); checkpointed.
14. **Non-blocking handoff (`_handoff`)**: register the delegation as an in-flight task and
    return **immediately** with "[위임됨 — SYS가 동료를 끝까지 완주시켜 결과로 당신을
    이어줍니다]". The worker's *whole turn* is **not** awaited inside the tool call (avoids
    CLI 75s cancel → detach churn). SYS drains it out-of-band (`_deliver`, `_drain_inflight`)
    and resumes the requester with the result. Baton already moved to `to`, so re-delegation is
    structurally impossible (single flow).

### (4) The delegation **contract** injected into a Work body
On a fresh Work delegation the owner receives a structured contract (not a free re-written spec):
- "[위임 — 이 목표를 끝까지 책임지는 owner는 당신입니다] 이 Task의 Goal: …" + "직접 구현하고
  run으로 '목표가 충족됨'을 검증한 뒤 … 보고하세요."
- **Vertical-slice mandate** ("수직 슬라이스 우선 … 마지막 통합 몰빵 금지", RFC-005).
- **Report contract** skeleton: `[결과]/[변경]/[검증]/[리스크]`.
- **Off-domain refusal channel:** if the goal mixes a category outside the owner's 직군, reply
  first line `[직군밖] 필요직군명` (self-definition of domain fit).
- **Interface contract** (`flow.current.interfaces`) + "직접 합의 — 리더 중계 금지": coordinate
  meshing details by *direct* `request(Info)` to the other domain's owner.
- **Collaboration notes** (`collab_notes` from meet/vote) so meeting consensus reaches the
  implementer (spec-evaporation fix, P-009).
- **Quality rubric injection:** if this Work goes to a *non-owner after owner delivery* (i.e. a
  verification), the **owner-domain 직무기준** (`craft_of`) is attached as a scoring rubric
  ("'돌아가는가'가 아니라 '충분한가'"). (See §13.)

### (5) Discord misfit
Encoded as text (`protocol.format_request`):
```
[Request]
To: <@id>
Kind: Work|Info
Body: ...
```
`From` is inferred from the posting bot, `RepliesTo` from a Discord reply, identity from the
message ID (`Discord.md`). **Kind, the baton handoff, all 14 gates, the contract, owner-by-
receipt, and the non-blocking handoff are invisible runtime logic** — Discord shows only a
formatted message and (optionally) a ✅/⚠️ reaction the system adds on the Response.

---

## 3. Response (+ Accept / Redo / clarify) — closing a Request

**Spec:** `Other/Rule/소통 단위/Response.md`. **Code:** `protocol.Response`;
`communication.respond`; `_deliver` result-marker logic.

### (1) What it IS
A **Response** closes a Request. Attributes: **From**, **To** (= the original requester),
**RepliesTo** (the Request it closes), **Body** (Work → result report; Info → answer).

### (2) Lifecycle / states
A Response pops the top stack Frame (LIFO), re-wakes the requester, and carries a **result
marker** that classifies the close: `accept` (closed; records the delivered Work pair),
`redo`, `incomplete` (turn-limit/timeout — re-delegate as *continuation*, no Redo limit),
`premature` (owner woke but did 0 real work — re-delegate, not absorb), `refused`
(`[직군밖]` — release ownership, recruit the named expert), `failed` (infra crash — **do not
re-staff**; same env = same crash), `clarify` (bounced question, not a completion).

### (3) Events / transitions
- On `accept` + Work → `_delivered[(from,to)]` set ⇒ next same-pair Work is a **Redo**.
- `owner_delivered=True` only when an owner did real work (`run`/Write) **and** returned
  substantive text — this is the evidence that unlocks `complete_task`.
- `cross_checks`/`cross_check_offdomain` increment when a *third party* responds about a
  ready product (independent verification; off-domain counts separately — homogeneous-model
  echo guard). (See §8.)
- **clarify**: surfaces the worker's question back to the delegator as the Response, who then
  re-assigns (collaboration, not error).

### (4) Actors / roles
Owner (delivers), delegator (Accept/Redo), third-party verifier (drives cross-checks),
direct_delegator (target of clarify bounce).

### (5) Discord misfit
```
[Response]
Body: ...
```
attached as a **reply** to the Request message (`RepliesTo` = the reply edge). The rich result
**markers** (`accept`/`incomplete`/`premature`/`refused`/`failed`/`clarify`) have **no Discord
representation** — they live only in `communication`/`flow.current`. Accept/Redo "이 또한
Thread에 메시지로 남는다" per `Discord.md`, but the structural consequences are runtime-only.

---

## 4. Status — the always-visible liveness signal

**Spec:** `Other/Rule/소통 단위/Status.md`. **Code:** `protocol.TaskStatus`,
`sys_core._status_text` (≈480–502), `format_task_status`; status period `ORGANT_STATUS_PERIOD`.

### (1) What it IS
The **Status** is a message that shows the User the live state of a flow **before they ask**
(상시 가시화), and whose **updates must be silent** (no new notification). It is the **only
system-authored message** allowed in the channel.

### (2) Lifecycle / states
Created when a flow starts; updated silently throughout; finalized to **완료 / 중단** when the
flow ends, after which it never updates again.

### (3) Events / transitions / rules
- Shows: what is in progress, **who is working now** (`flow.comm.alive`), delegation count,
  `leader_segment`, and **last-activity time**. A long-stalled timestamp must itself read as an
  anomaly to the User.
- **Dynamic, self-aging timestamp** is mandatory ("갱신이 멈춰도 스스로 늙어 보이는 형식") — a
  frozen "N초 전" string is a **false liveness signal** and is forbidden. (`Discord.md` realizes
  this with `<t:unix:R>`.)
- **No other system-authored posts.** Internal coordination events (request rejections, vote
  tallies, deploy results) are **never** posted to the channel — they return to the *party only*
  via tool return. The channel is for Organts' speech and artifacts; internal mechanism is hidden.

### (4) Actors / roles
Authored by the **System bot**; consumed by the **User**.

### (5) Discord misfit
One status message **edited in place** (Discord `edit` makes no notification → "조용한 갱신"),
with a `<t:unix:R>` dynamic timestamp. The "exactly one system message" rule exists *because*
Discord would otherwise spam internal events into the channel. A native platform can model
Status as a **live entity with a presence/heartbeat**, not a hand-edited message.

---

## 5. Project — the domain space

**Spec:** `Other/Rule/작업단위/Project.md`, `RFC-004`. **Code:** `sys_core` project registry
(`_register_project`, `_load/_save_projects`, topic reconcile), `guide_tools.create_project`.

### (1) What it IS
A **Project** is a *work unit and a space* with **one domain**, progressed incrementally through
**Tasks**. Created by the User, but **driven entirely by Organts**. It has **no explicit purpose
or end** (unlike a Task). Attributes: **Leader** (drives, can create Tasks), **Workspace**,
**Context** (recent work / key decisions / direction — all members must know it), **Archive**
(spec + past Tasks for reference).

### (2) Lifecycle / states
Registered as `P-NNN` (identity = number, not name — the system strips bot-supplied `P-…`
prefixes). Conflict resolution: same name + same purpose (≥50% token overlap) → reuse; same
name + different purpose → new `P-NNN`; never orphan an open Task by moving channels. Workspace
folder renamed to `p-NNN-slug`. Persists in `projects.json` (+ committed seed fallback) and is
reconciled against the Discord channel topic on boot.

### (3) Events / transitions
`create_project(name, team)` → create channel/space, assign team (`[leader] + assigned`, leader
prepended), register. Leader auto-reassigned if disconnected (`_valid_leader`). Cross-project
**user feedback aggregation** applies learned standards to new projects (`_aggregate_feedback`).

### (4) Actors / roles
**Leader** (the first/driving Organt), project team (invited members), the User (creates,
gives feedback).

### (5) Discord misfit
"Project당 하나의 Channel" (`Discord.md`). Project identity is anchored in a **channel-topic
string**: `[ORGANT:P-NNN] leader=ID | ws=path | name=label`, parsed/reconciled by SYS. Context
and Archive have no Discord home — they live in the workspace and `projects.json`. A channel is
a chat surface, not a domain object; the registry, topic-parsing, and seed/topic/disk
reconciliation are all workarounds for "a channel is the only durable handle Discord gives us."

---

## 6. Task — the bounded goal unit

**Spec:** `Other/Rule/작업단위/Task.md`, `RFC-004`. **Code:** `guide_tools.create_task`,
`set_goal`, `complete_task`; `Flow.TaskRef`; `protocol.TaskStatus`.

### (1) What it IS
A **Task** is the unit of work to accomplish a measurable **Goal**. Unlike a Project, it has a
**purpose and a termination condition**. Attributes:
- **Goal** — *measurable, real-measurable* success criterion. The **Team** sets it from the
  Purpose; the **Leader** judges whether it is met.
- **Purpose** — a *problem*, not a solution; given at start.
- **Team** — the Organts who complete it; the Leader recruits as needed.
- **Leader** — *drives* (leads more than works), decides, judges Goal completion, holds
  priority *within this Task* ("Work보다는 Leading", Task.md).

### (2) Lifecycle / states (Task.md flow)
1. Created with a Leader + Purpose. 2. Leader recruits the Team. 3. Team sets the Goal.
4. Create a Todo, Leader manages it. 5. Leader distributes work. 6. Leader checks finished work.
7. Leader checks Goal completion; if unmet, loop to 4. 8. Leader reports the outcome.

Runtime states on `TaskStatus`/`flow.current`: `진행/완료/중단`; `owner`, `owner_delivered`,
`owner_incomplete`, `verified`, `run_count`, `cross_checks`/`cross_check_offdomain`/
`cross_checkers`, `cc_held`, `loop_escalated`, `participated`, `peer_info_pairs`,
`work_delegated`/`work_delegated_to`, `collab_notes`, `acceptance`, `standard`, `interfaces`,
`evidence`, `_gate_pass`.

### (3) Events / transitions
- `create_task(members)` opens an **empty shell** — *Purpose deliberately blank, members only*
  ("리더가 할 일을 미리 못 박음 = 중앙집권 방지"); each expert later proposes their **own
  domain's** sub-work/ownership.
- `set_goal` confirms Purpose/Goal/acceptance/standard/interfaces (see §7).
- Work delegation assigns `owner` by receipt; `run` sets `verified`; `complete_task` closes
  with `result` (see §9). Checkpointed at **every transition** (`_checkpoint_open_task`).

### (4) Actors / roles
Leader (drives, judges), Team (sets Goal, executes), Owner (the receiver of a domain's Work),
verifiers (third-party/QA).

### (5) Discord misfit
"Task는 하나의 Thread를 가진다"; created via `/Task`, the system bot posts/edits a `[Task-XXX]`
status block (`Purpose/Status/Goal/Owner/Group/(result)`). A **thread = a Task scope** and
**channel membership = team membership** (`_add_members` on recruit). The whole completion state
machine (owner/verify/cross-check/gates) is compressed into a text block plus runtime fields. A
thread is a conversation container, not a stateful Task object.

---

## 7. set_goal — goal-before-work, team-consensus gate

**Code:** `guide_tools.set_goal` (1684–1955).

### (1) What it IS
Confirms and records the Task's **Purpose** (the problem) and **measurable Goal** (success
criterion), plus **acceptance** (concrete "good" conditions from each expert), **standard**
(the decomposed *maximal* component list), and **interfaces** (cross-domain contracts). It is
the **structural enforcement of "consensus → distribution" ordering**: leader-unilateral or
pre-specified goals are forbidden, and Work delegation is blocked until a Goal exists (§2 gate 7).

### (2) Lifecycle / states
Hold/confirm gate with per-Task one-time holds keyed by field presence (not per-flow flags):
- **Consensus-coverage hold:** every *reachable, uncovered domain* must have ≥1 participant in
  `participated` ("도메인 커버리지" not literal "전원" — same-직군 surplus is *echo* and
  excused for parallel use; busy-elsewhere domains are excused to avoid deadlock).
- **Busy-domain hold (once):** if a domain's only rep is busy in another flow → consciously
  choose ①wait (recommended) or ②proceed-if-clearly-closed (that expert builds their domain at
  execution). "정확하면 반출, 모호하면 대기."
- **Maximality hold:** holds *until `standard` is actually recorded* as a **decomposed,
  checkable component list** (WebSearch a real exemplar; include the **main usage flow /
  실사용성**, not just features) — or an explicit `[최대화 N/A: 사유]`.
- **Category-completeness check (P7):** one hold to fold a "wholly missing category" into the
  goal as a *build target* (or recruit), not just "noticed".

### (3) Events / transitions
On pass: writes Purpose/Goal/acceptance/standard/interfaces to `flow.current`; these flow into
the delegation contract (§2.4) and the completion gates (§9). The acceptance/standard/interfaces
are later *verified to have reached the code* at `complete_task`.

### (4) Actors / roles
The **Team** (via `meet` strongly recommended over 1:1 Info — less anchoring, auto-minutes);
the Leader records the converged result but may not dictate it.

### (5) Discord misfit
There is **no Goal/consensus object** in Discord — `set_goal` writes into the `[Task-XXX]` block's
`Goal:` line and runtime fields. The "team agreed" fact is inferred from `participated`
(populated by substantive `request(Info)`/`meet`/`vote`). The ordering invariant (no Work before
Goal) is pure runtime gating with no Discord analog.

---

## 8. Cross-check verification + reverify-dedup + circuit-breaker (수렴 경보)

**Code:** `request` cross-check accounting (1245–1290), reverify-dedup (867–885),
circuit-breaker (852–866, 1271–1288), `complete_task` cross-check gate + `cc_held` thrash
(2106–2244), constant `_LOOP_ESCALATE_CROSS=12` (env `ORGANT_LOOP_CROSS`).

### (1) What it IS
A family of mechanisms that make **independent third-party verification a structural gate** and
break **non-converging verification loops**:
- **Cross-check:** when a *ready product* exists and a *third party* (not owner, not leader)
  responds about it, `cross_checks += 1`; if the verifier's domain differs from the owner's,
  `cross_check_offdomain += 1` (homogeneous-model echo guard: "독립 검증 = 다른 도메인"). The
  verifier id is recorded in `cross_checkers`, and `last_verify_writes` snapshots code size.
- **Reverify-dedup:** the *same verifier* may not re-verify the *same unchanged code*
  (`to in cross_checkers` and writes == `last_verify_writes`) — kills the "최종 검증" infinite
  loop (P-031 ~13×).
- **Circuit-breaker (수렴 경보):** at `cross_checks >= 12` without closing, set `loop_escalated`,
  **escalate once to the User** ("[수렴 경보 — 사람 판정 필요] … ① 현 상태 수용·마감 / ②
  다른 방향 제시"), and **also halt** further verification delegations (§2 gate 8) — the alarm
  must *stop* the loop, not just notify. Owner-fix Work and `complete_task` remain allowed (no
  deadlock).

### (2) Lifecycle / states
`cross_checks` / `cross_check_offdomain` (counters) → `loop_escalated` (one-shot, persisted) →
verification-blocked until User intervenes. `cc_held` counts how many times the completion
cross-check gate has held; **≥3 → "반복 마감 — 독점·헛돎 경보"** escalation (leader thrashing on
solo `run`+re-complete). All counters are checkpointed (recovery must not re-demand verification).

### (3) Events / transitions
Cross-check increments on third-party Response; circuit-breaker fires on the 12th; reverify-dedup
blocks redundant re-verification; `cc_held` thrash escalates repeated empty completes.

### (4) Actors / roles
Owner (produces), **third-party verifier / QA** (independent check — QA prioritized in the gate),
Leader (must delegate verification rather than self-complete), User (final judge on escalation).

### (5) Discord misfit
No "verification" object exists in Discord; cross-check is *inferred* from who replies about an
owner's artifact. The circuit-breaker is the **one exception** that *does* post to the channel
(an escalation to the User) — everything else is tool-return-only. Verification depth, dedup, and
convergence detection are entirely runtime.

---

## 9. complete_task — the quality close gate (+ the [Response]/result)

**Code:** `guide_tools.complete_task` (1955–2424).

### (1) What it IS
Closes a Task when its Goal is met, recording `result` into the status block and clearing
`flow.current`. It is a **stack of quality gates** — completion is not a free assertion; it must
be *earned* by evidence.

### (2) Lifecycle / states
Pass requires clearing, in order, gates that each hold (often one-hold-then-pass, keyed in
`_gate_pass`):
1. **Verified gate:** Task must have been `run`-executed ("허위 완료 금지").
2. **Owner-incomplete gate:** owner returned incomplete (turn-limit/timeout) → re-delegate
   same owner as continuation.
3. **Owner-not-delivered gate:** if delegated, owner must have produced a *verified artifact*
   (`owner_delivered`).
4. **Perceptual-asymmetry gate:** non-visual dimensions (audio/tactile) need *real assets*, not
   code synthesis (or an explicit "[지각차원 없음: …]").
5. **Acceptance gate:** the agreed "good" conditions must have *reached the code*
   ("[수용기준 검증]" or "[수용기준 N/A: …]").
6. **Data-provenance gate:** "real/public data" must not be synthetic ("[데이터 출처: …]").
7. **Cross-check gate (hard):** ≥1 *independent (off-domain)* cross-check when a product and a
   third party exist; injects the **owner-domain 직무기준 rubric** + user-taste anchor; `cc_held`
   thrash escalation; suggests horizontal `meet` cross-critique → owner polish.
8. **Standard/maximality gate:** the decomposed `standard` components must be *accounted for in
   code* (multi-item, not a bare header) ("[최대성 검증]").
9. **Interface direct-agreement gate:** if interfaces + ≥2 worked domains exist, owners must have
   *directly* (peer↔peer Info, `peer_info_pairs`) agreed contracts — not via the leader.
10. **Team-contribution gate:** idle members (`act_by==0`) must be given Work, removed, or
    declared `[기여 불필요: 사유]`; members who *only spoke in meetings* but never received Work
    cannot be excused without at least one delegation.

Non-blocking marks for transparency: **solo-completion** ("[검증: 단독 마감 — 교차 검증 0]") and
**contribution-idle** ("[기여 미흡: …]") are recorded in `result`, plus **URL-truth** injection
(system-observed `flow.deployed` overrides bot-claimed URLs).

### (3) Events / transitions
On pass: status → `완료`; `result` assembled (incl. system run-evidence and marks);
`flow.current = None`; `_ckpt`. Idempotency via `_gate_pass` tuples.

### (4) Actors / roles
Leader (calls, judges), owner (must have delivered), third-party verifiers/QA (cross-check),
team (contribution), User (taste anchor / escalation).

### (5) Discord misfit
The close is just an edit of the `[Task-XXX]` block to status `완료` + `result`. **All ten gates
are invisible runtime logic**; the User sees only the final block. "Completion is earned by
evidence" has no Discord representation at all.

---

## 10. Ownership boundaries / domain, leader-monopoly block, cross-domain coordination queue

**Spec:** the 소유 경계 / "직군" / specialization principle (Task.md "Work보다는 Leading";
Architecture/Core.md "권한 밖 행동 차단"). **Code:** `permissions.py` PreToolUse hooks (~136–417),
`_offdomain_capability_hit` + `_CAPS`, `flow.pending_coordination`, `sys_core._auto_coordinate`.

### (1) What it IS
A set of **structural ownership rules** that prevent **absorption** (흡수 — one bot doing work
that belongs to another domain/owner), enforce **single-owner accountability**, and route
cross-domain work through the Leader's **coordination queue** instead of ad-hoc cross-domain
delegation. *Self-definition principle:* domain fit is judged by the receiving expert (and the
capability table), not by keyword classification by the system.

### (2) Lifecycle / states (the PreToolUse "흡수 차단" hook ladder, Write/Edit/run)
- **Implementation only in a Work context** — a bot woken by **Info** may *not* pre-implement
  (Write/Edit blocked); implementation happens only under Work delegation.
- **#6 Leader-monopoly block:** if a Task has other-domain teammates and the leader has
  `work_delegated==0` but `leader_writes>=1` → block ("리더 단독 구현 차단 … 중앙집권·독점").
- **#7 Intervention-monopoly block:** in an intervention, leader cannot solo-`run`
  reproduce/fix/verify without delegating (Task + Work required).
- **#8 Relative-absorption block:** leader doing *more* than the whole team (grace
  `lead_act>=8`) → block.
- **#9 Generic absorption block:** *any* actor (leader or owner) — if a *distinct-domain, idle,
  un-delegated, reachable* teammate exists, their work may not be absorbed ("모르는 일까지 하지
  말 것"); same-domain is not absorption. Deadlock-safe: only blocks when a reachable teammate
  actually exists.
- **#10 Stuck-worker absorption block ("같은 사람 재요청"):** when the baton bounces back from a
  stalled sub-owner, a *different-domain* actor may not Write/Edit to absorb the stuck work;
  released when the stuck worker acts again (`_stall_victim`/`_stall_blocks` with N-fallback so a
  truly dead worker doesn't freeze the build).
- **S2 leader proxy-implementation block:** the leader is *not* exempt from these — leader proxy
  Write/Edit on owner files is blocked to force `request(Work)` ("Work보다 Leading").

### (3) Events / transitions
`_offdomain_capability_hit(flow, to, body)` returns `{capability: [members]}` when the Work
needs a capability the target lacks but another teammate has → the **cross-domain coordination
queue** (`flow.pending_coordination`). `sys_core._auto_coordinate` then makes the **Leader**
directly delegate to the right expert next turn ("[SYS 조율 — X도메인이 막혀 당신(Y도메인)에
배정]"), breaking the worker↔PM ping-pong loop. Ownership releases on `[직군밖]` refusal and on
clean delegation unwinding (`_release_closed`).

### (4) Actors / roles
Every actor is bound by the hooks; the **Leader** is the *single coordination point* for new
cross-domain work; the **owner** holds single accountability; **distinct-domain idle teammates**
are protected from absorption.

### (5) Discord misfit
Discord has **no concept of ownership, domain, or "don't do someone else's work"** — all of this
is enforced by PreToolUse hooks intercepting file/`run` tool calls, plus a runtime coordination
queue the SYS injects into the leader's next prompt. None of it is visible or expressible in
Discord; it is the most heavily "shoehorned" layer.

---

## 11. recruit / 직군 (job assignment), "first item = leader", personas

**Spec:** Architecture (Organt = Agent + persona/CLAUDE.md + Skill + Hook + State; 기획 "파운데이션
모델 등록하여 커스텀 직원 생성"). **Code:** `guide_tools.recruit` (1329–1499); leader pick in
`create_project`; `_SPARE_LABEL="예비"`, `_JOB_SEP="·"`, `tentative_roles`, `_persist_job`.

### (1) What it IS
**recruit** assigns/creates **직군 (job roles)** at runtime — turning a **`예비` (spare,
unassigned) bot** into a specialist, or adding a second job (겸직). The roster is a pool of bots;
each Organt is a persona (CLAUDE.md = "개인의 인격과 기억"). **1봇 1직업** specialization is the
core company principle.

### (2) Lifecycle / states
`예비` → (recruit role) → **tentative** (`tentative_roles`, runtime only) → **persisted on first
real work** (Write/Edit/run; "일로 직업 획득 — 직업=기억"). A bot that never works stays spare
(prevents zero-memory job proliferation). 겸직 (max 2 jobs/bot) only when no spare exists or the
new job shares domain tokens. **Variant-name guard:** a new role that looks like a variant of an
existing one ("VFX 전문가" vs "VFX 아티스트") is blocked unless `new_role='yes'`. **Generalist
ban:** 풀스택/제너럴리스트 rejected (absorbs everything, becomes the parallel bottleneck).

### (3) Events / transitions
- **"First item = leader":** `create_project` sets `flow.project_team = _uniq([flow.leader] +
  assigned)` — the leader is prepended (header comment: "리더(첫 Organt)"). The leader is the
  driving identity of the Project/Task.
- recruit adds to `project_team` + `current.team`, joins the Task thread (`_add_members`),
  syncs the Discord role (best-effort, deferred for tentative hires), and persists via
  `_persist_job` once the bot works.
- Hard staffing brake: `consec_fail>=2` blocks recruiting (same unstable env → same failure;
  stops the "백엔드 6명" loop).

### (4) Actors / roles
Leader (recruits, picks Task members by their accumulated 직무기준/strengths), spare pool,
the System (assigns Discord roles, persists jobs).

### (5) Discord misfit
直군 = a **Discord custom role** assigned to the bot (`assign_job_role`); the *roster* is "bots
with role `예비`". Bots must be **created manually** in Discord (the explicit ADR-001 downside;
the 기획 wants foundation-model-registered custom employees instead). Personas live in CLAUDE.md
files, not in Discord. The "first member = leader" convention and 1봇1직업/겸직/variant rules are
runtime policy with only a thin Discord-role projection.

---

## 12. meet / vote — meetings and consensus rounds

**Code:** `guide_tools.meet` (2516–2622), `vote` (2424–2516); `_fork_collect` (377–419);
results → `collab_notes`; participation → `participated`.

### (1) What it IS
- **vote:** structured consensus — collect every member's choice + 2-line reasoning
  **simultaneously** (independent, anti-anchoring), then tally **by domain perspective**
  (same-직군 voting alike = 1 perspective, not N — homogeneous-model weighting guard).
- **meet:** round-robin meeting — **R1 simultaneous independent statements** (fork, anchor-free),
  **R2+ serial turn-taking** where each speaker sees prior remarks (blind agreement forbidden,
  reasons required); clamped 1–3 rounds; returns **minutes (회의록)**; leader converges.

### (2) Lifecycle / states
`_fork_collect` does a **fork-join with partial-join tolerance** (busy-elsewhere members are
skipped that round, not blocking the whole collection). Both append a record to
`flow.current.collab_notes` (clipped to 6000 chars) and mark `participated`; both `_ckpt` so the
meeting record survives a crash. Both are **detach-safe** (cancel → result appended to
`detached_results`).

### (3) Events / transitions
vote → `[표결] question / board / reasons` into `collab_notes`, tally returned ("N관점").
meet → `[회의] topic (R) / [1R]… / [2R]…` minutes into `collab_notes`, returned to the leader.
These notes are later injected into the **delegation contract** (§2.4) so consensus reaches the
implementer, and are referenced by the completion gates.

### (4) Actors / roles
Team members (vote/speak; non-leader participation tracked), Leader (calls, converges, records).
Meetings are explicitly **peer-to-peer** in spirit (`Communication.md`: "Peer to Peer에 가까운
소통").

### (5) Discord misfit
`Architecture/Feature.md` lists `vote`/`meet` as *future* "Discord 심화 대화 기능" — Discord has
no native meeting/vote object. They are simulated by SYS waking members in turn and posting their
speech as their own bot messages; the **minutes, the fork-join, the domain-perspective tally, and
the anti-anchoring round structure** are all runtime. `Status.md` forbids posting vote *tallies*
to the channel (tool-return only) — a direct symptom of the misfit.

---

## 13. The learning loop: experience → sleep distillation (수면 증류) → role_profiles (직무기준) → rubric injection

**Code:** `sys_core` — `_load/_save_profiles` (752–777), `_absorb_role_profiles` (1099–1147),
`pick_distill_job(s)` (1148–1167), `distill_role`/`_distill_role_inner` (1175–1242),
`craft_of` injection (`_craft_note` ~858, set on `flow`), env `ORGANT_DISTILL_MIN=5`,
`ORGANT_HYGIENE_AT=1100`, `_EXP_KEEP=12`. **Spec:** 기획 "기억 증류, 수면 / Skill 강화: 일하기
전 필요 학습, 일하며 쌓인 경험, 자기계발 시간에 보강."

### (1) What it IS
The mechanism by which an Organt's **work experience** becomes durable **craft standards**
(`role_profiles`, keyed by 직군) that are later **injected as quality rubrics** into work and
verification. "직업=기억"; the standard is written **by the expert themselves** (self-definition),
not mandated by the system.

### (2) Lifecycle / states (data shapes)
- `role_profiles: Dict[job → craft-standard text]` (≤~1500 chars), `role_experience:
  Dict[job → list of recent lessons]` (capped at 12), persisted atomically to
  `role_profiles.json`. On liftoff the data is lost and each expert regenerates on first task.
- **Capture:** every turn, `_absorb_role_profiles` parses the report for
  `[직무기준]JOB … [/직무기준]` and `[경험]JOB … [/경험]` blocks (filtering "없음" noise) and
  stores them.
- **Sleep / distillation:** when `len(role_experience[job]) >= 5` (or **hygiene distill** when a
  profile exceeds ~1100 chars even with 0 new experience), `distill_role` engages that expert on
  a `"__distill__"` pseudo-scope (so no flow can grab them) and `_distill_role_inner` asks them
  to compress current standard + raw experience ("원석") into ≤8 principles / ≤1000 chars,
  merging overlaps and discarding one-offs, returning a refreshed `[직무기준]` block. On success
  the experience log is cleared.

### (3) Events / transitions
Work → report with `[직무기준]`/`[경험]` blocks → absorb → (threshold) sleep-distill → refreshed
profile. **Injection:** `flow.craft_of = lambda job: role_profiles[job]`; the standard is added
to a worker's prompt (`_craft_note`) *before* working, and attached as a **scoring rubric** to
verification delegations and inside the `complete_task` cross-check gate ("'돌아가는가'가 아니라
'충분한가'").

### (4) Actors / roles
Each **expert** distills their *own* domain (found via `_bot_of_job`); SYS schedules distillation
during idle time; the rubric is consumed by workers and verifiers.

### (5) Discord misfit
There is **no Discord representation** of experience, sleep, distillation, or craft standards —
all of it lives in `role_profiles.json` and local session logs (`Status.md` insists role data
must **not** pollute the channel). "Sleep" is a scheduled background LLM call on a pseudo-scope.
The native SNS should model an Organt's **evolving competency/profile** as a first-class,
viewable entity.

---

## 14. parallel_work — bounded parallelism (the relaxation Feature)

**Code:** `guide_tools.parallel_work` (2622–2766); `_fork_collect`; `write_lease`; env
`ORGANT_FORK_FAN=3`. **Spec:** `Communication.md` 13–14 ("여럿(병렬)은 이 제약을 완화하는
Feature로 둔다"); `Feature.md` "병렬 작업 실행"; RFC-006.

### (1) What it IS
Delegates **several independent Work items to non-overlapping file regions simultaneously**
(parallel execution + serial integration). This is the *only* sanctioned relaxation of the
single-flow baton — and the relaxation is "different flows running concurrently," **never** two
alive bots inside one flow. Inter-flow safety is the **Engagement ledger** (one bot = one flow),
not a flow-count cap; natural concurrency ceiling = headcount.

### (2) Lifecycle / states
Validate (all in team, all job-assigned, non-empty files+body) → **mutual-exclusion gate**
(file paths must be disjoint) → set `write_lease[to]=paths` → `_fork_collect(Kind.WORK)` →
on join clear leases → first receiver becomes owner; `owner_delivered` if they worked. Width
2..FAN. **Currently disabled** (`_parallel_enabled=False`) due to workspace-sync + idle-non-fork
experts (gate 9) + write-lease churn (P-029); when off it returns "[병렬 비활성화]".

### (3) Events / transitions
Fork → per-assignee contract (Goal + exact write files + read-only elsewhere + no sub-delegation
+ report contract) → join → "통합·교차 검증·마감은 직렬로." Detach-safe.

### (4) Actors / roles
Leader (fans out), assignees (each owns a disjoint file region), then serial integration/verify.

### (5) Discord misfit
Parallelism contradicts Discord's free-for-all *and* the single-flow simulation; it is realized
by SYS forking multiple `wake`s with file leases. The "different flows concurrently, one bot one
flow" model and write-leases have **no Discord analog**. (Notably, the misfit pressure
*disabled* it in practice — a strong signal that the substrate is wrong.)

---

## 15. Persistence / recovery (checkpoint, boot recovery, parking)

**Code:** `sys_core` — `_checkpoint_open_task`/`_ckpt` (549–563), `_task_snapshot` (389–477),
`_restore_open_task` (620–750), `_resume_precise_chain` (1320–1375),
`communication.restore_chain`/`report_up_to`, `_save/_seed_file_owner` (565–618),
`reconcile_projects_from_discord` (316–361), `Engagement` (in-memory, self-healing).

### (1) What it IS
The machinery that makes the system **crash-safe**: snapshot the open Task at **every transition**
so a hard kill (SIGTERM/container reclaim) resumes the **same Task** with the **same owner,
ownership, gates, and delegation chain** — not a fresh start. "Parking" = a flow paused/queued
(its open Task checkpointed; the Status shows `⏸ 중단(미완 Task 이어가기 가능)`).

### (2) Lifecycle / states (snapshot shape)
`_task_snapshot` serializes: identity (task_id/thread_id/block_id); goal fields
(purpose/goal/owner/team/result); collaboration (collab_notes/acceptance/standard/interfaces/
participated/peer_info_pairs); **handoff facts** (work_delegated(_to), owner_delivered,
cross_checks/cross_checkers/last_verify_writes, act_by, contrib_checked, cross_check_offdomain,
loop_escalated); **completion gates** (`gate_pass`, delivered_pairs, redo_counts); execution
(run_count, evidence, cc_held, leader_writes, deploy_count/_writes/_once, writes_by_role); and
**precise resume** (`active_chain` = open frames, `last_work_body`, `precise_chain_frames`).
`verified` is **always reset to False** (false-completion backstop — fresh `run` evidence
required after recovery). Persisted to `projects.json` (per-project `open_task`), file ownership
to `file_owner`, jobs to `jobs.json`, profiles to `role_profiles.json`.

### (3) Events / transitions
- Boot → `_load_projects` (disk → seed fallback) → `reconcile_projects_from_discord`
  (priority **disk > topic > seed**).
- Resume → `_restore_open_task` rebuilds `flow.current` (re-merges team by union — never shrink),
  re-seeds file ownership from `audit.jsonl` (first-writer = owner), restores `_delivered`/
  `_redo_counts` (Redo-churn prevention). If a chain was open, `restore_chain` sets the baton to
  the **deepest worker**; `_resume_precise_chain` re-descends and unwinds C→B→A so each bot's
  scope is preserved (vs the old flatten that made one bot absorb another's work).
- Engagement is **in-memory** (restart = empty; recovery re-registers via `attach_engagement`)
  and **self-healing** (ghost holds on dead/finished scopes auto-clear on lookup).

### (4) Actors / roles
SYS (checkpoints, recovers, reconciles, reassigns dead leaders via `_valid_leader`).

### (5) Discord misfit
The **only durable Discord handle is the channel topic** (`[ORGANT:P-NNN] …`), so SYS reconciles
disk against parsed topic strings. All deep state lives in JSON files beside the process, not in
Discord. Precise-chain restoration explicitly notes it is **state restoration, not chat replay**
— Discord cannot represent a suspended call stack.

---

## 16. deploy / send_file — shipping the artifact

**Code:** `guide_tools.deploy` (2766–2877), `send_file` (2877–2914); `deploy.py`;
`sys_core._ensure_deploy` (1533–1575); `_deploy_infeasibility`, `deploy_service_name`.

### (1) What it IS
- **deploy:** publish a verified artifact for real (GitHub push + Render web-service create/update).
- **send_file:** send a workspace file to the User as an attachment (≤25MB; else deploy a URL).

### (2) Lifecycle / states
deploy guards: inflight-lock (`deploy_inflight`); **runaway cap** (`_deploy_count>=5` →
`deploy_capped`, escalate); **no-change-redeploy block** (writes unchanged since
`_deploy_writes`); **infeasibility pre-check** (`_deploy_infeasibility` — e.g. runtime Python on
Node-only Render → refactor to precomputed JSON / ONNX / TF.js); project-scoped deterministic
**service name** (`deploy_service_name` → `organt-p-NNN`); credentials (GH/RENDER env). Runs async
in a thread; sets `flow.deployed` (system-observed authoritative URL), `_deployed_once`,
`_deploy_count`; `_ckpt` immediately (count grows mid-Task). Handoff mode returns immediately and
SYS polls. **SYS-forced deploy** (`_ensure_deploy`) ships at flow-end if `package.json` exists,
deploy wasn't called, ≥1 Task completed, and creds exist (respecting the cap).

### (3) Events / transitions
`flow.deployed` is injected into the `complete_task` result as the **authoritative live URL**
(overriding bot-claimed URLs — URL-truth, §9).

### (4) Actors / roles
Leader (deploys; SYS monitors/forces), the User (receives the live URL / file).

### (5) Discord misfit
Deploy results and send-file go out as a (system) status/attachment, but per `Status.md` deploy
*results as internal events* must not be posted as system chatter — the authoritative URL is
surfaced through the Task `result`. There is no Discord object for "a deployment" or "an
artifact-with-provenance"; it's runtime state plus an attachment.

---

## 17. Communication rules (peer-to-peer, baton handoff) — Communication.md cross-cut

**Spec:** `Other/Rule/소통 단위/Communication.md`. **Code:** `communication.py`,
`_deliver` handoff, `permissions.py`.

Beyond §1, the Rule states cross-cutting communication invariants that the SNS must honor:
- **User-initiated only:** "흐름은 위(User)에서 SMS를 통해 시작된다. Organt는 흐름을 스스로
  시작하지 않는다." (No bot-initiated flows.)
- **Single liveness:** "살아 있는 Organt가 Request를 보내면, 보낸 Organt는 멈추고 받은 Organt가
  살아난다. 한 번에 하나의 Organt만 살아 있다."
- **Work busy-guard:** "Kind가 Work인 Request는 이미 미완의 Work를 들고 있는 Organt에게는 보낼
  수 없다. (작업 겹침과 순환 방지)."
- **LIFO unwind:** "Request는 보낸 역순으로 닫힌다." Flow ends only when all close, returning to
  origin.
- **Accept/Redo/escalate:** Work Response → Accept or Redo; Redo over limit → escalate up.
- **Peer-to-peer emphasis:** the Rule explicitly questions whether Discord can model meetings
  and leans P2P ("Peer to Peer에 가까운 소통"). Experts coordinate **directly** (owner↔owner Info,
  "리더 중계 금지").
- **Record fidelity (기록 충실성):** "시스템은 Organt의 발언을 침묵 절단하지 않는다 — 한도로 자를
  때는 잘렸음을 그 자리에 표기한다(회의록·표결 근거 포함). 동강난 주장으로 협의가 이어지면 협업
  자체가 부서진다." (Clipping must be *marked*, never silent — minutes/vote-rationale included.)

**Discord misfit:** P2P direct expert dialogue, the "one alive" rule, and marked-truncation are
runtime/convention; Discord neither enforces a single speaker nor preserves un-clipped speech by
design.

---

## Native-SNS implications (per primitive: what to make first-class)

For each Rule primitive, what a purpose-built platform should make **native** instead of the
Discord workaround:

| # | Primitive | Discord workaround (misfit) | Native SNS should make first-class |
|---|-----------|-----------------------------|-------------------------------------|
| 1 | **Single-flow LIFO baton** | Invisible runtime stack; bots can all post | A **Flow** object with an explicit **baton holder** + **Request stack**; a presence model where exactly one Organt is "active"; baton pass/unwind as native events |
| 2 | **Request (Work/Info)** | `[Request]` text block + mention + reply | A typed **Request** edge `{from,to,kind,body}` with a **non-blocking handoff** event; server enforces the 14 gates as transitions, not parsed text |
| 3 | **Response + Accept/Redo/markers** | `[Response]` reply + ✅/⚠️ reaction | A typed **Response** closing a Request, carrying a **result marker** enum (`accept/redo/incomplete/premature/refused/failed/clarify`) as a real field |
| 4 | **Status** | One edited message + `<t:unix:R>` | A **live Flow/Task presence entity** with a server-driven **heartbeat** + self-aging timestamp; "exactly one system message" becomes "a status surface," and internal events are *structurally* private (not posted) |
| 5 | **Project** | One channel + topic-string registry | A **Project** object `{id,leader,workspace,context,archive}` with native domain identity (number), Context and Archive as first-class fields |
| 6 | **Task** | One thread + `[Task-XXX]` block; thread=scope | A **Task** object with a real state machine + `TaskStatus` fields; **scope = Task object**, membership = Task.team |
| 7 | **set_goal / consensus ordering** | `Goal:` line + inferred `participated` | A **Goal/consensus object** that *records who agreed* and *blocks Work until confirmed* as a native precondition; acceptance/standard/interfaces as structured sub-objects verified at close |
| 8 | **Cross-check / circuit-breaker** | Inferred from who replies; one channel escalation | First-class **VerificationEvent**s (independent/off-domain), a **convergence counter** with an automatic **circuit-breaker → User-decision** event, and reverify-dedup as a server rule |
| 9 | **complete_task gates** | Edit block to `완료`; gates invisible | **Completion = a gated transition** with explicit, queryable **gate states** (verified/acceptance/cross-check/standard/interface/contrib); "earned by evidence" surfaced to the User |
| 10 | **Ownership / domain / coordination queue** | PreToolUse hooks; prompt-injected queue | Native **Ownership** (single accountable owner per artifact), **domain boundaries**, an **anti-absorption** rule engine, and a first-class **cross-domain coordination queue** routed to the leader |
| 11 | **recruit / 직군 / leader** | Discord custom roles; manual bot creation; leader=first member by convention | An **Organt roster** with typed **Job/직군** assignment (1봇1직업, 겸직≤2, variant-guard, generalist-ban), **work-earns-persistence**, and an explicit **Leader** role on Project/Task |
| 12 | **meet / vote** | Future "Discord 심화 기능"; waked turns; tallies hidden | Native **Meeting** (R1-parallel/R2+-serial, with **Minutes**) and **Vote** (simultaneous, **domain-perspective tally**) objects; results auto-linked to the Task's consensus record |
| 13 | **Learning: experience→sleep→role_profiles→rubric** | Local JSON only; nothing in Discord | A first-class **Competency/RoleProfile** entity per 직군 (expert-authored, distilled on "sleep"), viewable and **auto-injected as a quality rubric** into Work/verification |
| 14 | **parallel_work** | Forked wakes + file leases; disabled in practice | Native **concurrent Flows** with a **per-bot exclusivity ledger** (one bot=one flow) and **write-leases** as real resources — so parallelism is safe enough to *stay enabled* |
| 15 | **Persistence / recovery / parking** | JSON files + channel-topic reconcile | Durable, transactional **Flow/Task state** with per-transition checkpoints, **precise call-stack restoration** (not chat replay), **parking** as an explicit Flow state, and a self-healing engagement ledger |
| 16 | **deploy / send_file** | Attachment / status; results suppressed | A first-class **Deployment** (provenance, authoritative URL, runaway-cap, no-change/infeasibility guards) and **Artifact** object linked to the Task |
| 17 | **Communication rules** | Conventions + hooks | Enforce **user-initiated-only**, **single-liveness**, **P2P direct expert edges**, and **marked-truncation (no silent clipping)** as platform invariants |

### The seven most important design implications (build priorities)

1. **Model the Flow as a first-class object with an explicit single baton + LIFO Request stack.**
   This is the spine of the entire Rule. The platform must natively own "exactly one active
   Organt," sleep/wake, LIFO unwinding, `escalate`, `report_up_to` (upstream rewind), and
   `restore_chain` (precise resume) — none of which Discord can express. Everything else hangs
   off this.

2. **Make Request/Response typed edges with `Kind` and a `result marker`, and a native
   non-blocking handoff.** Replace `[Request]`/`[Response]` text parsing with structured events.
   The non-blocking handoff (delegate → return immediately → SYS drives the worker out-of-band →
   resume with result) is essential to keeping the single-flow model stable; it should be a
   platform capability, not a CLI-cancellation workaround.

3. **Treat goal-consensus and completion as gated state transitions, not assertions.**
   `set_goal` (consensus → distribution ordering, with recorded `participated`/coverage) and the
   ten `complete_task` gates (verified, acceptance, cross-check, standard, interface, contribution)
   are where *quality is structurally forced*. Expose these gates as first-class, queryable
   preconditions so "completion is earned by evidence" is visible and enforced by the server.

4. **Make ownership, domain boundaries, and anti-absorption native.** Single accountable **owner
   per artifact** (owner-by-receipt), **domain/직군** as typed metadata, the **leader-monopoly /
   absorption / stuck-worker blocks**, and the **cross-domain coordination queue** (routed to the
   leader) are currently bolted on via PreToolUse hooks and prompt injection. A purpose-built
   platform should enforce these as ownership rules on artifacts and a real coordination queue.

5. **Make verification first-class, with independence accounting and an automatic circuit-breaker.**
   Cross-check (independent/off-domain counting), reverify-dedup, and the 수렴 경보 circuit-breaker
   (escalate-once-to-User **and halt** at the convergence threshold; `cc_held` thrash escalation)
   are how the system avoids infinite loops the bots can't self-detect. Model VerificationEvents,
   a convergence counter, and a User-decision escalation as native constructs.

6. **Make the learning loop (experience → sleep-distillation → role_profiles → rubric injection) a
   first-class competency system.** "직업=기억": each 직군 has an expert-authored, distilled
   **craft standard** that is injected as a quality rubric into work and verification. This is
   entirely invisible in Discord (local JSON). A native SNS should expose evolving Organt
   competencies as viewable entities and wire rubric injection into Work/verification transitions.

7. **Make state durable and parallelism safe by construction.** Persist Flow/Task state
   transactionally (per-transition checkpoints; precise call-stack restoration; **parking** as an
   explicit state; self-healing **engagement ledger** for one-bot-one-flow exclusivity). Native
   durability + the exclusivity ledger + write-leases are what would let **parallel_work** (the
   sanctioned relaxation) stay enabled instead of being disabled under Discord/workspace-sync
   pressure — turning "concurrency limited only by headcount" from aspiration into a safe default.

---

### Appendix — primitive ↔ source map (quick index)

| Primitive | Spec doc | Code (primary) |
|-----------|----------|----------------|
| Communication / baton | `Rule/소통 단위/Communication.md` | `communication.py` (`CommunicationManager`) |
| Request | `Rule/소통 단위/Request.md` | `protocol.Request`, `guide_tools.request` (737) |
| Response | `Rule/소통 단위/Response.md` | `protocol.Response`, `communication.respond`, `_deliver` |
| Status | `Rule/소통 단위/Status.md` | `protocol.TaskStatus`, `sys_core._status_text` (480) |
| Project | `Rule/작업단위/Project.md`, `RFC-004` | `sys_core._register_project`, `guide_tools.create_project` |
| Task | `Rule/작업단위/Task.md`, `RFC-004` | `guide_tools.create_task/complete_task`, `Flow.TaskRef` |
| set_goal | (Task.md Goal/Team) | `guide_tools.set_goal` (1684) |
| cross-check / circuit-breaker | (RFC-008 quality) | `guide_tools.request` (1245–1290), `complete_task` (2106) |
| complete_task | (Task.md flow) | `guide_tools.complete_task` (1955) |
| ownership / coordination | (Task.md, Core.md hooks) | `permissions.py` (136–417), `_auto_coordinate` |
| recruit / 직군 / leader | Architecture, 기획 | `guide_tools.recruit` (1329), `create_project` leader pick |
| meet / vote | `Architecture/Feature.md` | `guide_tools.meet` (2516), `vote` (2424), `_fork_collect` |
| learning / distillation | 기획 (수면·증류·Skill) | `sys_core` `distill_role` (1175), `_absorb_role_profiles` (1099) |
| parallel_work | `Communication.md` 13–14, RFC-006 | `guide_tools.parallel_work` (2622) |
| persistence / recovery | (crash-safety) | `sys_core._checkpoint/_restore_open_task`, `restore_chain` |
| deploy / send_file | (shipping) | `guide_tools.deploy` (2766) / `send_file` (2877), `deploy.py` |

> **Note on doc/code divergence.** The Obsidian vault's RFCs stop at `RFC-004` and several
> referenced RFCs (RFC-005..008) and standalone Architecture/Communication "rich spec" files
> named in the brief **do not exist as files** — those numbers survive only as **inline
> citations inside the code** (e.g. "RFC-005: 검증 신호는 연속적이어야 한다", "RFC-006" parallel,
> "RFC-008 P0" rubric injection, "RFC-010 P3/P5" divergence→convergence). Treat the **code as the
> ground truth** for those concepts; this spec sources them from the code accordingly.
