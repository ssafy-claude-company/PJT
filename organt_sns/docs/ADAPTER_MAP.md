# Organt Brain ↔ Medium Adapter Map

> **Purpose.** Map the exact boundary between the **Organt "brain"** (the Python multi-agent
> collaboration engine in `src/`) and its current communication medium (**Discord**). This is the
> contract a custom social platform (`organt_sns`) must satisfy to be a **drop-in replacement** for
> Discord, plus the richer native events it should *also* expose.
>
> All file:line anchors are against the real code as of this audit. Paths are absolute.
>
> **Brain side (medium-agnostic):**
> - `src/sys_core.py` — `Sys` orchestrator (flow lifecycle, baton, recovery, event log `_log`).
> - `src/communication.py` — `CommunicationManager` (pure baton/LIFO stack, no network).
> - `src/guide_tools.py` — the 12 MCP `guide` tools workers call + the `Flow` object.
> - `src/protocol.py` — message encode/parse (`[Request]`/`[Response]`/`[Task-XXX]`).
> - `src/permissions.py` — PreToolUse gates (emit `tool_denied`).
> - `src/audit.py` — `AuditLog` + PostToolUse `tool_use` hook.
>
> **Medium side (the swappable seam):**
> - `src/discord_guide.py` — `DiscordGuide` ("Guide"): the transport. **This is the object the
>   custom SNS replaces.** The brain holds it as `flow.guide` / `self.guide` and never imports
>   `discord` outside this file + `src/channels.py` + `src/main.py` (the Discord event loop).
> - `src/main.py` — the Discord client wiring + `on_message` ingestion + boot recovery (the INPUT
>   side of the seam; the custom SNS replaces this listener).
> - `src/channels.py` — channel-id resolution helper (Discord-specific).
>
> **One-line model:** the brain speaks to the medium through **one object (`guide`)** for OUTPUT
> and is fed by **one handler (`on_message` → `route_channel_request`)** for INPUT. Everything else
> (baton, gates, recovery, growth) is medium-agnostic and emits to `logs/flow.jsonl` via one logger
> (`Sys._log`). Replace `DiscordGuide` + the listener and the brain is portable.

---

## 0. The seam at a glance

```
                         ┌─────────────────────────── BRAIN (medium-agnostic) ───────────────────────────┐
  Discord                │                                                                                │
  ────────  on_message   │   route_channel_request ──▶ handle_user_input ──▶ Flow + CommunicationManager  │
  message ──────────────▶│   (sys_core 2176)            (sys_core 1660)        (guide_tools 528 / comm.py) │
  (main.py 442)          │                                      │                        │                │
                         │                                      ▼                        ▼                │
  attachments ──────────▶│                                 12 MCP "guide" tools     Sys._log ──▶ flow.jsonl│
  (main.py 484)          │                                 (guide_tools 723)        (sys_core 779)         │
                         │                                      │                                          │
  boot recovery ────────▶│                                      │   AuditLog.record ──▶ audit.jsonl        │
  (main.py 508)          │                                      │   (audit.py 17; tool_use/tool_denied/    │
                         └──────────────────────────────────────┼──────────  user_request)  ──────────────┘
                                                                 ▼
                         ┌──────────── MEDIUM (DiscordGuide = `guide`, the swappable transport) ───────────┐
  Discord  ◀─────────────  guide.post / send_request / send_response / open_task / update_status /         │
  server                   send_file / react / assign_job_role / add_thread_members / set_channel_topic /  │
  (people see)             create_project_channel / set_nick / get_member_* (read-back)                    │
                         └────────────────────────────────────────────────────────────────────────────────┘
```

The brain **pushes** structured messages out through `guide.*` and **logs** every collaboration
state-change to `flow.jsonl`. A native SNS can (1) implement the `guide` surface to receive the same
pushes, and (2) subscribe to the event log (or an in-process hook on `Sys._log`) to render rich live
state Discord never had.

---

## (a) INPUT interface — how user requests ENTER the brain

### A.1 Transport-level wiring (`src/main.py`, the listener the SNS replaces)

- **System bot only** registers the handler: `@system_client.event async def on_message(message)`
  (`main.py:441`). Worker/Organt bots never receive messages; they only post/type.
- Privileged intents on the system client: `message_content=True, members=True` (`main.py:281`).
- **`guild_id`** is derived at runtime from `channel.guild.id` and injected into `Sys`
  (`main.py:362`). There is no `guild_id` config field; `CHANNEL_ID` (the main channel) is the only
  configured id (`src/config.py:48`).
- **`leader_id`** (default `To:` fallback) = the first worker bot that connects (`main.py:299`).

### A.2 `on_message` control flow (`main.py:442–506`)

In order:
1. **Ignore canary channel** — `if message.channel.id == canary["ch"]: return` (`main.py:444`).
2. **Ignore own + worker bots** (flows start from *User* only) —
   `if message.author.id in organts or message.author.id == system_client.user.id: return`
   (`main.py:447`). Filters by *known ids*, not a generic `.bot` flag.
3. **Classify channel** (`main.py:449–451`):
   ```python
   ch = message.channel.id
   is_project = ch in sysm.projects        # registered project channel (registry dict)
   is_main    = (ch == cfg.channel_id)     # the main #test channel
   ```
4. **Parse** → `parse(...)` (see A.3).
5. **Flow-start vs intervention branch** (see A.4).
6. **Per-session dedup** — `if str(message.id) in seen: return; seen.add(...)` (`main.py:477`).
   `seen` is in-memory only (`main.py:414`).
7. **Default recipient** — `if req.to_id is None: req.to_id = sysm.projects[ch]["leader"] if is_project else leader_id` (`main.py:480`).
8. **Attachment capture** (see A.5).
9. `audit.record("user_request", to=req.to_id, body=req.body[:200])` (`main.py:493`).
10. If project channel, accumulate feedback: `sysm.record_user_feedback(ch, req.body)` (`main.py:497`).
11. **Dispatch into brain** — `await sysm.route_channel_request(ch, req)` (`main.py:502`).

### A.3 `parse` — Discord primitive → structured message (`src/protocol.py:112`)

```python
def parse(*, message_id, author_id, mention_ids: List[int], reply_to_id,
          content: str) -> Optional[Union[Request, Response]]:
```
Called as (`main.py:458`):
```python
req = parse(message_id=str(message.id), author_id=message.author.id,
            mention_ids=[m.id for m in message.mentions],
            reply_to_id=(message.reference.message_id if message.reference else None),
            content=message.content)
```
Return rules:
- empty content → `None`.
- first line `[Response]` **and** `reply_to_id is not None` → `Response(body, from_id, replies_to=str(reply_to_id), message_id)`.
- first line `[Request]` → `Request(to_id=mention_ids[0] if mention_ids else None, kind, body, from_id, message_id)`. `kind`= `Kind.WORK` if the `Kind:` field starts with "work" else `Kind.INFO`. **Only the first mention** becomes `to_id`.
- else → `None`.

`Request`/`Response` dataclasses are in `protocol.py:23/34`. `Request.attachments: list` (`protocol.py:30`) holds `[(filename, bytes), ...]` — **`parse()` never fills it**; the listener does (A.5).

### A.4 What triggers FLOW-START vs PROJECT-CHANNEL INTERVENTION

The branch is **inside `on_message`** (`main.py:468–476`), *not* inside `route_channel_request`:
```python
if not isinstance(req, Request):
    # plaintext trigger is ONLY for registered project channels;
    # main(#test)/arbitrary channels accept the [Request] form only.
    if is_project and (message.content or "").strip():
        req = Request(to_id=None, kind=Kind.WORK, body=message.content.strip(),
                      from_id=message.author.id, message_id=str(message.id))
    else:
        return   # ignored: main/arbitrary channels need "[Request] To: @bot"
```
- **NEW FLOW START** (main #test or any arbitrary channel the bot can see): requires the structured
  `[Request]` form (i.e. `parse()` returned a `Request`). **Plaintext in the main channel is
  ignored** (chatter must not misfire a flow). `is_main` is computed but is *not* a positive gate —
  main and arbitrary channels share the "structured-only" rule.
- **PROJECT-CHANNEL INTERVENTION**: `parse()` returned non-`Request` **and** `is_project and content.strip()` → a synthetic `Request(to_id=None, kind=Work, body=<plaintext>)` is fabricated.
  **This is where the medium model is bent: bare plaintext in a registered project channel == an
  intervention into the running/closed project.**
- Distinguishing main vs project is `ch in sysm.projects` (dict lookup) vs `ch == cfg.channel_id`
  (id equality).

### A.5 Attachments / files (`main.py:484–492`)

```python
for att in (getattr(message, "attachments", None) or []):
    if int(getattr(att, "size", 0) or 0) > 25 * 1024 * 1024:   # skip > 25MB
        continue
    req.attachments.append((att.filename, await att.read()))    # (str, bytes)
```
- Data shape: **`(filename: str, bytes)`** tuples appended to `Request.attachments`.
- Forwarded: `route_channel_request` → `handle_user_input(..., attachments=...)`. The brain stages
  them into the workspace inbox (`inbound_files_staged` flow event, `sys_core.py:385`) so workers
  can `Read`/`run` them.

### A.6 Brain entry points

`src/sys_core.py:2176`:
```python
async def route_channel_request(self, channel_id, request: Request, root_id=None) -> dict:
    if request.to_id is None:
        self._log("ignored", reason="To 없음"); return {"mode": "ignored"}
    return await self.handle_user_input(channel_id, request.to_id, request.body,
                                        root_id=request.message_id,
                                        attachments=getattr(request, "attachments", None))
```
`src/sys_core.py:1660`:
```python
async def handle_user_input(self, channel_id, leader_id, user_text,
                            root_id=None, attachments=None) -> dict:
```
- `channel_id` registered (`self.projects.get(int(channel_id))`) ⇒ **intervention** (keeps sessions/
  team/delegation memory; may reassign leader). Unregistered ⇒ **fresh request** with a unique scope
  `f"new-{ms}"` so it runs in parallel.
- Entry-gate returns `{"mode": "queued", "queued": n}` when the leader/scope is busy or `max_flows`
  is hit (`sys_core.py:1674–1691`); otherwise builds `Flow(self.guide, channel_id, self.guild_id,
  lead, self.bot_info)` and runs it.

### A.7 Boot-recovery re-processing (`main.py:508–632`)

On startup the listener replays **unanswered** user requests so a restart doesn't drop work.
- **Channels scanned**: `[cfg.channel_id] + [every registered project channel]` (`main.py:517`).
- For each: `recent = guide.read_thread(ch, limit=30, include_plain=(ch in projects))` (`main.py:522`).
  `read_thread` (`discord_guide.py:470`) returns history in **chronological** order; with
  `include_plain` it wraps plaintext as `Request(to_id=None, ...)`.
- **"Unanswered" rule** — `find_pending_request(messages, known_ids)` (`main.py:92`): the **last user
  `Request` not followed by any `Response`**. Bot-authored requests are skipped; any `Response`
  clears pending (treated complete).
- Already-graduated requests (origin matched a registered project) are routed as interventions, not
  re-fired (`graduated_project`, `main.py:105`). Lingering open tasks are resumed.
- **Synthetic message id** for resumes: `message_id="recover-open-%s" % (id or channel)` →
  e.g. `recover-open-P-010` (`main.py:592`). The medium must tolerate a **non-numeric reply target**:
  `discord_guide.py:84` degrades a non-digit `reply_to` to a plain post.
- Convergence-parked tasks (`open_task.loop_escalated`) are **not** auto-resumed (await human).
- Dispatch is concurrent (`asyncio.gather` of `route_channel_request`, `main.py:630`).

---

## (b) OUTPUT / visibility interface — how actions become VISIBLE

### B.1 The Guide object (`DiscordGuide`, `src/discord_guide.py`)

The brain holds it as `flow.guide` (set in `Flow.__init__`, `guide_tools.py:531`) and `Sys.guide`.
Inside tools it is aliased `g = flow.guide` (`guide_tools.py:724`). **This is the complete set of
methods the medium must provide.** Signatures are exact.

| Method | Signature | Social meaning |
|---|---|---|
| `post` | `async post(channel_id, sender_id, content, reply_to=None) -> str` | Generic message **as bot `sender_id`** (falls back to system bot). Returns first message id (`"0"` on failure). Auto-splits > 2000 chars. The universal emit. |
| `send_request` | `async send_request(thread_id, sender_id, to_id, kind, body) -> str` | **Delegation announcement.** Posts `format_request(to_id, kind, body)` in the Task thread as the *requester* bot. |
| `send_response` | `async send_response(thread_id, sender_id, request_msg_id, body) -> str` | **Result / answer.** Posts `format_response(body)` as a **reply** to the request, as the *responder* bot. |
| `open_task` | `async open_task(channel_id, status: TaskStatus) -> (block_id, thread_id)` | **Task created.** Posts the `[Task-XXX]` status block (system bot) and spawns a Thread `Task-{id}`. |
| `update_status` | `async update_status(channel_id, status_msg_id, status) -> str` | **Task state changed.** Edits the status block in place (Purpose/Goal/Owner/Group/result). |
| `send_file` | `async send_file(channel_id, path, sender_id=0, caption="") -> str` | **Deliverable shipped to user.** Uploads a workspace file as an attachment, as bot `sender_id`. |
| `react` | `async react(channel_id, message_id, emoji) -> None` | **Status glyph** on a message (✅ done / ⚠️ crashed). best-effort. |
| `add_thread_members` | `async add_thread_members(thread_id, member_ids) -> None` | **Team membership** = who is on the Task (thread membership). |
| `assign_job_role` | `async assign_job_role(guild_id, user_id, job_name) -> bool` | **Job/직군 badge.** Creates/grants a role named after the job (splits "·" co-roles), removes stale job roles. |
| `set_nick` / `set_nicks` | `async set_nick(guild_id, user_id, nick) -> bool` | **Human name** of a bot (server nickname ≤32 chars). |
| `create_project_channel` | `async create_project_channel(guild_id, name) -> int` | **New project space** = a brand-new channel. |
| `get_or_create_channel` | `async get_or_create_channel(guild_id, name) -> int` | System channel (canary) get-or-create. |
| `set_channel_topic` / `get_channel_topics` | `async set_channel_topic(channel_id, topic) -> bool|None` | **Persistent registry record** in the channel topic (survives log loss; `None` = dead channel/404). |
| `edit_message` / `delete_message` | `async edit_message(channel_id, message_id, content)` | System-bot message edit/cleanup (canary anchor, status finalize). |
| `hide_channel` | `async hide_channel(guild_id, channel_id) -> None` | Hide an internal channel from people (`@everyone` view off). |
| `typing` | `async with guide.typing(channel_id, sender_id=None):` | **"…is typing"** presence while a bot works (visibility; best-effort). |
| read-backs | `get_member_jobs`, `get_member_nicks`, `get_custom_role_names`, `get_guild_bot_nicks`, `get_channel_topics`, `not_in_guild` | Identity/registry **recovery source-of-truth** from the server (survives container reclaim). |
| `register_organt` | `register_organt(user_id, client) -> None` | Register a sender identity (bot → client). |

Payload formats (`src/protocol.py`):
```
format_request (56):     [Request]\nTo: <@{to_id}>\nKind: Work|Info\nBody: {body}
format_response (61):    [Response]\nBody: {body}
format_task_status (65): [Task-{id}]\nPurpose: …\nStatus: …\nGoal: …\nOwner: …\nGroup:\n- <@id>: {info}…\n- result: {…}
```

### B.2 Every emit call-site and what it represents SOCIALLY

**SYS-level emits (`src/sys_core.py`):**
- `guide.post(flow.user_channel, lead, format_response(result), reply_to=flow.root_id)`
  (`sys_core.py:2082`) — **the final `[Response]` to the user** (the leader's report; closes the
  origin frame). This is the canonical "answer to the user".
- `guide.edit_message(status_ch, status_mid, ...)` (`sys_core.py:1925/1950/2094`) — the **side status
  line** for a flow (`✅ 완료` / `⏸ 중단`) edited as the flow progresses/finishes.
- `guide.post(channel_id, 0, self._status_text(...))` (`sys_core.py:1932`) — posts that status line
  (system bot).
- `guide.post(flow.user_channel, 0, "[배포 중단 — 런어웨이 차단] …")` (`sys_core.py:1557`) —
  **deploy runaway alert** to the user (5-deploy cap).
- `guide.post(cfg.channel_id, system_client.user.id, "[원터치 초대 필요] …")` (`main.py:357`) —
  **one-touch invite** announcement for uninvited bots.
- `guide.set_channel_topic(channel_id, topic)` (`sys_core.py:292`) — persist project registry to topic.

**Worker-tool emits (`src/guide_tools.py`), via `g = flow.guide`:**
- `g.send_request(thread_id, me_id, to, kind, body)` (`guide_tools.py:1025`) — **DELEGATION
  ANNOUNCEMENT** (`[Request]` in thread, from requester, mentioning target).
- `g.send_response(thread_id, to, req, result)` (`guide_tools.py:1158`) — **the recipient's
  `[Response]`** posted back into the thread (attributed to the *recipient* bot), as a reply.
- `_react(g, thread_id, req, "✅"|"⚠️")` (`guide_tools.py:1159`) — **verification/health glyph** on
  the delegation.
- `_say(who, text)` = `g.post(current.thread_id, who, text)` (`guide_tools.py:727`) — posts an
  opinion **as the speaker bot** (so meetings/votes look like real discussion, not a leader
  broadcast). Used by:
  - `vote` → `_say(v, "[표] {pick} — {reason}")` (`guide_tools.py:2482`) — **a vote cast** (one per voter).
  - `meet` → `_say(m, "[회의 1R] …")` / `"[회의 {r}R] …"` (`guide_tools.py:2556/2591`) — **a meeting opinion** per round.
  - `parallel_work` → `_say(m, "[병렬 보고] …")` (`guide_tools.py:2732`) — **a parallel branch report**.
- `g.open_task(ch, status)` (`guide_tools.py:1660`) + `_add_members(...)` (`guide_tools.py:1661`) —
  **a Task is opened** (status block + thread + team membership).
- `flow.refresh()` → `g.update_status(...)` (`guide_tools.py:620`) — **status block edit** on
  owner/goal/group/completion change.
- `g.assign_job_role(...)` (`guide_tools.py:1409/1484`) + `_add_members(...)` (`guide_tools.py:1496`) —
  **recruitment** (role badge + thread add).
- `g.create_project_channel(...)` (`guide_tools.py:1594`) — **a new project channel appears**.
- `g.send_file(flow.user_channel, full, sender_id=me_id, caption=…)` (`guide_tools.py:2901`) —
  **a file delivered to the user**, as the leader bot.
- `flow.guide.post(flow.user_channel, 0, "[수렴 경보 — 사람 판정 필요] …")` (`guide_tools.py:1280`) —
  **convergence/loop circuit-breaker alert** to the user.

**The unwind contract.** Tool *return values are not Discord events* (they go back to the calling
agent) — except the structured posts above. The user-facing report happens when the whole flow
unwinds to origin: `sys_core.py:2082`. Intermediate worker results are made visible by
`send_response` inside the `request` tool (`guide_tools.py:1158`).

---

## (c) The guide MCP tools (`src/guide_tools.py`) — tool → social event

**Registration.** `claude_agent_sdk.tool(name, description, schema)`; all built in
`make_guide_tools(flow, me_id, role)` (`guide_tools.py:723`), served by
`create_sdk_mcp_server("guide", "1.0.0", ...)` (`guide_tools.py:2914-2915`). Exposed as
`mcp__guide__<name>`. **Exactly 12 tools** (grep `@tool(` confirms no others).
Allowlists (`guide_tools.py:42–51`): `FLOW_TOOLS=[request,recruit,run]`,
`LEADER_TOOLS=[create_project,create_task,set_goal,complete_task,deploy,send_file,vote,meet,parallel_work]`.

| Tool (line) | Who | Parameters | What a human SEES + payload |
|---|---|---|---|
| **request** (737) | all | `{to_id:str, kind:str, body:str}` | **Delegation** `[Request]` in Task thread as requester (`send_request`, 1025); on completion the recipient's `[Response]` reply (`send_response`, 1158) + ✅/⚠️ react (1159); first Work recipient becomes **Owner** (status block edit); if `cross_checks ≥ limit`, **convergence alert** to user channel (1280). |
| **recruit** (1329) | all | `{member:str, role:str, reason:str, new_role:str}` | **Role badge** created/assigned (`assign_job_role`, 1409/1484; deferred for tentative spare hires until first real work), Task **status-block Group** updated, member **added to thread** (1496). No chat message. |
| **run** (1502) | all | `{command:str}` | **NONE.** Shell exec in workspace (60s, denylist). Captured `evidence` is later embedded into `[Response]`/result payloads as "[실행 증거]". |
| **create_project** (1580) | leader | `{name:str, team:str}` | **A new Discord text channel appears** (`create_project_channel`, 1594), named after the project; project registered → `P-NNNN`. |
| **create_task** (1609) | leader | `{members:str}` | **`[Task-XXX]` status block + Thread created** (`open_task`, 1660), Purpose/Goal/Owner blank by design, Group = team; members **added to thread** (1661). |
| **set_goal** (1684) | leader | `{purpose:str, goal:str, acceptance:str, standard:str, interfaces:str}` | On success, **status block edited** so Purpose+Goal populate (Goal `---`→text) (`refresh`, 1891). All gate rejections are text-only (no medium event). |
| **complete_task** (1955) | leader | `{result:str}` | **Status block → 완료 with `- result:`** (verified live-URL line, cross-check note, `[보고]`, `[시스템 실행기록]`) (`refresh`, 2417) + ✅ react on the block (2418). |
| **vote** (2424) | leader | `{question:str, options:str, members:str}` | **Each voter's `[표] choice — reason` posted to thread under that voter's name** (`_say`, 2482); tally is by domain (homogeneous-model). Tally board returned to leader only. |
| **meet** (2516) | leader | `{topic:str, members:str, rounds:str}` (rounds 1–3, default 2) | **Each member's `[회의 NR] …` opinion posted to thread under their own name** (R1 simultaneous fork 2556; R2+ serial 2591). Minutes returned to leader only. |
| **parallel_work** (2622) | leader | `{assignments:str}` (JSON `[{to,files,body}]`) | **Disabled by default** (`[병렬 비활성화]`, 2638). When enabled: **each branch's `[병렬 보고] …` posted to thread under the worker's name** (`_say`, 2732). |
| **deploy** (2766) | leader | `{name:str}` (slot forced to `organt-p-00n`) | **Directly: NONE** (returns result string to agent). Indirectly the live URL reaches the user via the final `[Response]` (sys_core 2082) and the `complete_task` `result` ("[시스템 검증 — 라이브 URL(권위)]"); cap-trip alert posted by **sys_core**, not this tool (sys_core 1557). |
| **send_file** (2877) | leader | `{path:str, caption:str}` | **File attachment in the user channel, as the leader bot**, caption as message text (`send_file`, 2901). |

---

## (d) Discord concept → Rule concept mapping

| Discord concept | Maps to (Rule / brain) | Where / notes |
|---|---|---|
| **Main `#test` channel** (`CHANNEL_ID`) | Flow ingress for *new* requests (origin = User/SMS) | `cfg.channel_id`; `is_main` (`main.py:451`). Accepts **`[Request]` form only**; plaintext ignored. |
| **A project channel** | **A Project** (`Project-XXXX` / `P-NNNN`) | `ch in sysm.projects` (`main.py:450`). Created by `create_project` → `create_project_channel`. One project = one channel; new requests always open a **new** channel (`discord_guide.py:149`). |
| **Plaintext in a project channel** | **An intervention** into that project (continue/redo) | Bent model: synthetic `Request(Work, body=plaintext)` (`main.py:471`). Distinguishes "new request" (structured, main) from "steer the running project" (plaintext, project channel). |
| **A Thread** (off a `[Task-XXX]` block) | **A Task's conversation scope** | `open_task` (`discord_guide.py:443`). All `[Request]`/`[Response]` for a Task live in its thread. |
| **Thread membership** | **The Task team** | `add_thread_members` (`discord_guide.py:222`); set on create_task/recruit. |
| **`[Task-XXX]` status block message** | **Task state** (Purpose/Goal/Owner/Group/result/status) | `format_task_status`; edited via `update_status`. The block is the Task's authoritative state card. |
| **A Role** (custom, non-managed, no admin perms) | **A 직군 (job)**; co-jobs joined by `·` (max 2) | `assign_job_role` / `_is_job_role` (`discord_guide.py:258`). **Roles are the persistence source-of-truth for jobs** across container reclaim (`get_member_jobs`, 304). |
| **A server nickname** | **The bot's human name** | `set_nick` (`discord_guide.py:235`); persistence source-of-truth for names (`get_member_nicks`, 325). |
| **A mention `<@id>`** (first one) | **`To:` target** of a Request | `parse` takes `mention_ids[0]` (`protocol.py:127`). |
| **A reply (message reference)** | **`RepliesTo`** — which Request a Response closes; message id = identity | `parse` requires `reply_to_id` for `[Response]` (`protocol.py:120`). |
| **An emoji reaction** | **Verification/health glyph** (✅ done, ⚠️ crashed) | `react` (`discord_guide.py:213`); set in `request` (1159) and `complete_task` (2418). |
| **A file attachment (inbound)** | **User-supplied material** → workspace inbox | `(filename, bytes)` (`main.py:484`) → `inbound_files_staged` (`sys_core.py:385`). |
| **A file attachment (outbound)** | **A deliverable** to the user | `send_file` (`discord_guide.py:165`). |
| **Channel topic** | **Persistent project registry record** | `set_channel_topic` (`discord_guide.py:373`); recovers registry when `logs/` is lost (`project_restored_from_topic`). |
| **Canary channel `sys-canary`** | **Internal receive-watchdog** (NOT user-facing) | `get_or_create_channel` + `hide_channel` (`main.py:424`); excluded from `on_message` (`main.py:444`). Anchor message edited/observed for liveness. |
| **"…is typing" indicator** | **Bot-working presence** | `guide.typing(...)` (`discord_guide.py:104`). Pure visibility; best-effort. |
| **Message id (snowflake)** | **Message/request identity**; `root_id` threads the final reply | `parse` → `message_id`; recovery uses **non-numeric synthetic ids** (`recover-open-P-010`) the medium must tolerate (`discord_guide.py:84`). |
| **Bot identity (which client posts)** | **From = the acting Organt** | `post`/`send_request` choose `self.organts[sender_id]` so messages are attributed to the real bot, not a central system voice. |

**Where the medium model is bent (important for the SNS):**
1. **Plaintext-in-project-channel = intervention** (no native Discord concept; the brain overloads
   "a normal message" to mean "steer this project").
2. **Roles double as a database** (job persistence) and **nicknames as a name DB** — because
   `logs/` can be reclaimed; the SNS should provide first-class persistent identity instead.
3. **Synthetic non-numeric reply targets** during recovery — the medium must not assume reply
   targets are numeric/real.
4. **A "Task" is a (status-block message + its thread) pair** — two Discord objects bound by
   convention; an SNS can make Task a first-class entity.
5. **Opinions are posted "as the bot"** to fake real multi-party discussion in a flat channel — an
   SNS with native threaded/multi-speaker structure can render this directly.

---

## (e) The real-time event stream — `logs/flow.jsonl` & `logs/audit.jsonl`

Two append-only JSONL sinks. **These ARE the collaboration's event stream** a native SNS would
render live.

### E.1 Loggers

- **Flow logger** — `def _log(self, event, **f)` (`sys_core.py:779`): `rec = {"event": event,
  "ts": time.time(), **f}` → in-memory `self.flow_log` + appended to `self.flow_log_path`
  (`logs/flow.jsonl`, `sys_core.py:77`). **Only `event` + `ts` are auto-attached**; everything else
  is per-call-site. Injected into tools as `flow.log = self._log` (`sys_core.py:1900`), so both
  `self._log(...)` (sys_core) and `flow.log(...)` (guide_tools) write the same file. All event names
  are double-quoted literals (no computed names).
- **Audit logger** — `def record(self, event, **fields) -> dict` (`src/audit.py:17`): attaches `ts` +
  `event`, writes `logs/audit.jsonl` (instantiated `main.py:267`, path `config.py:42`).

> A native SNS should subscribe here — either tail the JSONL, or (cleaner) inject a sink so
> `Sys._log` / `AuditLog.record` also publish to the SNS event bus in-process.

### E.2 `audit.jsonl` — 3 event types (the full set)

| event | call site(s) | payload | meaning |
|---|---|---|---|
| `tool_use` | `audit.py:40` (PostToolUse hook) | `actor, role, tool, tool_input, tool_use_id` | every Organt tool call (who/what). ~24.5k live. |
| `tool_denied` | `permissions.py` ×13 (104,114,130,150,163,177,192,214,223,259,297,402,436) | `actor, role, tool, reason, tool_use_id` (+`path` at 114/130) | a PreToolUse **policy block** (the violation taxonomy — see reasons below). |
| `user_request` | `main.py:493, 620` | `to, body[:200]` | a user request entered the brain. |

`tool_denied` **reasons** (the gate taxonomy): `권한 밖 도구`, `작업공간 밖 경로`, `쓰기 리스 밖`,
`협의(Info) 중 선구현`, `위임된 owner 도메인 대리구현`, `개입 목표 미확정 선수정`, `리더 독식(위임
없이 단독 구현)`, `개입 Task 미개설 단독 실행`, `개입 위임없이 단독 run 독식`, `리더 흡수(팀 합보다
많이 doing)`, `타 직군 소유 파일 편집`, `타 도메인 전문가 일 흡수(전문가 idle)`, `막힌 동료 일
흡수(재요청 대신 대신함)`.

> Note: `sys_core.py:585` opens `audit.jsonl` **read-only** to replay `tool_use` Write/Edit records
> and reconstruct file authorship (`file_owner_seeded`). It is a reader, not a second writer.

### E.3 `flow.jsonl` — event catalog (83 in code; 58 observed live)

Grouped logically. `(R)` = present in the runtime log; counts are live occurrences.
Format: `event` — `src/file:line` — `{payload}`.

**Delegation / request handoff (baton edges):**
- `req_sent` (R, 894×) — guide_tools.py:1039 — `frm, to, kind, seg, redo, body[:60]`
- `req_busy_elsewhere` (R) — guide_tools.py:832 — `frm, to, holder, kind, seg`
- `req_rejected` (R) — guide_tools.py:840 — `frm, to, kind, alive, seg, reason`
- `req_failed` (R) — guide_tools.py:1213 — `to, consec, seg`
- `dup_parallel_merged` — guide_tools.py:814 — `frm, to, kind, seg`
- `handoff_nest_guard` — guide_tools.py:1084 — `to, depth`
- `delegation_detached` (R, 428×) — guide_tools.py:1324 / 2505 (`to="vote"`) / 2611 (`to="meet"`) / 2753 (`to="parallel"`) — `to, seg` (`to` ∈ {member-id, "vote", "meet", "parallel"})
- `await_inflight_delegation` (R, 605×) — sys_core.py:1250 — `n`
- `sys_auto_delegate` (R) — sys_core.py:1390 — `task, owner, leader_runs`
- `sys_auto_continue` (R) — sys_core.py:1292 — `task, owner, left`
- `auto_coordinate` (R) — sys_core.py:1433 — `to, frm`

**Baton recovery / whose-turn:**
- `baton_recover` (R, 85×) — guide_tools.py:1176 — `me, stuck_alive, to`
- `baton_recover_continue` (R, 90×) — sys_core.py:2001 — `alive, recovered`
- `owner_resumable_timeout` — guide_tools.py:1201 — `to, seg`
- `precise_resume_wake` (R) — sys_core.py:1348 — `worker, level, stack`
- `precise_resume_done` (R) — sys_core.py:1374 — `levels, alive, done`
- `precise_resume_failed` — sys_core.py:1977 — `err`
- `continue_incomplete` (R, 746×) — sys_core.py:2026 — `task, attempt, seg, progressed`

**Parallel fan-out / join:**
- `parallel_work` (R) — guide_tools.py:2716 — `n, to(csv), seg`
- `parallel_join` (R) — guide_tools.py:2740 — `n, seg`

**Domain / authorship guards:**
- `work_crossdomain_blocked` (R) — guide_tools.py:906 — `frm, to, my, to_jobs, caps, seg`
- `work_offdomain_blocked` (R) — guide_tools.py:934 — `to, caps, seg`
- `work_refused_offdomain` (R) — guide_tools.py:1226 — `to, need, seg`
- `owner_no_work` (R) — guide_tools.py:1238 — `to, seg`
- `authorship_concentration` (R, legacy*) — `task, top, share, roles`

**Task lifecycle / contribution:**
- `task_contrib_idle` (R) — guide_tools.py:2330 — `task, idle[]`
- `task_contrib_overridden` (R, 120×) — guide_tools.py:2393 — `task, idle[]`
- `task_absorbed_blocked` (R) — guide_tools.py:2350 — `task, absorbed[]`
- `task_solo_completed` — guide_tools.py:2386 — `task, owner`

**Goal-setting gates:**
- `set_goal_busy_consensus_hold` — guide_tools.py:1758 — `task, domains`
- `set_goal_consensus_coverage` (R) — guide_tools.py:1770 — `task, redundant[], uncovered_busy[]`
- `set_goal_gap_check` (R) — guide_tools.py:1796 — `task`
- `set_goal_staffing_gap` (R) — guide_tools.py:1829 — `task, gaps[]`
- `set_goal_depth_gap` — guide_tools.py:1851 — `task, caps[]`
- `set_goal_standard_set` (R) — guide_tools.py:1886 — `task, chars`
- `set_goal_decomp_check` (R, legacy*) — `task, domains`
- `set_goal_parallel_plan` (R, legacy*) — `task, doms`

**Completion / verification gates:**
- `acceptance_gate` (R, 49×) — guide_tools.py:2054 — `task, defined`
- `complete_percept_gate` (R) — guide_tools.py:2009 — `task, essential`
- `data_provenance_gate` (R) — guide_tools.py:2092 — `task, file, marker`
- `complete_thrash` — guide_tools.py:2143 — `task, holds`
- `standard_bind_gate` (R) — guide_tools.py:2264 — `task`
- `iface_dialogue_gate` (R) — guide_tools.py:2291 — `task`
- `loop_escalated_block` — guide_tools.py:861 — `to, cross`
- `loop_circuit_breaker` — guide_tools.py:1278 — `task, cross`
- `reverify_dedup` (R) — guide_tools.py:878 — `to, cross`
- `loop_escalated_cleared_by_user` — sys_core.py:1804 — `project, task`

**Agent growth / leveling / experience:**
- `role_earned` (R) — sys_core.py:1497 — `member, role` (job earned via first real work → Discord role)
- `role_experience_saved` (R, 1691× — most frequent) — sys_core.py:1143 — `job, lines`
- `role_distilled` (R, 226×) — sys_core.py:1233 — `job, used`
- `role_profile_saved` (R, 239×) — sys_core.py:1141 — `job, size`
- `role_distill_noop` (R) — sys_core.py:1241 — `job`
- `role_distill_failed` — sys_core.py:1225 — `job, err`
- `recruit_variant_blocked` — guide_tools.py:1367 — `asked, existing`

**Recruiting / leadership / project registry:**
- `project_reuse_denied_new_request` (R) — sys_core.py:223 — `existing, made`
- `project_name_uniquified` — sys_core.py:231 — `asked, made, existing`
- `project_channel_move_refused` — sys_core.py:240 — `project, kept, asked`
- `project_restored_from_topic` (R) — sys_core.py:342 — `project, channel`
- `project_updated_from_topic` — sys_core.py:350 — `project, channel`
- `project_leader_reassigned` — sys_core.py:1651 — `project, old, new, reason`
- `leader_reassigned` (R) — sys_core.py:1713 — `project, old, new`
- `channel_marked_dead` — sys_core.py:301 — `channel, project`
- `projects_seed_restored` (R) — sys_core.py:130 — `n`

**Deploy:**
- `deploy_cap` (R) — guide_tools.py:2785 — `count`
- `deploy_thrash` (R) — guide_tools.py:2805 — `writes`
- `deploy_infeasible` — guide_tools.py:2828 — `reason`
- `ensure_deploy` (R) — sys_core.py:1571 — `forced`
- `ensure_deploy_skipped_capped` — sys_core.py:1555 — `count`
- `ensure_deploy_failed` — sys_core.py:2079 — `err`

**Boot / recovery / files / intervention / lifecycle / errors:**
- `open_task_restored` (R, 99×) — sys_core.py:748 — `project, task, owner`
- `deep_chain_restored` (R) — sys_core.py:723 — `depth, deepest, task`
- `file_owner_seeded` (R) — sys_core.py:616 — `project, files`
- `inbound_files_staged` (R) — sys_core.py:385 — `files[]`
- `inbound_stage_error` — sys_core.py:387 — `err`
- `file_sent_to_user` — guide_tools.py:2906 — `path, size, seg`
- `intervention` (R, 126×) — sys_core.py:1876 — `project, text[:60]`
- `intervention_keep_sessions` (R, 126×) — sys_core.py:1704 — `project`
- `queued` (R) — sys_core.py:1679 — `text[:80], depth, scope, lead_busy`
- `reset_sessions` (R) — sys_core.py:802 — `cleared, scope`
- `agent_timeout` (R) — sys_core.py:1523 — `organt, role, sec`
- `agent_revive` — sys_core.py:1529 — `organt, attempt, err`
- `flow_idle_abort` — sys_core.py:1085 — `idle, timeout`
- `flow_idle_aborted` — sys_core.py:2070 — (none)
- `final_post_failed` — sys_core.py:2085 — `err`
- `ignored` — sys_core.py:2178 — `reason`
- `flow_done` (R, 65×) — sys_core.py:2129 — `project, tasks, comm_done`

\* **legacy** (`set_goal_decomp_check`, `set_goal_parallel_plan`, `authorship_concentration`) appear
in the live `flow.jsonl` but not in current source — emitted by an earlier code version. Include them
if the SNS must render everything the stream has ever produced.

### E.4 The baton/comm history (in-memory, `CommunicationManager.history`)

`src/communication.py` is pure logic (no I/O) but its `history` list is the **authoritative baton
ledger** and a natural SNS event source (it is what the flow events above describe). Tuples appended:
`("request", from, to, req_id, kind)`, `("respond", from, to, req_id, result)`,
`("redo", from, to, req_id, count)`, `("escalate", to, from, req_id, reason)`,
`("report_relay" / "report_up", …)`, `("clarify", …)`, `("restore_chain", depth, alive)`.
The single live invariant: **exactly one `alive` Organt at a time** (the baton holder); responses
close the stack **LIFO**; when empty the flow returns to `origin` (User) and ends. Global exclusivity
across flows is `Engagement` (bot → scope, `communication.py:46`).

---

## SUMMARY — Minimal adapter + richer native events

### The MINIMAL adapter the custom SNS must implement (drop-in Discord replacement)

**INPUT (feed the brain).** Provide a listener that, per inbound user message, calls
`Sys.route_channel_request(channel_id, Request(...))` with a `Request`/`Response` built like
`protocol.parse` (`to_id` = first mention/None, `kind` Work/Info, `body`, `from_id`, `message_id`),
plus:
- Distinguish **main channel** (structured `[Request]` only ⇒ new flow) from **project channel**
  (plaintext ⇒ intervention; structured ⇒ normal). Use `ch in Sys.projects` vs the configured main id.
- Capture attachments as `(filename: bytes)` tuples into `Request.attachments`.
- Filter the bots' own/worker messages; only *user* messages start flows.
- On boot, replay the **last unanswered user request per channel** (a `Request` with no following
  `Response`), tolerating **synthetic non-numeric message ids** (`recover-open-<id>`).

**OUTPUT (the `guide` object).** Implement the `DiscordGuide` surface the brain calls. The
**load-bearing minimum** (what actually fires at runtime) is:
- `post(channel_id, sender_id, content, reply_to=None) -> msg_id` — generic emit **as a bot identity**.
- `send_request(thread_id, sender_id, to_id, kind, body) -> msg_id` — delegation.
- `send_response(thread_id, sender_id, request_msg_id, body) -> msg_id` — result/answer (reply).
- `open_task(channel_id, status) -> (block_id, thread_id)` — create Task (state card + scope).
- `update_status(channel_id, status_msg_id, status) -> msg_id` — edit Task state.
- `create_project_channel(guild_id, name) -> channel_id` — new project space.
- `send_file(channel_id, path, sender_id, caption) -> msg_id` — deliverable to user.
- `react(channel_id, message_id, emoji)` — ✅/⚠️ glyph.
- `add_thread_members(thread_id, member_ids)` — Task team membership.
- `assign_job_role(guild_id, user_id, job_name) -> bool` + `set_nick(guild_id, user_id, nick) -> bool`
  — job & name identity (and their batch/read-back variants `get_member_jobs`/`get_member_nicks`/
  `get_custom_role_names`/`get_guild_bot_nicks` for recovery).
- `set_channel_topic` / `get_channel_topics` — persistent registry (or replace with a real DB).
- `read_thread(thread_id, limit, include_plain) -> [Request|Response]` — history for boot recovery.
- `typing(...)`, `edit_message`, `delete_message`, `hide_channel`, `get_or_create_channel`,
  `register_organt`, `invite_url`/`not_in_guild` — presence/system-channel/onboarding niceties
  (best-effort; can be stubbed initially).

Message payloads must match `protocol.format_request/format_response/format_task_status` **iff** you
also rely on `read_thread`+`parse` to re-ingest your own messages on boot. (If the SNS keeps native
typed state instead of re-parsing chat text, you only need the *semantics*, not the literal
`[Request]\nTo:…` strings.)

**EVENT INTAKE.** Subscribe to the brain's event stream — tail `logs/flow.jsonl` (`Sys._log`) and
`logs/audit.jsonl` (`AuditLog.record`), or inject an in-process sink. The minimal "parity" set to
render a working timeline: `req_sent`, `delegation_detached`, `flow_done`, `intervention`,
`open_task_restored`, plus `tool_use`/`tool_denied`/`user_request`.

### Richer native events/state the SNS should ALSO expose (Discord couldn't)

The brain already emits these; Discord could only show flat chat. A native SNS should render them as
first-class live state:

1. **Baton holder / whose-turn** — the single `alive` Organt (the baton) and the `seg`
   (leader-segment / turn counter). Source: `CommunicationManager.alive` + `req_sent {frm,to,seg}`,
   `req_busy_elsewhere {holder}`, `baton_recover{,_continue}`, `precise_resume_wake {worker,level,
   stack}`. **Live "who is speaking now" + turn number.**
2. **Delegation tree** — the request stack as a live tree. Source: `req_sent {frm→to}` edges,
   `delegation_detached {to ∈ member|vote|meet|parallel}`, `await_inflight_delegation {n}`,
   `report_relay`/`report_up` (comm.history), `parallel_work`→`parallel_join`. **Render the LIFO
   call-stack / org chart of the active flow.**
3. **Meeting rounds** — `meet`/`vote` structure: `delegation_detached to="meet"/"vote"`, per-speaker
   opinions (currently `_say` posts), `set_goal_consensus_coverage {redundant[], uncovered_busy[]}`.
   **Render rounds, per-domain opinions, and the tally as structured UI** (not free text in a thread).
4. **Verification state** — the cross-check / acceptance meters: `cross_checks` count via
   `acceptance_gate {defined}`, `complete_percept_gate`, `data_provenance_gate {file,marker}`,
   `standard_bind_gate`, `iface_dialogue_gate`, `reverify_dedup {cross}`, `loop_circuit_breaker
   {cross}`, `complete_thrash {holds}`. **A live "is this Task verified / how many rounds / parked
   for human?" indicator** (incl. the `loop_escalated` convergence alarm).
5. **Agent growth / leveling** — the per-bot career: `role_earned {member,role}` (earns a job from
   first real work), `role_experience_saved {job,lines}` (XP — most frequent event),
   `role_distilled {job,used}` (skill consolidation), `role_profile_saved {job,size}` (craft
   profile). **Per-agent level/XP/skill cards that grow over time** — 1-bot-1-job-1-memory identity
   is already persistent.
6. **Task state card** — Purpose/Goal/Owner/Group/acceptance/standard/result as a structured entity
   (today crammed into one editable `[Task-XXX]` message). Source: `open_task`/`update_status` +
   `set_goal_*` + `complete_task` result. **A real Task object with fields, owner, team, and a
   verified live-URL.**
7. **Team-health / authorship** — `authorship_concentration {top,share,roles}`,
   `task_contrib_idle/overridden {idle[]}`, `writes_by_role`. **Who actually contributed vs idle;
   single-author dominance warning.**
8. **Project registry & leadership** — `intervention`, `leader_reassigned`, `project_restored_from_topic`,
   `flow_done`, `queued {depth,scope,lead_busy}`. **A projects board with live status, current
   leader, and queue depth.**

These 8 are the payoff of the migration: the brain *already* produces this state every run, but
Discord can only flatten it into chat + role badges + one edited status message. A purpose-built SNS
can subscribe to `Sys._log` and render the baton, the delegation tree, meeting rounds, verification
progress, and agent growth as live, first-class views.
