<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'

const projects = ref([])
const loading = ref(true)
onMounted(async () => {
  projects.value = await api.projects()
  loading.value = false
})
</script>

<template>
  <div class="container">
    <div class="page-title">프로젝트</div>
    <div class="page-sub">AI 직원들이 단일 흐름으로 완성해 온 프로젝트들. 카드를 눌러 협업 서사와 생성형 AI 브리핑을 확인하세요.</div>

    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <div v-else class="grid cards">
      <router-link v-for="p in projects" :key="p.pid" class="card link" :to="`/projects/${p.pid}`">
        <div class="between">
          <span class="nm mono">{{ p.pid }}</span>
          <span class="badge" :class="{ ok: p.event_count > 0 }">{{ p.event_count }} 이벤트</span>
        </div>
        <div style="font-weight:700;margin:8px 0 6px">{{ p.name || '(이름 없음)' }}</div>
        <div class="flex muted" style="font-size:12px;gap:14px">
          <span v-if="p.leader_role">리더 {{ p.leader_role }}</span>
          <span>Task {{ p.task_count }}</span>
        </div>
      </router-link>
    </div>
  </div>
</template>
