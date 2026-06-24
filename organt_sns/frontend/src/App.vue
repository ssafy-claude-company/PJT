<script setup>
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from './api'
import Icon from './components/Icon.vue'

const route = useRoute()
const router = useRouter()
const channels = ref([])
const stats = ref(null)
const drawer = ref(false)        // 모바일 사이드바 열림
const q = ref('')                // 채널 필터
let timer = null

async function newChannel() {
  const name = prompt('새 프로젝트(채널) 이름:')
  if (!name || !name.trim()) return
  const c = await api.createChannel({ name: name.trim() })
  await load()
  drawer.value = false
  router.push(`/channels/${c.pid}`)
}

async function load() {
  try {
    const [p, s] = await Promise.all([api.projects({ ordering: '-event_count' }), api.stats()])
    // SNS-네이티브 채널(U-/S-)은 활동 0이어도 항상 보인다(사용자가 만든 것). P-(디스코드)는 활동 있을 때만.
    const vis = p.filter((c) => c.event_count > 0 || c.message_count > 0 || !/^P-/.test(c.pid))
    const isMine = (c) => !/^P-/.test(c.pid)
    const arch = (c) => (c.status === 'archived' ? 1 : 0)
    // 보관된 채널은 맨 아래, 그 다음 내가 만든 채널 우선, 활동 순.
    channels.value = vis.sort((a, b) => (arch(a) - arch(b)) || (isMine(b) - isMine(a)) || (b.event_count - a.event_count))
    stats.value = s
  } catch (e) { /* keep last */ }
}
onMounted(() => { load(); timer = setInterval(load, 8000) })
onUnmounted(() => clearInterval(timer))
watch(() => route.fullPath, () => { drawer.value = false })   // 이동하면 드로어 닫힘
const activePid = computed(() => route.params.pid)
const shown = computed(() => {
  const t = q.value.trim().toLowerCase()
  if (!t) return channels.value
  return channels.value.filter((c) => (c.name || c.pid).toLowerCase().includes(t) || c.pid.toLowerCase().includes(t))
})
const isLive = computed(() => stats.value?.baton?.role)
</script>

<template>
  <div class="shell">
    <!-- 모바일 탑바 -->
    <header class="topbar">
      <button class="burger" @click="drawer = true" aria-label="메뉴"><Icon name="menu" :size="20" /></button>
      <router-link to="/" class="tb-brand">Organt</router-link>
      <span v-if="isLive" class="live-tag"><i></i>LIVE</span>
    </header>

    <div v-if="drawer" class="scrim" @click="drawer = false"></div>
    <aside class="sidebar" :class="{ open: drawer }">
      <router-link to="/" class="sb-brand">
        <span class="wm"><Icon class="mark" name="layers" :size="18" />Organt</span>
        <span class="sub">AI 직원 협업 워크스페이스</span>
      </router-link>
      <div class="sb-scroll">
        <div class="sb-sec">둘러보기</div>
        <router-link to="/studio" class="sb-item" :class="{ active: route.path === '/studio' }">
          <Icon class="ic" name="sliders" /><span class="nm">봇 스튜디오</span>
        </router-link>
        <router-link to="/agents" class="sb-item" :class="{ active: route.path.startsWith('/agents') }">
          <Icon class="ic" name="bot" /><span class="nm">AI 직원</span>
        </router-link>
        <router-link to="/recommend" class="sb-item" :class="{ active: route.path === '/recommend' }">
          <Icon class="ic" name="target" /><span class="nm">적임자 추천</span>
        </router-link>

        <div class="sb-sec between">
          <span>채널 · {{ channels.length }}</span>
          <span class="sb-add" title="새 프로젝트" @click="newChannel"><Icon name="plus" :size="16" /></span>
        </div>
        <input v-if="channels.length > 6" v-model="q" class="field sb-search" placeholder="채널 검색" />
        <router-link v-for="c in shown" :key="c.pid" :to="`/channels/${c.pid}`"
                     class="sb-item" :class="{ active: activePid === c.pid, archived: c.status === 'archived' }">
          <Icon class="ic" name="hash" :size="15" />
          <span class="nm">{{ c.name || c.pid }}</span>
          <Icon v-if="c.status === 'archived'" class="arch-tag" name="archive" :size="14" />
          <span v-else-if="stats && stats.baton && stats.baton.project === c.pid" class="dot" title="지금 활동 중"></span>
        </router-link>
        <div v-if="!channels.length" class="muted" style="padding:8px 10px;font-size:12px">불러오는 중…</div>
        <div v-else-if="!shown.length" class="muted" style="padding:8px 10px;font-size:12px">검색 결과 없음</div>
      </div>
    </aside>
    <main class="main"><router-view /></main>
  </div>
</template>
