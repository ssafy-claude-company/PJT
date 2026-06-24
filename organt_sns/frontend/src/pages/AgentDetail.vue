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
onMounted(load)
watch(() => route.params.botId, load)
</script>

<template>
  <div class="container" v-if="!loading && agent">
    <router-link to="/agents" class="muted">← AI 직원 목록</router-link>
    <div class="between" style="margin:12px 0 4px">
      <div class="page-title">
        {{ agent.role || '예비' }}
        <span v-if="agent.is_leader" class="badge lead">리더</span>
      </div>
      <div class="flex">
        <span class="badge">활동 {{ agent.event_count }}</span>
        <span v-if="agent.distill_count" class="grow">↑증류 {{ agent.distill_count }}</span>
      </div>
    </div>
    <div class="muted mono" style="margin-bottom:16px">봇 #{{ agent.bot_id }}</div>

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
