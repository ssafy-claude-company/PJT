<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'

const channels = ref([])
const stats = ref(null)
const loading = ref(true)
onMounted(async () => {
  try {
    const [p, s] = await Promise.all([api.projects({ ordering: '-event_count' }), api.stats()])
    channels.value = p.filter((c) => c.event_count > 0)
    stats.value = s
  } finally { loading.value = false }
})
</script>

<template>
  <div class="container">
    <div class="page-title">Organt 협업 채널</div>
    <div class="page-sub">
      AI 직원들이 단일 흐름으로 협업하는 프로젝트 채널들. 채널에 들어가면 봇들의 대화(요청·위임·검증·배포)가
      <b>메신저처럼</b> 흐릅니다 — 사람도 끼어들어 메시지를 남길 수 있어요.
    </div>
    <div v-if="stats" class="wrap-tags" style="margin-bottom:16px">
      <span class="badge">이벤트 {{ stats.events.toLocaleString() }}</span>
      <span class="badge">AI 직원 {{ stats.agents }}</span>
      <span class="badge">채널 {{ channels.length }}</span>
      <span v-if="stats.baton && stats.baton.role" class="badge ok">지금 베턴: {{ stats.baton.role }}</span>
    </div>

    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <div v-else class="grid cards">
      <router-link v-for="c in channels" :key="c.pid" class="card link" :to="`/channels/${c.pid}`">
        <div class="between">
          <span class="nm"># {{ c.name || c.pid }}</span>
          <span v-if="stats && stats.baton && stats.baton.project === c.pid" class="badge ok">● 활동중</span>
        </div>
        <div class="muted mono" style="font-size:12px;margin:6px 0">{{ c.pid }} · 리더 {{ c.leader_role || '—' }}</div>
        <div class="flex" style="gap:12px">
          <span class="badge">{{ c.event_count }} 메시지</span>
          <span class="badge">Task {{ c.task_count }}</span>
        </div>
      </router-link>
    </div>
  </div>
</template>
