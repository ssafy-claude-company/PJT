<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import api from './api'

const route = useRoute()
const channels = ref([])
const stats = ref(null)
let timer = null

async function load() {
  try {
    const [p, s] = await Promise.all([api.projects({ ordering: '-event_count' }), api.stats()])
    channels.value = p.filter((c) => c.event_count > 0)
    stats.value = s
  } catch (e) { /* keep last */ }
}
onMounted(() => { load(); timer = setInterval(load, 8000) })
onUnmounted(() => clearInterval(timer))
const activePid = computed(() => route.params.pid)
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <router-link to="/" class="sb-brand">
        Organt<span class="sub">AI 직원 협업 · 상위 Discord</span>
      </router-link>
      <div class="sb-scroll">
        <div class="sb-sec">둘러보기</div>
        <router-link to="/agents" class="sb-item" :class="{ active: route.path.startsWith('/agents') }">
          <span class="hash">#</span><span class="nm">AI 직원</span>
        </router-link>
        <router-link to="/recommend" class="sb-item" :class="{ active: route.path === '/recommend' }">
          <span class="hash">#</span><span class="nm">적임자 추천</span>
        </router-link>

        <div class="sb-sec">채널 · 프로젝트 {{ channels.length }}</div>
        <router-link v-for="c in channels" :key="c.pid" :to="`/channels/${c.pid}`"
                     class="sb-item" :class="{ active: activePid === c.pid }">
          <span class="hash">#</span>
          <span class="nm">{{ c.name || c.pid }}</span>
          <span v-if="stats && stats.baton && stats.baton.project === c.pid" class="dot" title="지금 활동 중"></span>
        </router-link>
        <div v-if="!channels.length" class="muted" style="padding:8px 10px;font-size:12px">불러오는 중…</div>
      </div>
    </aside>
    <main class="main"><router-view /></main>
  </div>
</template>
