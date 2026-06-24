<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import api from '../api'
import EventItem from '../components/EventItem.vue'
import { KIND } from '../kinds'

const events = ref([])
const stats = ref(null)
const kindFilter = ref('')
const loading = ref(true)
let timer = null

const kindOptions = computed(() => {
  const bk = stats.value?.by_kind || {}
  return Object.entries(bk).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => ({ k, n, label: KIND[k]?.label || k }))
})

async function load() {
  try {
    const [ev, st] = await Promise.all([
      api.events(kindFilter.value ? { kind: kindFilter.value } : {}),
      api.stats(),
    ])
    events.value = ev.results || []
    stats.value = st
  } finally { loading.value = false }
}
function setKind(k) { kindFilter.value = kindFilter.value === k ? '' : k; load() }
onMounted(() => { load(); timer = setInterval(load, 6000) })
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="container grid cols2">
    <div class="panel" style="align-self:start">
      <h2>
        협업 피드 — AI 직원들이 지금 무엇을 하는가
        <span v-if="kindFilter" class="muted">· {{ KIND[kindFilter]?.label || kindFilter }} 필터</span>
      </h2>
      <div v-if="loading" class="empty"><span class="spin"></span> 불러오는 중…</div>
      <div v-else-if="!events.length" class="empty">이벤트가 없습니다</div>
      <EventItem v-for="e in events" :key="e.seq" :ev="e" />
    </div>

    <div class="grid" style="gap:16px;align-content:start">
      <div class="panel" style="padding:14px">
        <h2 style="border:0;padding:0 0 8px;margin:0">지금 베턴 (단일 흐름)</h2>
        <template v-if="stats && stats.baton">
          <div style="font-size:18px;font-weight:800">{{ stats.baton.role || '유휴' }}</div>
          <div class="muted" style="font-size:12px;margin:2px 0">{{ stats.baton.summary }}</div>
          <router-link v-if="stats.baton.project" class="p-tag" :to="`/projects/${stats.baton.project}`">
            {{ stats.baton.project }}
          </router-link>
        </template>
        <div v-else class="muted">유휴 — 활동 흐름 없음</div>
      </div>

      <div class="panel">
        <h2>이벤트 종류 — 클릭해 필터</h2>
        <div v-for="o in kindOptions" :key="o.k" class="row link"
             :style="kindFilter === o.k ? 'background:var(--panel2)' : ''" @click="setKind(o.k)">
          <span class="k" :style="{ background: KIND[o.k]?.bg, color: KIND[o.k]?.c }">{{ o.label }}</span>
          <span class="badge mono">{{ o.n.toLocaleString() }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
