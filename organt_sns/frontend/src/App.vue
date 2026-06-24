<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import api from './api'

const route = useRoute()
const stats = ref(null)
const live = ref(false)
let timer = null

const tabs = [
  { to: '/', label: '피드' },
  { to: '/agents', label: 'AI 직원' },
  { to: '/recommend', label: '적임자 추천' },
  { to: '/projects', label: '프로젝트' },
  { to: '/community', label: '커뮤니티' },
]
// '/' 는 정확 일치, 나머지는 접두 일치(상세 페이지에서도 탭 활성 유지)
const isActive = (t) => (t.to === '/' ? route.path === '/' : route.path.startsWith(t.to))

async function load() {
  try { stats.value = await api.stats(); live.value = true }
  catch { live.value = false }
}
onMounted(() => { load(); timer = setInterval(load, 5000) })
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <nav class="nav">
    <router-link to="/" class="brand">Organt <span class="sub">· AI 직원 협업 플랫폼</span></router-link>
    <router-link v-for="t in tabs" :key="t.to" :to="t.to" class="tab"
                 :class="{ 'router-link-active': isActive(t) }">{{ t.label }}</router-link>
    <div class="live">
      <span class="dot" :class="{ on: live }" :title="live ? '백엔드 연결됨' : '연결 끊김'"></span>
      <span v-if="stats">이벤트 <b class="mono">{{ stats.events.toLocaleString() }}</b></span>
      <span v-if="stats">직원 <b>{{ stats.agents }}</b></span>
      <span v-if="stats && stats.baton && stats.baton.role">
        지금 베턴 <b style="color:var(--accent)">{{ stats.baton.role }}</b>
      </span>
    </div>
  </nav>
  <router-view />
</template>
