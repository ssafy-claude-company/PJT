<script setup>
import { ref, onMounted, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import EventItem from '../components/EventItem.vue'
import Icon from '../components/Icon.vue'
import { monogram, avatarBg, AVATAR_COLORS } from '../avatar'
import { me } from '../user'

const route = useRoute()
const agent = ref(null)
const isMine = computed(() => agent.value && agent.value.owner_handle && agent.value.owner_handle === me.handle)
const events = ref([])
const profile = ref(null)
const loading = ref(true)
const editing = ref(false)
const saving = ref(false)
const form = ref({ name: '', role: '', avatar: '', persona: '' })

const error = ref(false)
async function load() {
  loading.value = true; error.value = false
  const id = route.params.botId
  try {
    agent.value = await api.agent(id)
    events.value = await api.agentEvents(id)
    const profs = await api.profiles()
    profile.value = profs.find((p) => p.role === agent.value.role) || null
  } catch (e) { error.value = true; agent.value = null }
  finally { loading.value = false }
}
function startEdit() {
  form.value = { name: agent.value.name || '', role: agent.value.role || '', avatar: agent.value.avatar || '', persona: agent.value.persona || '' }
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
    <router-link to="/agents" class="back"><Icon name="arrowL" :size="15" />우리 직원</router-link>

    <div class="prof-head">
      <span class="big-av" :style="{ background: avatarBg(agent) }">{{ monogram(agent.name, agent.role) }}</span>
      <div style="flex:1;min-width:0">
        <div class="between">
          <div>
            <div class="page-title" style="margin:0">{{ agent.name || '이름 없음' }}</div>
            <div class="flex" style="gap:7px;margin-top:4px">
              <span class="muted" style="font-size:14px">{{ agent.role || '대기 중' }}</span>
              <span v-if="agent.is_leader" class="badge lead">리더</span>
              <span v-if="isMine" class="badge accent">내 직원</span>
              <span v-else class="badge">공개 직원</span>
            </div>
          </div>
          <button v-if="!editing && isMine" class="btn ghost sm" @click="startEdit"><Icon name="edit" :size="15" />정보 수정</button>
        </div>
        <div class="muted mono" style="font-size:12px;margin-top:8px">활동 {{ agent.event_count }}<span v-if="agent.distill_count"> · 성장 {{ agent.distill_count }}</span></div>
        <div v-if="agent.persona && !editing" class="persona">{{ agent.persona }}</div>
      </div>
    </div>

    <div v-if="editing" class="panel" style="margin-bottom:18px">
      <h2>직원 정보 수정</h2>
      <div style="padding:18px;display:grid;gap:14px">
        <div>
          <label class="lbl">이름</label>
          <input v-model="form.name" placeholder="직원 이름" />
        </div>
        <div>
          <label class="lbl">역할</label>
          <input v-model="form.role" placeholder="역할 — 맡은 일" />
        </div>
        <div>
          <label class="lbl">아바타 색</label>
          <div class="av-grid">
            <button class="sw" :class="{ on: !form.avatar }" :style="{ background: avatarBg({ name: form.name, role: form.role }) }" title="자동" @click="form.avatar = ''"></button>
            <button v-for="c in AVATAR_COLORS" :key="c" class="sw" :class="{ on: form.avatar === c }" :style="{ background: c }" @click="form.avatar = c"></button>
          </div>
        </div>
        <div>
          <label class="lbl">성격</label>
          <textarea v-model="form.persona" rows="3" placeholder="어떻게 일하면 좋을지 (선택)"></textarea>
        </div>
        <div class="flex" style="gap:8px">
          <button class="btn" @click="saveEdit" :disabled="saving">{{ saving ? '저장 중…' : '저장' }}</button>
          <button class="btn ghost" @click="editing = false" :disabled="saving">취소</button>
        </div>
      </div>
    </div>

    <div class="grid cols2">
      <div class="panel" style="align-self:start">
        <h2>최근 활동</h2>
        <div v-if="!events.length" class="empty">아직 활동 기록이 없어요</div>
        <EventItem v-for="e in events" :key="e.seq" :ev="e" />
      </div>
      <div class="panel" style="align-self:start">
        <h2>쌓은 노하우 · {{ agent.role }}</h2>
        <div v-if="profile" style="padding:16px">
          <div class="flex" style="margin-bottom:12px">
            <span class="grow">성장 {{ profile.distill_count }}회</span>
            <span class="badge">새 경험 {{ profile.experience_count }}</span>
          </div>
          <div class="pre">{{ profile.criteria || '(아직 없어요)' }}</div>
        </div>
        <div v-else class="empty">아직 쌓은 노하우가 없어요</div>
      </div>
    </div>
  </div>
  <div v-else-if="error" class="container empty">직원을 찾을 수 없습니다. <router-link to="/agents" class="muted" style="text-decoration:underline">목록으로</router-link></div>
  <div v-else class="container empty"><span class="spin"></span></div>
</template>

<style scoped>
.back { display: inline-flex; align-items: center; gap: 5px; color: var(--text3); font-size: 13px }
.back:hover { color: var(--text) }
.prof-head { display: flex; gap: 18px; align-items: flex-start; margin: 16px 0 22px }
.big-av { width: 64px; height: 64px; border-radius: 19px; flex: none; display: flex; align-items: center; justify-content: center;
  font-size: 27px; color: #fff; font-weight: 700 }
.persona { margin-top: 14px; color: var(--text); font-size: 13px; line-height: 1.6; padding: 11px 14px;
  background: var(--surface2); border: 1px solid var(--line); border-left: 2px solid var(--accent); border-radius: var(--r) }
.lbl { display: block; font-size: 11px; color: var(--text3); font-weight: 600; letter-spacing: .04em; text-transform: uppercase; margin-bottom: 7px }
.sw { width: 30px; height: 30px; border-radius: 50%; border: 2px solid transparent; cursor: pointer; transition: .12s; outline: 1px solid var(--line) }
.sw:hover { transform: scale(1.08) }
.sw.on { border-color: var(--text); outline-color: var(--text) }
.cols2 { grid-template-columns: 1fr 1fr }
@media(max-width:760px){ .cols2 { grid-template-columns: 1fr } }
</style>
