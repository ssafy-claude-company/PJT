<script setup>
import { ref, onMounted, onUnmounted, watch, nextTick, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '../api'
import { kindMeta, timeFmt } from '../kinds'
import CollabPanel from '../components/CollabPanel.vue'
import Icon from '../components/Icon.vue'
import { monogram, avatarColor, avatarBg } from '../avatar'

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

// 대화(conversation) 종류 — 버블로. 그 외(work/raw/experience)는 활동 줄로 접는다.
const CONV = new Set(['delegation', 'consultation', 'goal_set', 'meeting', 'verification',
  'deploy', 'task_complete', 'recruit', 'agent_learned', 'convergence_alert', 'user_request', 'intervention'])

const rendered = computed(() => {
  const out = []
  let runRole = null, runN = 0
  const flush = () => { if (runN) { out.push({ type: 'activity', role: runRole, n: runN, key: 'a' + out.length }); runN = 0; runRole = null } }
  for (const m of (data.value?.messages || [])) {
    if (m.type === 'human') { flush(); out.push(m); continue }
    if (CONV.has(m.kind)) { flush(); out.push(m); continue }
    const who = m.actor_name || m.actor_role
    if (who === runRole) runN++
    else { flush(); runRole = who; runN = 1 }
  }
  flush()
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
  try {
    const fresh = await api.channelMessages(route.params.pid)
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
    data.value.messages.push(m); draft.value = ''
    await nextTick(); scrollBottom()
  } finally { sending.value = false }
}
async function loadBrief() {
  showBrief.value = !showBrief.value
  if (showBrief.value && !briefing.value) briefing.value = await api.briefing(route.params.pid)
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
const REASON_LABEL = { role_match: '직군 적합', keyword_overlap: '키워드 일치', expertise: '전문성', track_record: '실적' }
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
    reqBody.value = ''
    data.value = await api.channelMessages(route.params.pid)
    await nextTick(); scrollBottom()
  } finally { reqSending.value = false }
}
// 채널 관리(관리 기능)
const isMine = computed(() => !/^P-/.test(route.params.pid))
async function doRename() {
  menu.value = false
  const name = prompt('새 채널 이름:', data.value?.name || '')
  if (!name || !name.trim()) return
  const r = await api.renameChannel(route.params.pid, name.trim())
  if (data.value) data.value.name = r.name
}
async function doArchive() {
  menu.value = false
  await api.archiveChannel(route.params.pid)
}
async function doRemove() {
  menu.value = false
  if (!confirm(`채널 '${data.value?.name || route.params.pid}'을(를) 삭제할까요? 되돌릴 수 없습니다.`)) return
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
watch(() => route.params.pid, () => { live.value = false; load() })
</script>

<template>
  <div class="chan-head">
    <span class="h">{{ data?.name || route.params.pid }}</span>
    <span class="cid">{{ route.params.pid }}</span>
    <span v-if="batonHere" class="live-baton"><i class="pulse"></i>{{ batonHere }} 작업 중</span>
    <span v-else-if="live" class="live-tag" title="라이브 — 자동 갱신 중"><i></i>LIVE</span>
    <div class="baton">
      <button class="iconbtn" :class="{ on: showStruct }" title="협업 구조" @click="showStruct = !showStruct"><Icon name="network" /></button>
      <button class="iconbtn" :class="{ on: showBrief }" title="AI 브리핑" @click="loadBrief"><Icon name="spark" /></button>
      <div class="ch-menu">
        <button class="iconbtn" title="채널 관리" @click="menu = !menu"><Icon name="more" /></button>
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
    대기 중인 봇 요청 <b>{{ data.pending_count }}건</b> — 요청은 정상 접수됐습니다. 실제 협업은 <b>라이브 러너가 켜져 있을 때</b> 처리됩니다.
  </div>

  <div v-if="showBrief && briefing" class="panel" style="margin:12px 20px 0">
    <h2>생성형 AI 협업 브리핑</h2>
    <div style="padding:14px 16px">
      <div class="pre">{{ briefing.text }}</div>
      <div class="muted" style="font-size:11px;margin-top:8px">
        {{ briefing.generated ? '생성형 AI' : '규칙기반' }} · 교차검증 {{ briefing.stats.cross_checks }}회 · 배포 {{ briefing.stats.deploy_count }}회
      </div>
    </div>
  </div>

  <div class="msgs" ref="msgsEl" @scroll.passive="onScroll">
    <div v-if="loading" class="empty"><span class="spin"></span> 대화 불러오는 중…</div>
    <template v-else>
      <div class="day-sep">채널 시작 — 봇들의 협업 대화</div>
      <template v-for="m in rendered" :key="m.key">
        <div v-if="m.type === 'activity'" class="activity">
          <span class="dotmark"></span>{{ m.role || '직원' }} — 작업 {{ m.n }}건 (Read · run · Edit)
        </div>
        <div v-else-if="m.type === 'human'" class="msg human">
          <div class="av" style="background:var(--accent)">나</div>
          <div class="bd">
            <div class="who"><span class="nm">{{ m.author }}</span><span class="t">{{ timeFmt(m.ts) }}</span></div>
            <div class="bubble">{{ m.body }}</div>
          </div>
        </div>
        <div v-else class="msg agent">
          <router-link v-if="m.actor_id" :to="`/agents/${m.actor_id}`" class="av" :style="{ background: avatarColor(m.actor_name || m.actor_role) }">{{ monogram(m.actor_name, m.actor_role) }}</router-link>
          <div v-else class="av" :style="{ background: avatarColor(m.actor_name || m.actor_role) }">{{ monogram(m.actor_name, m.actor_role) }}</div>
          <div class="bd">
            <div class="who">
              <router-link v-if="m.actor_id" :to="`/agents/${m.actor_id}`" class="nm">{{ m.actor_name || m.actor_role || '직원' }}</router-link>
              <span v-else class="nm">{{ m.actor_name || m.actor_role || '직원' }}</span>
              <span v-if="m.actor_name && m.actor_role" class="role">{{ m.actor_role }}</span>
              <span class="ktag" :style="{ background: kindMeta(m.kind).bg, color: kindMeta(m.kind).c }">{{ kindMeta(m.kind).label }}</span>
              <span v-if="m.target_name || m.target_role" class="role">→ {{ m.target_name || m.target_role }}</span>
              <span class="t">{{ timeFmt(m.ts) }}</span>
            </div>
            <div class="txt">{{ m.summary }}</div>
          </div>
        </div>
      </template>
      <div v-if="!rendered.length" class="empty">아직 메시지가 없습니다</div>
    </template>
  </div>

  <div class="composer">
    <div class="seg" style="margin-bottom:10px">
      <button :class="{ on: mode === 'msg' }" @click="mode = 'msg'"><Icon name="message" :size="15" />메시지</button>
      <button :class="{ on: mode === 'req' }" @click="mode = 'req'"><Icon name="send" :size="15" />봇에게 요청</button>
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
            <span v-else class="ph">담당 봇 선택 (비우면 리더)</span>
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
        <button class="btn ghost sm" :disabled="recLoading || !reqBody.trim()" @click="suggest" title="이 일에 적합한 봇 추천">
          <Icon name="target" :size="15" />{{ recLoading ? '…' : '적임자' }}
        </button>
      </div>
      <div v-if="recs.length" class="recs">
        <span class="muted" style="font-size:11.5px">추천</span>
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
    <div class="hint">메시지는 사람 소통, 요청은 봇에게 작업·질문을 1급으로 투입합니다 — 러너 연결 시 라이브 협업.</div>
  </div>
</template>
