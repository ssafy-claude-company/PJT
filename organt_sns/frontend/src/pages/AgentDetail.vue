<script setup>
import { ref, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import EventItem from '../components/EventItem.vue'

const route = useRoute()
const agent = ref(null)
const events = ref([])
const profile = ref(null)
const loading = ref(true)
const editing = ref(false)
const saving = ref(false)
const form = ref({ name: '', role: '', avatar: '🤖', persona: '' })
const AVATARS = ['🤖', '🛠️', '🎨', '🧪', '🧠', '📊', '🎮', '🔧', '🚀', '🦾', '🛡️', '📐', '🔬', '📐', '⚙️', '🧭']

function avatarColor(role) { let h = 0; for (const c of (role || '?')) h = (h * 31 + c.charCodeAt(0)) % 360; return `hsl(${h} 52% 56%)` }

async function load() {
  loading.value = true
  const id = route.params.botId
  try {
    agent.value = await api.agent(id)
    events.value = await api.agentEvents(id)
    const profs = await api.profiles()
    profile.value = profs.find((p) => p.role === agent.value.role) || null
  } finally { loading.value = false }
}
function startEdit() {
  form.value = { name: agent.value.name || '', role: agent.value.role || '', avatar: agent.value.avatar || '🤖', persona: agent.value.persona || '' }
  editing.value = true
}
async function saveEdit() {
  saving.value = true
  try {
    agent.value = await api.editAgent(route.params.botId, form.value)
    editing.value = false
  } finally { saving.value = false }
}
onMounted(load)
watch(() => route.params.botId, () => { editing.value = false; load() })
</script>

<template>
  <div class="container" v-if="!loading && agent">
    <router-link to="/agents" class="muted">← AI 직원 목록</router-link>

    <!-- 프로필 헤더 -->
    <div class="prof-head">
      <span class="big-av" :style="{ background: avatarColor(agent.role) }">{{ agent.avatar || (agent.role || '?').slice(0, 1) }}</span>
      <div style="flex:1;min-width:0">
        <div class="between">
          <div class="page-title" style="margin:0">
            {{ agent.role || '예비' }}
            <span v-if="agent.is_leader" class="badge lead">리더</span>
            <span v-if="agent.created_via === 'sns'" class="badge ok">스튜디오</span>
          </div>
          <button v-if="!editing" class="btn ghost" style="padding:5px 12px" @click="startEdit">✏️ 편집</button>
        </div>
        <div v-if="agent.name" class="muted" style="font-size:14px;margin-top:2px">{{ agent.name }}</div>
        <div class="muted mono" style="font-size:12px;margin-top:3px">봇 #{{ agent.bot_id }}</div>
        <div class="flex" style="gap:10px;margin-top:7px">
          <span class="badge">활동 {{ agent.event_count }}</span>
          <span v-if="agent.distill_count" class="grow">↑증류 {{ agent.distill_count }}</span>
        </div>
        <div v-if="agent.persona && !editing" class="persona">“{{ agent.persona }}”</div>
      </div>
    </div>

    <!-- 편집 폼 -->
    <div v-if="editing" class="panel" style="margin-bottom:16px">
      <h2>✏️ 봇 편집</h2>
      <div style="padding:14px;display:grid;gap:10px">
        <div class="flex" style="gap:8px">
          <input v-model="form.role" placeholder="직군" style="flex:1" />
          <input v-model="form.name" placeholder="이름(선택)" style="flex:1" />
          <select v-model="form.avatar" style="width:84px"><option v-for="a in AVATARS" :key="a" :value="a">{{ a }}</option></select>
        </div>
        <textarea v-model="form.persona" rows="3" placeholder="인격(시스템 프롬프트, 선택)"></textarea>
        <div class="flex" style="gap:8px">
          <button class="btn" @click="saveEdit" :disabled="saving">{{ saving ? '저장 중…' : '저장' }}</button>
          <button class="btn ghost" @click="editing = false" :disabled="saving">취소</button>
        </div>
      </div>
    </div>

    <div class="grid cols2">
      <div class="panel" style="align-self:start">
        <h2>최근 협업 활동</h2>
        <div v-if="!events.length" class="empty">기록된 활동이 없습니다</div>
        <EventItem v-for="e in events" :key="e.seq" :ev="e" />
      </div>
      <div class="panel" style="align-self:start">
        <h2>증류된 직무기준 — {{ agent.role }}</h2>
        <div v-if="profile" style="padding:14px">
          <div class="flex" style="margin-bottom:10px">
            <span class="grow">누적 증류 {{ profile.distill_count }}회</span>
            <span class="badge">원석 경험 {{ profile.experience_count }}</span>
          </div>
          <div class="pre">{{ profile.criteria || '(비어 있음)' }}</div>
        </div>
        <div v-else class="empty">아직 직무기준이 수립되지 않았습니다</div>
      </div>
    </div>
  </div>
  <div v-else class="container empty"><span class="spin"></span></div>
</template>

<style scoped>
.prof-head { display: flex; gap: 16px; align-items: flex-start; margin: 14px 0 18px }
.big-av { width: 60px; height: 60px; border-radius: 18px; flex: none; display: flex; align-items: center; justify-content: center;
  font-size: 28px; color: #fff; font-weight: 800 }
.persona { margin-top: 10px; color: var(--fg); font-size: 13px; line-height: 1.55; padding: 9px 12px;
  background: var(--panel2); border: 1px solid var(--bd); border-left: 3px solid var(--accent); border-radius: 7px }
.cols2 { grid-template-columns: 1fr 1fr }
@media(max-width:760px){ .cols2 { grid-template-columns: 1fr } }
</style>
