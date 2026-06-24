<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from './api'

const route = useRoute()
const router = useRouter()
const channels = ref([])
const stats = ref(null)
let timer = null

async function newChannel() {
  const name = prompt('새 프로젝트(채널) 이름:')
  if (!name || !name.trim()) return
  const c = await api.createChannel({ name: name.trim() })
  await load()
  router.push(`/channels/${c.pid}`)
}

async function load() {
  try {
    const [p, s] = await Promise.all([api.projects({ ordering: '-event_count' }), api.stats()])
    // SNS-네이티브 채널(U-/S-)은 활동 0이어도 항상 보인다(사용자가 만든 것). P-(디스코드)는 활동 있을 때만.
    const vis = p.filter((c) => c.event_count > 0 || c.message_count > 0 || !/^P-/.test(c.pid))
    // 내가 만든 채널(U-/S-)을 위로 — 디스코드 쇼케이스(P-)는 그 아래.
    const isMine = (c) => !/^P-/.test(c.pid)
    channels.value = vis.sort((a, b) => (isMine(b) - isMine(a)) || (b.event_count - a.event_count))
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
        <router-link to="/studio" class="sb-item" :class="{ active: route.path === '/studio' }">
          <span class="hash">⚙</span><span class="nm">봇 스튜디오</span>
        </router-link>
        <router-link to="/agents" class="sb-item" :class="{ active: route.path.startsWith('/agents') }">
          <span class="hash">#</span><span class="nm">AI 직원</span>
        </router-link>
        <router-link to="/recommend" class="sb-item" :class="{ active: route.path === '/recommend' }">
          <span class="hash">#</span><span class="nm">적임자 추천</span>
        </router-link>

        <div class="sb-sec between" style="display:flex;align-items:center;justify-content:space-between">
          <span>채널 · {{ channels.length }}</span>
          <span class="hash" style="cursor:pointer;font-size:15px" title="새 프로젝트" @click="newChannel">＋</span>
        </div>
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
