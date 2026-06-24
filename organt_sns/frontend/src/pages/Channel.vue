<script setup>
import { ref, onMounted, onUnmounted, watch, nextTick, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '../api'
import { kindMeta, timeFmt } from '../kinds'
import CollabPanel from '../components/CollabPanel.vue'
import Icon from '../components/Icon.vue'
import { monogram, avatarColor, avatarBg } from '../avatar'
import { askPrompt, askConfirm } from '../dialog'

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
const stats = ref(null)
const msgsEl = ref(null)
// 입력 모드: 사람 메시지(msg) vs 봇 요청(req, Work/Info 1급)
const mode = ref('msg')
const agents = ref([])
const reqTo = ref('')
const reqKind = ref('W')
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

// 디스코드식: 사람·직원 구분 없이 한 흐름. 같은 사람 연속 메시지는 한 묶음(머리글 1번).
const groups = computed(() => {
  const out = []
  let cur = null, work = null
  const flushWork = () => { if (work) { out.push(work); work = null } }
  for (const m of (data.value?.messages || [])) {
    if (!isConv(m)) {                                   // 도구 작업 — 누가 했든 한 줄로 조용히 접는다
      cur = null
      if (work) work.n++
      else work = { type: 'work', key: 'w' + out.length, n: 1 }
      continue
    }
    flushWork()
    // 사람: 직접 보낸 메시지 + 행위자 없는 사용자 요청·개입(과거 디스코드 기록)
    const userOrigin = (m.kind === 'user_request' || m.kind === 'intervention') && !m.actor_id
    const isHuman = m.type === 'human' || userOrigin
    const author = m.type === 'human' ? (m.author || '나') : isHuman ? '사람' : (m.actor_name || m.actor_role || '직원')
    const id = isHuman ? 'h:' + author : (m.actor_id || m.actor_role || '?')
    const raw = m.body || m.summary || ''
    const line = { key: m.key, text: isHuman ? cleanLine(raw, null) : cleanLine(raw, m.kind), kind: m.kind, ts: m.ts,
      to: (!isHuman && (m.target_name || m.target_role)) || null }
    if (cur && cur.id === id) { cur.lines.push(line) }
    else {
      cur = { type: 'group', key: 'g' + m.key, id, author, isHuman,
        seed: isHuman ? author : (m.actor_id || m.actor_name || m.actor_role),
        actorId: isHuman ? null : m.actor_id, lines: [line], ts: m.ts }
      out.push(cur)
    }
  }
  flushWork()
  return out
})

const batonHere = computed(() => {
  const b = stats.value?.baton
  return b && b.project === route.params.pid ? b.role : null
})

const atBottom = ref(true)
const live = ref(false)
let poll = null
function scrollBottom() { const el = msgsEl.value; if (el) el.scrollTop = el.scrollHeight }
function onScroll() {
  const el = msgsEl.value; if (!el) return
  atBottom.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 80
}
async function load() {
  loading.value = true; briefing.value = null; showBrief.value = false
  try { data.value = await api.channelMessages(route.params.pid) }
  finally { loading.value = false; atBottom.value = true; await nextTick(); scrollBottom() }
}
// 라이브 폴링 — 새 메시지를 새로고침 없이. 바닥에 있을 때만 자동스크롤, 탭 숨김 시 정지.
async function refresh() {
  if (document.hidden) return
  const pid = route.params.pid                 // 채널 전환 레이스 방지: 응답이 늦게 와도 현재 채널만 반영
  try {
    const fresh = await api.channelMessages(pid)
    if (pid !== route.params.pid) return        // 그새 채널이 바뀌었으면 버린다
    const prevLast = data.value?.messages?.length
    data.value = fresh
    live.value = true
    if ((fresh.messages?.length || 0) !== prevLast && atBottom.value) { await nextTick(); scrollBottom() }
  } catch (e) { /* 직전 상태 유지 */ }
  api.stats().then((s) => { stats.value = s }).catch(() => {})
}
function startPoll() { stopPoll(); poll = setInterval(refresh, 4000) }
function stopPoll() { if (poll) { clearInterval(poll); poll = null } }
async function send() {
  const body = draft.value.trim(); if (!body) return
  sending.value = true
  try {
    const m = await api.say(route.params.pid, { body, author: '사람' })
    if (data.value?.messages) data.value.messages.push(m)
    draft.value = ''
    await nextTick(); scrollBottom()
  } finally { sending.value = false }
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
    await api.makeRequest(route.params.pid, { to_id: reqTo.value || undefined, kind: reqKind.value, body })
    reqBody.value = ''; reqTo.value = ''       // 다음 요청은 기본(리더)으로 리셋
    data.value = await api.channelMessages(route.params.pid)
    await nextTick(); scrollBottom()
  } finally { reqSending.value = false }
}
// 채널 관리(관리 기능)
const isMine = computed(() => !/^P-/.test(route.params.pid))
async function doRename() {
  menu.value = false
  const name = await askPrompt({ title: '채널 이름 변경', placeholder: '새 이름', value: data.value?.name || '' })
  if (!name) return
  const r = await api.renameChannel(route.params.pid, name)
  if (data.value) data.value.name = r.name
}
async function doArchive() {
  menu.value = false
  await api.archiveChannel(route.params.pid)
}
async function doRemove() {
  menu.value = false
  const ok = await askConfirm({ title: '채널 삭제', message: `'${data.value?.name || route.params.pid}' 채널을 삭제합니다. 되돌릴 수 없습니다.`, danger: true })
  if (!ok) return
  await api.removeChannel(route.params.pid)
  router.push('/')
}
onMounted(() => {
  load()
  api.agents({ ordering: '-event_count' }).then((a) => { agents.value = a })
  api.stats().then((s) => { stats.value = s }).catch(() => {})
  startPoll()
})
onUnmounted(stopPoll)
watch(() => route.params.pid, () => {
  live.value = false; menu.value = false; pickerOpen.value = false
  showBrief.value = false; showStruct.value = false
  load()
})
</script>

<template>
  <div class="chan-head">
    <span class="h">{{ data?.name || route.params.pid }}</span>
    <span class="cid">{{ route.params.pid }}</span>
    <span v-if="batonHere" class="live-baton"><i class="pulse"></i>{{ batonHere }} 작업 중</span>
    <span v-else-if="live" class="muted" style="font-size:11.5px" title="자동 갱신 중">실시간 보기</span>
    <div class="baton">
      <button class="iconbtn" :class="{ on: showStruct }" title="협업 한눈에" aria-label="협업 한눈에" @click="showStruct = !showStruct"><Icon name="network" /></button>
      <button class="iconbtn" :class="{ on: showBrief }" title="AI 요약" aria-label="AI 요약" @click="loadBrief"><Icon name="spark" /></button>
      <div class="ch-menu">
        <button class="iconbtn" title="채널 관리" aria-label="채널 관리" @click="menu = !menu"><Icon name="more" /></button>
        <div v-if="menu" class="menu-back" @click="menu = false"></div>
        <div v-if="menu" class="menu-pop">
          <button @click="doRename"><Icon class="ic" name="edit" :size="15" />이름 변경</button>
          <button @click="doArchive"><Icon class="ic" name="archive" :size="15" />보관 / 복원</button>
          <template v-if="isMine"><div class="menu-sep"></div>
            <button @click="doRemove" class="danger"><Icon class="ic" name="trash" :size="15" />삭제</button></template>
          <div v-else class="menu-note">디스코드 쇼케이스 채널은 삭제 불가</div>
        </div>
      </div>
    </div>
  </div>

  <CollabPanel v-if="showStruct" :key="route.params.pid" :pid="route.params.pid" :baton="stats?.baton" />

  <div v-if="data && data.pending_count" class="pending-bar">
    대기 중인 요청 <b>{{ data.pending_count }}건</b> — 정상 접수됐습니다. 실제 작업은 <b>협업 엔진이 켜져 있을 때</b> 직원들이 처리합니다.
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
    <template v-else>
      <template v-for="g in groups" :key="g.key">
        <!-- 도구 작업: 조용한 한 줄 -->
        <div v-if="g.type === 'work'" class="work-line"><span class="dotmark"></span>작업 중 · {{ g.n }}건</div>
        <!-- 메시지 묶음: 사람·직원 동일 레이아웃 -->
        <div v-else class="cmsg">
          <router-link v-if="g.actorId" :to="`/agents/${g.actorId}`" class="cmsg-av" :style="{ background: g.isHuman ? 'var(--accent)' : avatarColor(g.seed) }">{{ monogram(g.author) }}</router-link>
          <div v-else class="cmsg-av" :style="{ background: g.isHuman ? 'var(--accent)' : avatarColor(g.seed) }">{{ monogram(g.author) }}</div>
          <div class="cmsg-bd">
            <div class="cmsg-head">
              <router-link v-if="g.actorId" :to="`/agents/${g.actorId}`" class="cmsg-name">{{ g.author }}</router-link>
              <span v-else class="cmsg-name">{{ g.author }}</span>
              <span v-if="!g.isHuman" class="ai-tag">AI</span>
              <span class="cmsg-time">{{ timeFmt(g.ts) }}</span>
            </div>
            <div v-for="ln in g.lines" :key="ln.key" class="cmsg-line">
              <span v-if="ln.to" class="cmsg-to">@{{ ln.to }}</span>{{ ln.text }}
            </div>
          </div>
        </div>
      </template>
      <div v-if="!groups.length" class="empty">아직 대화가 없어요. 아래에서 메시지를 보내거나 직원에게 일을 부탁해보세요.</div>
    </template>
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
      <div class="flex" style="gap:8px;margin-bottom:8px;align-items:stretch">
        <div class="picker" style="flex:1">
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
        <div class="seg">
          <button :class="{ on: reqKind === 'W' }" @click="reqKind = 'W'">작업</button>
          <button :class="{ on: reqKind === 'I' }" @click="reqKind = 'I'">질문</button>
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
        <input class="field" v-model="reqBody" :placeholder="reqKind === 'W' ? '무엇을 만들지 / 할지' : '무엇을 물어볼지'" @keyup.enter="sendRequest" :disabled="reqSending" />
        <button class="btn" @click="sendRequest" :disabled="reqSending || !reqBody.trim()"><Icon name="send" :size="15" />{{ reqSending ? '…' : '요청' }}</button>
      </div>
    </template>
    <div class="hint">메시지는 팀과의 대화이고, 요청은 직원에게 작업·질문을 맡깁니다. 처리는 협업 엔진이 켜져 있을 때 진행됩니다.</div>
  </div>
</template>
