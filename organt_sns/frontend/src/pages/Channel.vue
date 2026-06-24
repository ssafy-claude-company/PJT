<script setup>
import { ref, onMounted, watch, nextTick, computed } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import { kindMeta, timeFmt } from '../kinds'

const route = useRoute()
const data = ref(null)
const loading = ref(true)
const draft = ref('')
const sending = ref(false)
const briefing = ref(null)
const showBrief = ref(false)
const msgsEl = ref(null)
// 입력 모드: 사람 메시지(msg) vs 봇 요청(req, Work/Info 1급)
const mode = ref('msg')
const agents = ref([])
const reqTo = ref('')
const reqKind = ref('W')
const reqBody = ref('')
const reqSending = ref(false)

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
    if (m.actor_role === runRole) runN++
    else { flush(); runRole = m.actor_role; runN = 1 }
  }
  flush()
  return out
})

function avatarColor(role) {
  let h = 0; for (const c of (role || '?')) h = (h * 31 + c.charCodeAt(0)) % 360
  return `hsl(${h} 52% 58%)`
}
const initials = (role) => (role || '?').replace(/[^가-힣A-Za-z]/g, '').slice(0, 2) || '?'

function scrollBottom() { const el = msgsEl.value; if (el) el.scrollTop = el.scrollHeight }
async function load() {
  loading.value = true; briefing.value = null; showBrief.value = false
  try { data.value = await api.channelMessages(route.params.pid) }
  finally { loading.value = false; await nextTick(); scrollBottom() }
}
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
async function sendRequest() {
  const body = reqBody.value.trim(); if (!body) return
  reqSending.value = true
  try {
    await api.makeRequest(route.params.pid, { to_id: reqTo.value || undefined, kind: reqKind.value, body })
    reqBody.value = ''
    data.value = await api.channelMessages(route.params.pid)
    await nextTick(); scrollBottom()
  } finally { reqSending.value = false }
}
onMounted(() => { load(); api.agents({ ordering: '-event_count' }).then((a) => { agents.value = a }) })
watch(() => route.params.pid, load)
</script>

<template>
  <div class="chan-head">
    <span class="h"># {{ data?.name || route.params.pid }}</span>
    <span class="muted mono" style="font-size:12px">{{ route.params.pid }}</span>
    <div class="baton">
      <button class="btn ghost" style="padding:4px 11px" @click="loadBrief">🧠 브리핑</button>
    </div>
  </div>

  <div v-if="showBrief && briefing" class="panel" style="margin:10px 18px 0">
    <h2>🧠 생성형 AI 협업 브리핑</h2>
    <div style="padding:12px 14px">
      <div class="pre">{{ briefing.text }}</div>
      <div class="muted" style="font-size:11px;margin-top:6px">
        {{ briefing.generated ? '생성형 AI' : '규칙기반' }} · 교차검증 {{ briefing.stats.cross_checks }}회 · 배포 {{ briefing.stats.deploy_count }}회
      </div>
    </div>
  </div>

  <div class="msgs" ref="msgsEl">
    <div v-if="loading" class="empty"><span class="spin"></span> 대화 불러오는 중…</div>
    <template v-else>
      <div class="day-sep">채널 시작 — 봇들의 협업 대화</div>
      <template v-for="m in rendered" :key="m.key">
        <div v-if="m.type === 'activity'" class="activity">
          <span class="ic">🔧</span> {{ m.role || '직원' }} — 작업 {{ m.n }}건 (Read·run·Edit…)
        </div>
        <div v-else-if="m.type === 'human'" class="msg human">
          <div class="av" style="background:#1f6feb;color:#fff">나</div>
          <div class="bd">
            <div class="who"><span class="nm">{{ m.author }}</span><span class="t">{{ timeFmt(m.ts) }}</span></div>
            <div class="bubble">{{ m.body }}</div>
          </div>
        </div>
        <div v-else class="msg agent">
          <router-link v-if="m.actor_id" :to="`/agents/${m.actor_id}`" class="av" :style="{ background: avatarColor(m.actor_role) }">{{ initials(m.actor_role) }}</router-link>
          <div v-else class="av" :style="{ background: avatarColor(m.actor_role) }">{{ initials(m.actor_role) }}</div>
          <div class="bd">
            <div class="who">
              <router-link v-if="m.actor_id" :to="`/agents/${m.actor_id}`" class="nm">{{ m.actor_role || '직원' }}</router-link>
              <span v-else class="nm">{{ m.actor_role || '직원' }}</span>
              <span class="ktag" :style="{ background: kindMeta(m.kind).bg, color: kindMeta(m.kind).c }">{{ kindMeta(m.kind).label }}</span>
              <span v-if="m.target_role" class="role">→ {{ m.target_role }}</span>
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
    <div class="flex" style="gap:6px;margin-bottom:8px">
      <button class="btn ghost" style="padding:4px 12px;font-size:12px"
              :style="mode === 'msg' ? 'border-color:var(--accent);color:var(--accent)' : ''" @click="mode = 'msg'">💬 메시지</button>
      <button class="btn ghost" style="padding:4px 12px;font-size:12px"
              :style="mode === 'req' ? 'border-color:var(--accent);color:var(--accent)' : ''" @click="mode = 'req'">📨 요청(봇에게)</button>
    </div>

    <div v-if="mode === 'msg'" class="row">
      <input v-model="draft" placeholder="이 채널에 메시지 남기기…" @keyup.enter="send" :disabled="sending" />
      <button class="btn" @click="send" :disabled="sending || !draft.trim()">{{ sending ? '…' : '보내기' }}</button>
    </div>

    <template v-else>
      <div class="flex" style="gap:8px;margin-bottom:6px">
        <select v-model="reqTo" style="flex:1">
          <option value="">담당 봇 (비우면 리더)</option>
          <option v-for="b in agents" :key="b.bot_id" :value="b.bot_id">{{ b.avatar || '🤖' }} {{ b.role }}{{ b.name ? (' · ' + b.name) : '' }}</option>
        </select>
        <button class="btn ghost" style="padding:7px 12px" :style="reqKind === 'W' ? 'border-color:var(--accent);color:var(--accent)' : ''" @click="reqKind = 'W'">작업</button>
        <button class="btn ghost" style="padding:7px 12px" :style="reqKind === 'I' ? 'border-color:var(--accent);color:var(--accent)' : ''" @click="reqKind = 'I'">질문</button>
      </div>
      <div class="row">
        <input v-model="reqBody" :placeholder="reqKind === 'W' ? '무엇을 만들/할지…' : '무엇을 물어볼지…'" @keyup.enter="sendRequest" :disabled="reqSending" />
        <button class="btn" @click="sendRequest" :disabled="reqSending || !reqBody.trim()">{{ reqSending ? '…' : '요청' }}</button>
      </div>
    </template>
    <div class="hint">메시지 = 사람 소통(F1303) · 요청 = 봇에게 Work/Info 1급 투입 → SYS가 처리(러너 연결 시 라이브 협업).</div>
  </div>
</template>
