<script setup>
import { ref, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import EventItem from '../components/EventItem.vue'

const route = useRoute()
const project = ref(null)
const events = ref([])
const briefing = ref(null)
const loading = ref(true)
const briefLoading = ref(true)

async function load() {
  loading.value = true
  briefLoading.value = true
  briefing.value = null
  const pid = route.params.pid
  try {
    project.value = await api.project(pid)
    events.value = await api.projectEvents(pid)
  } finally { loading.value = false }
  // 브리핑은 별도(생성형 AI — 키 설정 시 수초 소요 가능)
  try { briefing.value = await api.briefing(pid) } finally { briefLoading.value = false }
}
onMounted(load)
watch(() => route.params.pid, load)
</script>

<template>
  <div class="container" v-if="!loading && project">
    <router-link to="/projects" class="muted">← 프로젝트 목록</router-link>
    <div class="page-title" style="margin:12px 0 2px">{{ project.pid }} · {{ project.name }}</div>
    <div class="flex muted" style="font-size:12px;gap:14px;margin-bottom:16px">
      <span v-if="project.leader_role">리더 {{ project.leader_role }}</span>
      <span>{{ project.event_count }} 이벤트</span>
      <span>Task {{ project.task_count }}</span>
    </div>

    <div class="panel" style="margin-bottom:16px">
      <h2>🧠 생성형 AI 협업 브리핑</h2>
      <div style="padding:14px">
        <div v-if="briefLoading" class="muted"><span class="spin"></span> 브리핑 생성 중…</div>
        <template v-else-if="briefing">
          <div class="pre">{{ briefing.text }}</div>
          <div class="wrap-tags" style="margin-top:10px">
            <span v-for="(v, k) in briefing.stats.by_kind" :key="k" class="badge">{{ k }} {{ v }}</span>
          </div>
          <div class="muted" style="font-size:11px;margin-top:8px">
            {{ briefing.generated ? `생성형 AI · ${briefing.model}` : '규칙기반 폴백 (AI 키 미설정 시)' }}
            · 교차검증 {{ briefing.stats.cross_checks }}회 · 배포 {{ briefing.stats.deploy_count }}회
          </div>
        </template>
        <div v-else class="muted">브리핑을 불러오지 못했습니다</div>
      </div>
    </div>

    <div class="grid cols2">
      <div class="panel" style="align-self:start">
        <h2>협업 이벤트</h2>
        <div v-if="!events.length" class="empty">이벤트가 없습니다</div>
        <EventItem v-for="e in events" :key="e.seq" :ev="e" :show-project="false" />
      </div>
      <div class="panel" style="align-self:start">
        <h2>Task — 목표·교차검증·배포</h2>
        <div v-if="!project.tasks || !project.tasks.length" class="empty">등록된 Task가 없습니다</div>
        <div v-for="t in project.tasks" :key="t.id"
             style="padding:12px 14px;border-bottom:1px solid var(--bd2)">
          <div class="between">
            <span class="nm mono">{{ t.task_id }}</span>
            <span class="badge">{{ t.owner_role || '미정' }}</span>
          </div>
          <div class="muted" style="font-size:12px;margin:6px 0">{{ t.goal || t.purpose || '(목표 미기재)' }}</div>
          <div class="flex" style="gap:12px">
            <span class="badge">교차검증 {{ t.cross_checks }}</span>
            <span class="badge ok">배포 {{ t.deploy_count }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div v-else class="container empty"><span class="spin"></span></div>
</template>
