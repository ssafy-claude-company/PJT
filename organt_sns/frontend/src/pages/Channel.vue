<script setup>
import { ref, onMounted, onUnmounted, watch, nextTick, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '../api'
import { kindMeta, timeFmt, dayKey, dayLabel } from '../kinds'
import CollabPanel from '../components/CollabPanel.vue'
import ArticlePanel from '../components/ArticlePanel.vue'
import Icon from '../components/Icon.vue'
import { monogram, avatarColor, avatarBg } from '../avatar'
import { askPrompt, askConfirm } from '../dialog'
import { toast } from '../toast'
import { me } from '../user'

const route = useRoute()
const router = useRouter()
const menu = ref(false)
const data = ref(null)
const loading = ref(true)
const draft = ref('')
const sending = ref(false)
const briefing = ref(null)
const showBrief = ref(false)
const showStruct = ref(false)
const showArticle = ref(false)   // 산출물·작업 보드(배포/repo 링크 + Task)
const stats = ref(null)
const msgsEl = ref(null)
// 입력 모드: 사람 메시지(msg) vs 봇 요청(req, Work/Info 1급)
const mode = ref('msg')
const agents = ref([])
const reqTo = ref('')
const reqKind = ref('auto')     // 자동 분류(본문으로 W/I) 기본, 작업/질문은 수동 override
const reqBody = ref('')
const reqSending = ref(false)
// 적임자 추천(F1301) — 요청 작성 중 누구에게 맡길지 제안
const recs = ref([])
const recLoading = ref(false)
// 봇 선택(커스텀 드롭다운)
const pickerOpen = ref(false)
const reqToBot = computed(() => agents.value.find((b) => String(b.bot_id) === String(reqTo.value)))
function choose(b) { reqTo.value = b ? b.bot_id : ''; pickerOpen.value = false }

// 대화로 보일 종류. 그 외(work/raw 등 도구활동)는 흐름을 끊지 않게 뒤로 접는다.
const CONV = new Set(['delegation', 'consultation', 'goal_set', 'meeting', 'verification',
  'deploy', 'task_complete', 'recruit', 'agent_learned', 'convergence_alert', 'user_request', 'intervention'])
// 사람·SnsGuide(g/c 키) 메시지는 항상 대화. 이벤트(e 키)는 CONV 종류만 대화, work/raw는 활동.
const isConv = (m) => m.type === 'human' || (m.key && (m.key[0] === 'g' || m.key[0] === 'c')) || CONV.has(m.kind)
// 로그 접두("이름 → id: 본문")를 떼어 자연스러운 말로. 본문 없는 마커는 종류별 한마디로.
const VERB = { delegation: '맡겼어요', verification: '확인했어요', task_complete: '완료했어요', deploy: '배포했어요', goal_set: '목표를 정했어요', consultation: '물어봤어요', recruit: '합류했어요' }
function cleanLine(s, kind) {
  s = (s || '').trim()
  const arrow = s.search(/→/)
  if (arrow >= 0) {                                   // 폴백 요약("이름 → id: 본문")만 접두 제거
    const colon = s.indexOf(':', arrow)
    if (colon >= 0) { const body = s.slice(colon + 1).trim(); if (body) return body }
    return VERB[kind] || ''                           // 본문 없는 위임 마커 등
  }
  s = s.replace(/^사용자\s*개입\s*[—\-:]\s*/, '').trim()    // "사용자 개입 — " 접두 제거
  if (VERB[kind] && s.length <= 4) return VERB[kind] // "배포","완료" 같은 단어 마커 → 친근한 말
  return s
}

// 회의·표결은 발화자가 매 줄 달라 '연속 동일발화자' 묶음에 안 잡혀 N개 버블로 흩뿌려진다.
// 같은 kind 연속을 한 블록(참여자 스택 + 발언 목록)으로 — 디스코드식 평채널 흩뿌림을 네이티브 구조화로.
const TAG_KINDS = new Set(['meeting', 'vote'])
// 수명주기 랜드마크 — 일반 버블 대신 흐름 분절선(페이즈 구분)으로. 목표→작업→완료→배포의 마디.
const MILESTONE = new Set(['goal_set', 'task_complete', 'deploy', 'convergence_alert'])
// 블록 헤더 아바타 스택용 — 발언자 중복 제거(첫 등장 순).
const collabSpeakers = (g) => {
  const seen = new Set(), out = []
  for (const e of g.entries) { if (!seen.has(e.seed)) { seen.add(e.seed); out.push(e) } }
  return out
}

// 디스코드식: 사람·직원 구분 없이 한 흐름. 같은 사람 연속 메시지는 한 묶음(머리글 1번).
const groups = computed(() => {
  const out = []
  let cur = null, work = null, collab = null, lastDay = null   // dayKey/dayLabel은 kinds.js 공용(중복 제거)
  const flushWork = () => { if (work) { out.push(work); work = null } }
  const flushCollab = () => { collab = null }      // 블록은 생성 시 바로 out에 넣고 entries만 누적
  const maybeDay = (ts) => {
    const k = dayKey(ts)
    if (k !== lastDay) { flushWork(); flushCollab(); cur = null; out.push({ type: 'date', key: 'd' + out.length, label: dayLabel(ts) }); lastDay = k }
  }
  for (const m of (data.value?.messages || [])) {
    if (!isConv(m)) {                                   // 도구 작업 — 한 줄로 접되, 펼치면 무슨 작업인지 보이게
      cur = null; flushCollab()
      const it = workItem(m.summary || '')
      if (work) { work.n++; if (work.items.length < 50) work.items.push(it) }
      else work = { type: 'work', key: 'w' + out.length, n: 1, items: [it] }
      continue
    }
    maybeDay(m.ts)
    flushWork()
    // 회의·표결: 발화자 무관 같은 kind 연속을 한 블록으로(참여자 스택 + 발언). 집계는 만들지 않음
    // (Status Rule — 표결 집계 등 내부 조율은 채널 비노출, 도구 반환 전용. 채널엔 발언만 온다).
    if (m.type === 'agent' && TAG_KINDS.has(m.kind)) {
      cur = null
      const entry = { key: m.key, actorId: m.actor_id || null, author: m.actor_name || m.actor_role || '직원',
        role: m.actor_name ? m.actor_role : null, seed: m.actor_id || m.actor_name || m.actor_role,
        text: cleanLine(m.body || m.summary || '', m.kind), ts: m.ts, round: m.round || null }
      if (collab && collab.kind === m.kind) { collab.entries.push(entry) }
      else { collab = { type: 'collab', kind: m.kind, key: 'k' + m.key, label: kindMeta(m.kind), ts: m.ts, entries: [entry] }; out.push(collab) }
      continue
    }
    flushCollab()
    // 수명주기 페이즈 구분선 — 목표/완료/배포/경보를 일반 버블이 아니라 흐름 분절선으로
    if (m.type === 'agent' && MILESTONE.has(m.kind)) {
      cur = null
      out.push({ type: 'phase', key: 'p' + m.key, kind: m.kind, label: kindMeta(m.kind), ts: m.ts,
        text: cleanLine(m.body || m.summary || '', m.kind), actor: m.actor_name || m.actor_role || null })
      continue
    }
    // 사람: 직접 보낸 메시지 + 행위자 없는 사용자 요청·개입(과거 디스코드 기록)
    const userOrigin = (m.kind === 'user_request' || m.kind === 'intervention') && !m.actor_id
    const isHuman = m.type === 'human' || userOrigin
    const isInterject = m.type === 'human' && m.interject       // 진행 중 개입 — 구분 표시
    const author = m.type === 'human' ? (m.author || '나') : isHuman ? '사람' : (m.actor_name || m.actor_role || '직원')
    // 개입은 단독 그룹(인접 사람 메시지와 안 섞이게) — 흐름 중 끼어든 신호라 따로 보이게
    const id = isInterject ? 'ij:' + m.key : isHuman ? 'h:' + author : (m.actor_id || m.actor_role || '?')
    const raw = m.body || m.summary || ''
    const line = { key: m.key, text: isHuman ? cleanLine(raw, null) : cleanLine(raw, m.kind), kind: m.kind, ts: m.ts,
      to: (!isHuman && (m.target_name || m.target_role)) || null }
    if (cur && cur.id === id) { cur.lines.push(line) }
    else {
      cur = { type: 'group', key: 'g' + m.key, id, author, isHuman, interject: isInterject,
        role: isHuman ? null : (m.actor_name ? m.actor_role : null),
        seed: isHuman ? author : (m.actor_id || m.actor_name || m.actor_role),
        actorId: isHuman ? null : m.actor_id, lines: [line], ts: m.ts }
      out.push(cur)
    }
  }
  flushWork()
  return out
})
// 도구 작업 한 건을 "행동 + 대상"으로. summary 예: "AI 엔지니어: Read /…/game.js"
const TOOL = { Read: '읽기', Write: '쓰기', Edit: '수정', MultiEdit: '수정', run: '실행', Bash: '실행', Glob: '검색', Grep: '검색', WebSearch: '검색', WebFetch: '가져오기', NotebookEdit: '수정' }
function workItem(s) {
  s = (s || '').replace(/^[^:]*:\s*/, '').trim()
  const sp = s.indexOf(' ')
  const tool = TOOL[sp > 0 ? s.slice(0, sp) : s] || (sp > 0 ? s.slice(0, sp) : s) || '작업'
  let rest = sp > 0 ? s.slice(sp + 1).trim() : ''
  if (/\//.test(rest) && !/\s/.test(rest)) rest = rest.split('/').filter(Boolean).pop() || rest   // 경로 → 파일명
  if (rest.length > 48) rest = '…' + rest.slice(-46)
  return { tool, target: rest }
}
const openWork = ref(new Set())
function toggleWork(key) { const s = new Set(openWork.value); s.has(key) ? s.delete(key) : s.add(key); openWork.value = s }
// 긴 메시지 접기 + 프로젝트 맥락 띠 펼침
const ctxOpen = ref(false)
const longOpen = ref(new Set())
const isLong = (t) => (t || '').length > 360
function toggleLong(k) { const s = new Set(longOpen.value); s.has(k) ? s.delete(k) : s.add(k); longOpen.value = s }
// 가벼운 마크다운 — 코드블록·인라인코드·굵게·링크·줄바꿈. HTML 이스케이프 후 적용.
function renderMd(s) {
  s = String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  s = s.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, l, c) => `<pre class="md-pre">${c.replace(/\n+$/, '')}</pre>`)
  s = s.replace(/`([^`\n]+)`/g, '<code class="md-code">$1</code>')
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
  s = s.replace(/(https?:\/\/[^\s<）)]+)/g, '<a href="$1" target="_blank" rel="noopener" class="md-link">$1</a>')
  return s.replace(/\n/g, '<br>')
}

// 엔진 상태(네이티브) — 구조화된 처리 상태(working/done)에서. 이모지 파싱 아님.
const liveStatus = computed(() => {
  const ls = data.value?.live_status
  if (!ls || !ls.ts) return null
  const age = Date.now() / 1000 - ls.ts
  const terminal = ls.state === 'done' || ls.state === 'stopped'   // 종결 상태(완료·중지)
  if (terminal ? age >= 1800 : age >= 300) return null   // 완료·중지 30분 / 작업중 5분
  return ls
})
// 정체(조용함) 라벨 — '작업 중'이라도 마지막 봇 출력 이후 오래 조용하면 정직하게 알린다.
// 90초 미만은 정상 생성 중으로 보고 숨김(턴이 길 수 있음), 그 이상이면 'N분째 조용'.
const quietLabel = computed(() => {
  const q = liveStatus.value?.quiet
  if (liveStatus.value?.state !== 'working' || q == null || q < 90) return null
  const m = Math.floor(q / 60)
  return m >= 1 ? `${m}분째 조용` : `${q}초째 조용`
})
// 협업 엔진(러너) 가동 여부 — 정적 안내문 대신 실제 heartbeat 기반.
const engineLive = computed(() => !!stats.value?.engine?.live)
const batonHere = computed(() => {
  const b = stats.value?.baton
  // 최근(5분 이내)일 때만 '작업 중'. 오래된 시드 baton으로 영구 표시되던 것 방지.
  const live = b && b.ts && (Date.now() / 1000 - b.ts) < 300
  return live && b.project === route.params.pid ? b.role : null
})

const atBottom = ref(true)
const live = ref(false)
let poll = null
function scrollBottom() { const el = msgsEl.value; if (el) el.scrollTop = el.scrollHeight }
function onScroll() {
  const el = msgsEl.value; if (!el) return
  atBottom.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 80
}
const loadErr = ref(false)
async function load() {
  loading.value = true; briefing.value = null; showBrief.value = false; loadErr.value = false
  try { data.value = await api.channelMessages(route.params.pid) }
  catch (e) { if (!data.value) loadErr.value = true }   // 401은 인터셉터가 처리 — 그 외 실패면 빈 화면 대신 안내
  finally { loading.value = false; atBottom.value = true; await nextTick(); scrollBottom() }
}
// 라이브 폴링 — 새 메시지를 새로고침 없이. 바닥에 있을 때만 자동스크롤, 탭 숨김 시 정지.
async function refresh() {
  if (document.hidden) return
  const pid = route.params.pid                 // 채널 전환 레이스 방지: 응답이 늦게 와도 현재 채널만 반영
  let fresh
  try {
    fresh = await api.channelMessages(pid)
    if (pid !== route.params.pid) return        // 그새 채널이 바뀌었으면 버린다
  } catch (e) { api.stats().then((s) => { stats.value = s }).catch(() => {}); return }
  live.value = true
  if (!data.value) {
    data.value = fresh
  } else {
    // append-only: 기존 메시지 배열은 보존하고 '새 메시지만' 덧붙인다 — 매번 통째 교체하면
    // DOM이 다시 그려져 드래그한 텍스트 선택이 지워지고 복사가 안 됐다. 메타데이터만 갱신.
    const cur = data.value
    cur.name = fresh.name; cur.context = fresh.context; cur.live_status = fresh.live_status
    cur.pending_count = fresh.pending_count; cur.leader_id = fresh.leader_id; cur.leader_role = fresh.leader_role
    cur.visibility = fresh.visibility; cur.owner_handle = fresh.owner_handle
    cur.is_owner = fresh.is_owner; cur.is_member = fresh.is_member
    const have = new Set((cur.messages || []).map((m) => m.key))
    const add = (fresh.messages || []).filter((m) => !have.has(m.key))
    if (add.length) {
      cur.messages.push(...add)
      if (atBottom.value) { await nextTick(); scrollBottom() }
    }
  }
  api.stats().then((s) => { stats.value = s }).catch(() => {})
}
function startPoll() { stopPoll(); poll = setInterval(refresh, 2500) }   // 봇 응답이 더 빨리 떠 보이게(4s→2.5s)
function stopPoll() { if (poll) { clearInterval(poll); poll = null } }
async function send() {
  const body = draft.value.trim(); if (!body) return
  sending.value = true
  try {
    const m = await api.say(route.params.pid, { body, author: '사람' })
    if (data.value?.messages) data.value.messages.push(m)
    draft.value = ''
    await nextTick(); scrollBottom()
  } catch (e) { toast('메시지를 보내지 못했어요', 'err') }
  finally { sending.value = false }
}
const briefErr = ref(false)
async function loadBrief() {
  showBrief.value = !showBrief.value
  if (showBrief.value && !briefing.value) {
    briefErr.value = false
    try { briefing.value = await api.briefing(route.params.pid) }
    catch (e) { briefErr.value = true }
  }
}
async function suggest() {
  const q = reqBody.value.trim(); if (!q) return
  recLoading.value = true
  try {
    const r = await api.recommend(q, 3)
    recs.value = (r.results || []).slice(0, 3)
  } finally { recLoading.value = false }
}
function pickRec(b) { reqTo.value = b.bot_id; recs.value = [] }
const REASON_LABEL = { role_match: '역할 적합', keyword_overlap: '키워드 일치', expertise: '전문성', track_record: '실적' }
function recWhy(b) {
  const r = b.reasons || {}
  const top = Object.entries(r).sort((a, c) => c[1] - a[1])[0]
  return top ? (REASON_LABEL[top[0]] || top[0]) : ''
}
async function sendRequest() {
  const body = reqBody.value.trim(); if (!body) return
  recs.value = []
  reqSending.value = true
  try {
    const r = await api.makeRequest(route.params.pid, { to_id: reqTo.value || undefined, kind: reqKind.value, body })
    if (reqKind.value === 'auto') toast(r?.kind === 'I' ? '질문으로 분류했어요' : '작업으로 분류했어요')
    reqBody.value = ''; reqTo.value = ''       // 다음 요청은 기본(리더)으로 리셋
    data.value = await api.channelMessages(route.params.pid)
    await nextTick(); scrollBottom()
  } finally { reqSending.value = false }
}
// 진행 중 개입(정보 전달) — 흐름 도중 봇에게 정보를 넘김. 러너가 받아 deliver_human_info로 다음 턴에 주입.
const interjectBody = ref('')
const interjecting = ref(false)
async function doInterject() {
  const body = interjectBody.value.trim()
  if (!body) return
  interjecting.value = true
  try {
    await api.interject(route.params.pid, { body })
    interjectBody.value = ''
    toast('전했어요 — 봇이 다음 턴에 반영합니다')
    data.value = await api.channelMessages(route.params.pid)
  } catch (e) { toast(e?.response?.data?.detail || '전하지 못했어요', 'err') }
  finally { interjecting.value = false }
}

// 작업 중지 — 진행 중인 협업 흐름을 멈춤(소유자/멤버). 러너가 신호를 받아 SYS.request_cancel.
const stopping = ref(false)
async function doStop() {
  stopping.value = true
  try {
    await api.stopWork(route.params.pid)
    toast('작업 중지를 요청했어요 — 곧 멈춥니다')
  } catch (e) { toast(e?.response?.data?.detail || '중지하지 못했어요', 'err') }
  finally { stopping.value = false }
}

// 멎은 요청 다시 맡기기 — 처리하던 러너가 죽어 '작업 중'으로 박제된 요청을 큐로 되돌림(소유자/멤버).
const requeuing = ref(false)
async function doRequeue() {
  requeuing.value = true
  try {
    const r = await api.requeueStuck(route.params.pid)
    toast(r.requeued ? `${r.requeued}건 다시 맡겼어요` : '다시 맡길 요청이 없어요')
    if (r.requeued) data.value = await api.channelMessages(route.params.pid)
  } catch (e) { toast(e?.response?.data?.detail || '다시 맡기지 못했어요', 'err') }
  finally { requeuing.value = false }
}

// 멤버(멀티유저) — 채널을 함께 쓰는 사람들. 친구를 초대해 공동 리드.
const members = ref([])
const invitedPending = ref([])     // 초대했지만 아직 수락 안 한 사람
const showMembers = ref(false)
const myFriends = ref([])
const mbg = (m) => m.color || avatarColor(m.handle || m.name)
const mini = (m) => (m.name || m.handle || '?').slice(0, 1)
const memberHandles = computed(() => new Set([...members.value.map((m) => m.handle), ...invitedPending.value.map((m) => m.handle)]))
const invitable = computed(() => myFriends.value.filter((f) => !memberHandles.value.has(f.handle)))
async function loadMembers() {
  try { const r = await api.members(route.params.pid); members.value = r.members || []; invitedPending.value = r.invited || [] }
  catch (e) { members.value = []; invitedPending.value = [] }
}
async function toggleMembers() {
  showMembers.value = !showMembers.value
  if (showMembers.value && !me.is_guest) {
    try { myFriends.value = await api.friends() } catch (e) { myFriends.value = [] }
  }
}
async function invite(handle) {
  try {
    const r = await api.invite(route.params.pid, handle)
    members.value = r.members || []; invitedPending.value = r.invited || []
    toast('초대했어요')
  } catch (e) { toast(e?.response?.data?.detail || '초대하지 못했어요', 'err') }
}

// 채널 관리·접근(소유자/멤버 기반)
const isOwner = computed(() => !!data.value?.is_owner)
const canInvite = computed(() => !!data.value?.is_member && !me.is_guest)
const isPublic = computed(() => data.value?.visibility === 'public')
async function doVisibility() {
  menu.value = false
  try {
    const r = await api.setChannelVisibility(route.params.pid)
    if (data.value) data.value.visibility = r.visibility
    toast(r.visibility === 'public' ? '공개 채널로 바꿨어요' : '비공개 채널로 바꿨어요')
  } catch (e) { toast('변경하지 못했어요', 'err') }
}
async function doRename() {
  menu.value = false
  const name = await askPrompt({ title: '채널 이름 변경', placeholder: '새 이름', value: data.value?.name || '' })
  if (!name) return
  try {
    const r = await api.renameChannel(route.params.pid, name)
    if (data.value) data.value.name = r.name
    toast('이름을 바꿨어요')
  } catch (e) { toast('이름을 바꾸지 못했어요', 'err') }
}
async function doArchive() {
  menu.value = false
  try {
    const r = await api.archiveChannel(route.params.pid)        // {archived} 반환 — 로컬 상태도 갱신(메뉴 라벨 최신화)
    if (data.value) data.value.status = r?.archived ? 'archived' : (data.value.context?.status || 'live')
    toast(r?.archived ? '채널을 보관했어요' : '채널을 복원했어요')
  } catch (e) { toast('처리하지 못했어요', 'err') }
}
async function doRemove() {
  menu.value = false
  const ok = await askConfirm({ title: '채널 삭제', message: `'${data.value?.name || route.params.pid}' 채널을 삭제합니다. 되돌릴 수 없습니다.`, danger: true })
  if (!ok) return
  await api.removeChannel(route.params.pid)
  window.dispatchEvent(new Event('organt:channels'))   // 사이드바에서 즉시 사라지게(8초 폴 안 기다림)
  router.push('/')
}
onMounted(() => {
  load(); loadMembers()
  api.agents({ ordering: '-event_count' }).then((a) => { agents.value = a })
  api.stats().then((s) => { stats.value = s }).catch(() => {})
  startPoll()
})
onUnmounted(stopPoll)
watch(() => route.params.pid, () => {
  live.value = false; menu.value = false; pickerOpen.value = false
  showBrief.value = false; showStruct.value = false; showMembers.value = false; showArticle.value = false
  load(); loadMembers()
})
</script>

<template>
  <div class="chan-head">
    <span class="h">{{ data?.name || route.params.pid }}</span>
    <span v-if="data" class="vis-badge" :class="{ pub: isPublic }" :title="isPublic ? '공개 채널 — 누구나 열람' : '비공개 채널 — 멤버만'">
      <Icon :name="isPublic ? 'globe' : 'lock'" :size="12" />{{ isPublic ? '공개' : '비공개' }}
    </span>
    <span class="cid">{{ route.params.pid }}</span>
    <span v-if="batonHere" class="live-baton"><i class="pulse"></i>{{ batonHere }} 작업 중</span>
    <span v-else-if="live" class="muted" style="font-size:11.5px" title="자동 갱신 중">실시간 보기</span>
    <div class="baton">
      <div class="ch-members">
        <button class="members-btn" :class="{ on: showMembers }" title="멤버 · 친구 초대" aria-label="멤버" @click="toggleMembers">
          <span v-if="members.length" class="mstack">
            <span v-for="m in members.slice(0, 3)" :key="m.handle" class="mav xs" :style="{ background: mbg(m) }">{{ mini(m) }}</span>
          </span>
          <Icon v-else name="userPlus" :size="16" />
          <span v-if="members.length" class="mc">{{ members.length }}</span>
        </button>
        <template v-if="showMembers">
          <div class="menu-back" @click="showMembers = false"></div>
          <div class="members-pop">
            <div class="mp-sec">멤버 {{ members.length }}</div>
            <div class="mp-list">
              <div v-for="m in members" :key="m.handle" class="mp-row">
                <span class="mav" :style="{ background: mbg(m) }">{{ mini(m) }}</span>
                <div class="mp-meta"><div class="mp-n">{{ m.name }}</div><div class="mp-h">@{{ m.handle }}</div></div>
                <span v-if="m.role === 'lead'" class="lead-pill">리드</span>
              </div>
              <div v-if="!members.length" class="mp-empty">아직 멤버가 없어요. 친구를 초대해 함께 만들어보세요.</div>
            </div>
            <template v-if="invitedPending.length">
              <div class="mp-sec">초대됨 · 대기 {{ invitedPending.length }}</div>
              <div class="mp-list">
                <div v-for="m in invitedPending" :key="m.handle" class="mp-row">
                  <span class="mav" :style="{ background: mbg(m), opacity: .6 }">{{ mini(m) }}</span>
                  <div class="mp-meta"><div class="mp-n">{{ m.name }}</div><div class="mp-h">@{{ m.handle }}</div></div>
                  <span class="wait-pill">수락 대기</span>
                </div>
              </div>
            </template>
            <template v-if="canInvite">
              <div class="mp-sec">친구 초대</div>
              <div class="mp-list">
                <div v-for="f in invitable" :key="f.handle" class="mp-row">
                  <span class="mav" :style="{ background: mbg(f) }">{{ mini(f) }}</span>
                  <div class="mp-meta"><div class="mp-n">{{ f.name }}</div><div class="mp-h">@{{ f.handle }}</div></div>
                  <button class="btn sm" @click="invite(f.handle)">초대</button>
                </div>
                <div v-if="!invitable.length" class="mp-empty">초대할 친구가 없어요. <router-link to="/friends" class="mp-link">친구 추가 →</router-link></div>
              </div>
            </template>
            <div v-else-if="!me.is_guest" class="mp-empty">이 채널의 멤버만 친구를 초대할 수 있어요.</div>
            <div v-else class="mp-empty">체험 계정은 초대할 수 없어요. 회원가입하면 함께 협업할 수 있어요.</div>
          </div>
        </template>
      </div>
      <button class="iconbtn" :class="{ on: showArticle }" title="산출물 · 작업" aria-label="산출물 · 작업" @click="showArticle = !showArticle; showStruct = false"><Icon name="box" /></button>
      <button class="iconbtn" :class="{ on: showStruct }" title="협업 한눈에" aria-label="협업 한눈에" @click="showStruct = !showStruct; showArticle = false"><Icon name="network" /></button>
      <button class="iconbtn" :class="{ on: showBrief }" title="AI 요약" aria-label="AI 요약" @click="loadBrief"><Icon name="spark" /></button>
      <div class="ch-menu">
        <button class="iconbtn" title="채널 관리" aria-label="채널 관리" @click="menu = !menu"><Icon name="more" /></button>
        <div v-if="menu" class="menu-back" @click="menu = false"></div>
        <div v-if="menu" class="menu-pop">
          <template v-if="isOwner">
            <button @click="doVisibility"><Icon class="ic" :name="isPublic ? 'lock' : 'globe'" :size="15" />{{ isPublic ? '비공개로 전환' : '공개로 전환' }}</button>
            <button @click="doRename"><Icon class="ic" name="edit" :size="15" />이름 변경</button>
            <button @click="doArchive"><Icon class="ic" name="archive" :size="15" />보관 / 복원</button>
            <div class="menu-sep"></div>
            <button @click="doRemove" class="danger"><Icon class="ic" name="trash" :size="15" />삭제</button>
          </template>
          <div v-else class="menu-note">채널 소유자만 관리할 수 있어요.</div>
        </div>
      </div>
    </div>
  </div>

  <!-- 프로젝트 한눈에: 채팅 안 읽어도 목표·상태·산출물 파악 -->
  <div v-if="data?.context && (data.context.goal || data.context.links?.length)" class="proj-ctx">
    <div class="ctx-bar">
      <span class="ctx-status" :class="{ done: data.context.status === '완료' }">{{ data.context.status }}</span>
      <span v-if="data.leader_role" class="ctx-meta">리더 · {{ data.leader_role }}</span>
      <span v-if="data.context.deploys" class="ctx-meta">배포 {{ data.context.deploys }}회</span>
      <span class="ctx-grow"></span>
      <a v-for="l in data.context.links" :key="l" :href="l" target="_blank" rel="noopener" class="ctx-link"><Icon name="link" :size="13" />{{ l.replace(/^https?:\/\//, '').slice(0, 32) }}</a>
    </div>
    <div v-if="data.context.goal" class="ctx-goal" :class="{ open: ctxOpen }">
      <span class="ctx-goal-k">목표</span><span class="ctx-goal-t">{{ data.context.goal }}</span>
    </div>
    <button v-if="data.context.goal && data.context.goal.length > 84" class="ctx-more" @click="ctxOpen = !ctxOpen">{{ ctxOpen ? '접기' : '더 보기' }}</button>
  </div>

  <!-- 엔진 상태(네이티브) — 구조화 상태에서 조립, 이모지 아님 -->
  <div v-if="liveStatus" class="live-strip" :class="{ done: liveStatus.state === 'done', stopped: liveStatus.state === 'stopped', stalled: quietLabel }">
    <Icon v-if="liveStatus.state === 'done'" name="check" :size="14" class="ls-ic" /><Icon v-else-if="liveStatus.state === 'stopped'" name="x" :size="14" class="ls-ic" /><i v-else class="pulse"></i>
    <span class="live-strip-t"><b>{{ liveStatus.state === 'done' ? '완료' : liveStatus.state === 'stopped' ? '중지됨' : (liveStatus.actor || '직원') + ' 작업 중' }}</b><span v-if="liveStatus.goal" class="ls-goal"> · {{ liveStatus.goal }}</span><span v-if="quietLabel" class="ls-quiet"> · {{ quietLabel }}</span></span>
    <button v-if="liveStatus.state === 'working' && (data?.is_owner || data?.is_member)" class="ls-stop" :disabled="stopping" @click="doStop" title="진행 중인 작업을 멈춥니다">{{ stopping ? '…' : '중지' }}</button>
  </div>

  <!-- 진행 중 개입 — 작업 중일 때만. 끼어들어 정보 전하면 봇이 다음 턴에 판단·반영. -->
  <div v-if="liveStatus && liveStatus.state === 'working' && (data?.is_owner || data?.is_member)" class="interject-bar">
    <Icon name="send" :size="13" class="ij-ic" />
    <input class="ij-field" v-model="interjectBody" placeholder="끼어들어 정보 전하기 — 봇이 다음 턴에 반영합니다 (예: 백엔드 코드 다시 봐)"
           @keyup.enter="doInterject" :disabled="interjecting" />
    <button class="btn ghost sm" :disabled="interjecting || !interjectBody.trim()" @click="doInterject">{{ interjecting ? '…' : '개입' }}</button>
  </div>

  <CollabPanel v-if="showStruct" :key="route.params.pid" :pid="route.params.pid" :baton="stats?.baton" />
  <ArticlePanel v-if="showArticle" :key="'art-' + route.params.pid" :pid="route.params.pid" />

  <div v-if="data && data.pending_count" class="pending-bar">
    대기 중인 요청 <b>{{ data.pending_count }}건</b> — 정상 접수됐습니다.
    <template v-if="engineLive">협업 엔진이 <b>가동 중</b>이라 직원들이 곧 처리합니다.</template>
    <template v-else>협업 엔진이 <b>꺼져 있어</b> 대기 중입니다 — 엔진이 켜지면 처리됩니다.</template>
  </div>

  <!-- 멎은 요청 — 처리하던 러너가 멈춰 '작업 중'으로 박제된 것. 소유자/멤버가 다시 큐로. -->
  <div v-if="data && data.stuck_count" class="stuck-bar">
    <span>멎은 요청 <b>{{ data.stuck_count }}건</b> —
      <template v-if="engineLive">처리하던 직원이 응답을 멈췄어요. 다시 맡겨 큐로 되돌릴 수 있어요.</template>
      <template v-else>협업 엔진이 <b>꺼져</b> 처리가 멈췄어요. 다시 맡겨두면 엔진이 켜질 때 처리됩니다.</template>
    </span>
    <span class="sb-grow"></span>
    <button class="btn ghost sm" :disabled="requeuing" @click="doRequeue">{{ requeuing ? '…' : '다시 맡기기' }}</button>
  </div>

  <div v-if="showBrief && briefErr" class="pending-bar">브리핑을 불러오지 못했습니다. 잠시 후 다시 시도하세요.</div>
  <div v-if="showBrief && briefing" class="panel" style="margin:12px 20px 0">
    <h2>AI 요약</h2>
    <div style="padding:14px 16px">
      <div class="pre">{{ briefing.text }}</div>
      <div class="muted" style="font-size:11px;margin-top:8px">
        {{ briefing.generated ? 'AI가 요약' : '자동 요약' }} · 서로 점검 {{ briefing.stats.cross_checks }}회 · 배포 {{ briefing.stats.deploy_count }}회
      </div>
    </div>
  </div>

  <div class="msgs" ref="msgsEl" @scroll.passive="onScroll">
    <div v-if="loading" class="empty"><span class="spin"></span> 대화 불러오는 중…</div>
    <div v-else-if="loadErr" class="empty">대화를 불러오지 못했어요. <button class="linklike" @click="load">다시 시도</button></div>
    <template v-else>
      <template v-for="g in groups" :key="g.key">
        <!-- 날짜 구분 -->
        <div v-if="g.type === 'date'" class="day-sep"><span>{{ g.label }}</span></div>
        <!-- 수명주기 페이즈 구분선 — 목표/완료/배포/경보로 흐름을 마디 짓기 -->
        <div v-else-if="g.type === 'phase'" class="phase-sep" :class="g.kind">
          <span class="ph-pill" :style="{ color: g.label.c, background: g.label.bg }">{{ g.label.label }}</span>
          <span v-if="g.text" class="ph-text">{{ g.text }}</span>
          <span v-if="g.actor" class="ph-actor">{{ g.actor }}</span>
          <span class="ph-time">{{ timeFmt(g.ts) }}</span>
        </div>
        <!-- 도구 작업: 접힌 한 줄, 클릭하면 무슨 작업인지 펼침 -->
        <div v-else-if="g.type === 'work'" class="work-fold">
          <button class="work-toggle" @click="toggleWork(g.key)">
            <Icon name="chevron" :size="13" class="wchev" :class="{ open: openWork.has(g.key) }" />
            직원 작업 {{ g.n }}건
          </button>
          <div v-if="openWork.has(g.key)" class="work-items">
            <div v-for="(it, i) in g.items" :key="i" class="work-item"><span class="wt">{{ it.tool }}</span><span v-if="it.target" class="wg">{{ it.target }}</span></div>
            <div v-if="g.n > g.items.length" class="work-item wmore">…외 {{ g.n - g.items.length }}건</div>
          </div>
        </div>
        <!-- 회의·표결: 흩뿌린 버블 대신 한 블록(참여자 스택 + 발언 목록) -->
        <div v-else-if="g.type === 'collab'" class="collab-block" :class="g.kind">
          <div class="cb-head">
            <span class="cb-kind" :style="{ color: g.label.c, background: g.label.bg }">{{ g.label.label }}</span>
            <span class="cb-stack">
              <span v-for="s in collabSpeakers(g)" :key="s.seed" class="cb-av xs" :style="{ background: avatarColor(s.seed) }" :title="s.author">{{ monogram(s.author) }}</span>
            </span>
            <span class="cb-meta">{{ collabSpeakers(g).length }}명 · 발언 {{ g.entries.length }}</span>
            <span class="cb-time">{{ timeFmt(g.ts) }}</span>
          </div>
          <div class="cb-body">
            <template v-for="(e, i) in g.entries" :key="e.key">
              <div v-if="e.round && e.round !== (i ? g.entries[i - 1].round : null)" class="cb-round"><span>{{ e.round }}라운드</span></div>
              <div class="cb-row">
                <router-link v-if="e.actorId" :to="`/agents/${e.actorId}`" class="cb-av" :style="{ background: avatarColor(e.seed) }">{{ monogram(e.author) }}</router-link>
                <span v-else class="cb-av" :style="{ background: avatarColor(e.seed) }">{{ monogram(e.author) }}</span>
                <div class="cb-rbd">
                  <div class="cb-rhead"><span class="cb-rname">{{ e.author }}</span><span v-if="e.role" class="cb-rrole">{{ e.role }}</span></div>
                  <div class="cb-rtext" v-html="renderMd(e.text)"></div>
                </div>
              </div>
            </template>
          </div>
        </div>
        <!-- 메시지 묶음: 사람·직원 동일 레이아웃 -->
        <div v-else class="cmsg" :class="{ interject: g.interject }">
          <router-link v-if="g.actorId" :to="`/agents/${g.actorId}`" class="cmsg-av" :style="{ background: g.isHuman ? 'var(--accent)' : avatarColor(g.seed) }">{{ monogram(g.author) }}</router-link>
          <div v-else class="cmsg-av" :style="{ background: g.isHuman ? 'var(--accent)' : avatarColor(g.seed) }">{{ monogram(g.author) }}</div>
          <div class="cmsg-bd">
            <div class="cmsg-head">
              <router-link v-if="g.actorId" :to="`/agents/${g.actorId}`" class="cmsg-name">{{ g.author }}</router-link>
              <span v-else class="cmsg-name">{{ g.author }}</span>
              <span v-if="g.role" class="cmsg-role">{{ g.role }}</span>
              <span v-if="g.interject" class="ij-tag">개입</span>
              <span v-if="g.actorId && g.actorId === data?.leader_id" class="lead-pill">리더</span>
              <span v-else-if="!g.isHuman" class="ai-tag">AI</span>
              <span class="cmsg-time">{{ timeFmt(g.ts) }}</span>
            </div>
            <div v-for="ln in g.lines" :key="ln.key" class="cmsg-line">
              <div :class="{ clamp: isLong(ln.text) && !longOpen.has(ln.key) }">
                <span v-if="ln.to" class="cmsg-to">@{{ ln.to }}</span><span v-html="renderMd(ln.text)"></span>
              </div>
              <button v-if="isLong(ln.text)" class="more-btn" @click="toggleLong(ln.key)">{{ longOpen.has(ln.key) ? '접기 ▴' : '더 보기 ▾' }}</button>
            </div>
          </div>
        </div>
      </template>
      <div v-if="!groups.length" class="empty">아직 대화가 없어요. 아래에서 메시지를 보내거나 직원에게 일을 부탁해보세요.</div>
    </template>
    <button v-if="!atBottom && groups.length" class="to-bottom" @click="scrollBottom(); atBottom = true"><Icon name="chevron" :size="16" />맨 아래로</button>
  </div>

  <div class="composer">
    <div class="seg" style="margin-bottom:10px">
      <button :class="{ on: mode === 'msg' }" @click="mode = 'msg'"><Icon name="message" :size="15" />메시지</button>
      <button :class="{ on: mode === 'req' }" @click="mode = 'req'"><Icon name="send" :size="15" />직원에게 부탁</button>
    </div>

    <div v-if="mode === 'msg'" class="row">
      <input class="field" v-model="draft" placeholder="이 채널에 메시지 남기기" @keyup.enter="send" :disabled="sending" />
      <button class="btn" @click="send" :disabled="sending || !draft.trim()"><Icon name="send" :size="15" />보내기</button>
    </div>

    <template v-else>
      <div class="req-tools">
        <div class="picker">
          <button class="trigger" @click="pickerOpen = !pickerOpen">
            <template v-if="reqToBot">
              <span class="bot-av sm" :style="{ background: avatarBg(reqToBot) }">{{ monogram(reqToBot.name, reqToBot.role) }}</span>
              <span>{{ reqToBot.name || reqToBot.role }}<span v-if="reqToBot.name" class="muted"> · {{ reqToBot.role }}</span></span>
            </template>
            <span v-else class="ph">맡길 직원 (비우면 리더가)</span>
            <Icon class="chev" name="chevron" :size="15" />
          </button>
          <template v-if="pickerOpen">
            <div class="menu-back" @click="pickerOpen = false"></div>
            <div class="picker-pop">
              <div class="picker-opt" :class="{ sel: !reqTo }" @click="choose(null)"><span class="ph muted">리더에게 (자동)</span></div>
              <div v-for="b in agents" :key="b.bot_id" class="picker-opt" :class="{ sel: String(b.bot_id) === String(reqTo) }" @click="choose(b)">
                <span class="bot-av sm" :style="{ background: avatarBg(b) }">{{ monogram(b.name, b.role) }}</span>
                <span>{{ b.name || b.role }}<span v-if="b.name" class="muted"> · {{ b.role }}</span></span>
                <span class="role">활동 {{ b.event_count }}</span>
              </div>
            </div>
          </template>
        </div>
        <button class="btn ghost sm" :disabled="recLoading || !reqBody.trim()" @click="suggest" title="이 일에 잘 맞는 직원 추천">
          <Icon name="target" :size="15" />{{ recLoading ? '…' : '추천 받기' }}
        </button>
      </div>
      <div v-if="recs.length" class="recs">
        <span class="muted" style="font-size:11.5px">이 일엔</span>
        <button v-for="b in recs" :key="b.bot_id" class="recchip" @click="pickRec(b)" :title="`${b.role} · 활동 ${b.event_count}`">
          <span class="bot-av sm" :style="{ background: avatarColor(b.name || b.role) }">{{ monogram(b.name, b.role) }}</span>
          <b>{{ b.name || b.role }}</b><span class="muted" style="font-size:11.5px">{{ b.role }}</span><span class="why" v-if="recWhy(b)">{{ recWhy(b) }}</span>
        </button>
      </div>
      <div class="row">
        <input class="field" v-model="reqBody" placeholder="무엇을 맡길지 — 담당 직원이 보고 작업이든 답변이든 알아서 합니다" @keyup.enter="sendRequest" :disabled="reqSending" />
        <button class="btn" @click="sendRequest" :disabled="reqSending || !reqBody.trim()"><Icon name="send" :size="15" />{{ reqSending ? '…' : '요청' }}</button>
      </div>
    </template>
    <div class="hint">메시지는 팀과의 대화이고, 요청은 직원에게 작업·질문을 맡깁니다. 처리는 협업 엔진이 켜져 있을 때 진행됩니다.</div>
  </div>
</template>
