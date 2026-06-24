<script setup>
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from './api'
import Icon from './components/Icon.vue'
import Dialog from './components/Dialog.vue'
import SignIn from './components/SignIn.vue'
import { askPrompt } from './dialog'
import { me, loadMe, isGuest } from './user'
import { avatarColor } from './avatar'

const route = useRoute()
const router = useRouter()
const channels = ref([])
const stats = ref(null)
const drawer = ref(false)
const q = ref('')
const showSignIn = ref(false)
let timer = null

async function newChannel() {
  if (isGuest()) { showSignIn.value = true; return }
  const name = await askPrompt({ title: '새 프로젝트', placeholder: '채널 이름' })
  if (!name) return
  const c = await api.createChannel({ name })
  await load()
  drawer.value = false
  router.push(`/channels/${c.pid}`)
}

async function load() {
  try {
    const [p, s] = await Promise.all([api.projects({ ordering: '-event_count' }), api.stats()])
    const vis = p.filter((c) => c.event_count > 0 || c.message_count > 0 || !/^P-/.test(c.pid))
    const isMine = (c) => !/^P-/.test(c.pid)
    const arch = (c) => (c.status === 'archived' ? 1 : 0)
    channels.value = vis.sort((a, b) => (arch(a) - arch(b)) || (isMine(b) - isMine(a)) || (b.event_count - a.event_count))
    stats.value = s
  } catch (e) { /* keep last */ }
}
onMounted(async () => {
  await loadMe()
  // 첫 방문에 한 번만 환영(닫을 수 있음). 둘러보기는 게스트로도 가능, 소셜 행동 때 가입 유도.
  if (isGuest() && !localStorage.getItem('organt_seen')) { showSignIn.value = true; localStorage.setItem('organt_seen', '1') }
  load(); timer = setInterval(load, 8000)
})
onUnmounted(() => clearInterval(timer))
watch(() => route.fullPath, () => { drawer.value = false })
const activePid = computed(() => route.params.pid)
const shown = computed(() => {
  const t = q.value.trim().toLowerCase()
  if (!t) return channels.value
  return channels.value.filter((c) => (c.name || c.pid).toLowerCase().includes(t) || c.pid.toLowerCase().includes(t))
})
const isLive = computed(() => stats.value?.baton?.role)
const meBg = computed(() => me.color || avatarColor(me.handle || 'guest'))
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <button class="burger" @click="drawer = true" aria-label="메뉴"><Icon name="menu" :size="20" /></button>
      <router-link to="/" class="tb-brand">Organt</router-link>
      <span v-if="isLive" class="live-tag"><i></i>LIVE</span>
    </header>

    <div v-if="drawer" class="scrim" @click="drawer = false"></div>
    <aside class="sidebar" :class="{ open: drawer }">
      <router-link to="/" class="sb-brand">
        <span class="wm"><Icon class="mark" name="layers" :size="18" /><span class="wt">Organt</span></span>
        <span class="sub">친구와 AI 직원이 함께 일하는 곳</span>
      </router-link>
      <div class="sb-scroll">
        <div class="sb-sec">둘러보기</div>
        <router-link to="/friends" class="sb-item" :class="{ active: route.path === '/friends' }">
          <Icon class="ic" name="user" /><span class="nm">친구</span>
        </router-link>
        <router-link to="/studio" class="sb-item" :class="{ active: route.path === '/studio' }">
          <Icon class="ic" name="sliders" /><span class="nm">직원 만들기</span>
        </router-link>
        <router-link to="/agents" class="sb-item" :class="{ active: route.path.startsWith('/agents') }">
          <Icon class="ic" name="bot" /><span class="nm">우리 직원</span>
        </router-link>
        <router-link to="/recommend" class="sb-item" :class="{ active: route.path === '/recommend' }">
          <Icon class="ic" name="target" /><span class="nm">직원 찾기</span>
        </router-link>

        <div class="sb-sec between">
          <span>채널 · {{ channels.length }}</span>
          <button class="sb-add" title="새 프로젝트" aria-label="새 프로젝트" @click="newChannel"><Icon name="plus" :size="16" /></button>
        </div>
        <input v-if="channels.length > 6" v-model="q" class="field sb-search" placeholder="채널 검색" />
        <router-link v-for="c in shown" :key="c.pid" :to="`/channels/${c.pid}`"
                     class="sb-item" :class="{ active: activePid === c.pid, archived: c.status === 'archived' }">
          <Icon class="ic" name="hash" :size="15" />
          <span class="nm">{{ c.name || c.pid }}</span>
          <Icon v-if="c.status === 'archived'" class="arch-tag" name="archive" :size="14" />
          <span v-else-if="stats && stats.baton && stats.baton.project === c.pid" class="dot" title="지금 활동 중"></span>
        </router-link>
        <div v-if="!channels.length" class="empty" style="padding:14px"><span class="spin"></span></div>
        <div v-else-if="!shown.length" class="muted" style="padding:8px 10px;font-size:12px">검색 결과 없음</div>
      </div>

      <!-- 내 프로필 (하단 고정) -->
      <button class="sb-me" @click="showSignIn = true">
        <span class="me-av" :style="{ background: meBg }">{{ (me.name || me.handle || '?').slice(0, 1) }}</span>
        <span class="me-meta">
          <span class="me-n">{{ me.name || '프로필 만들기' }}</span>
          <span v-if="me.handle" class="me-h">@{{ me.handle }}</span>
          <span v-else class="me-h">눌러서 시작하기</span>
        </span>
        <Icon name="sliders" :size="14" class="me-edit" />
      </button>
    </aside>
    <main class="main"><router-view /></main>
    <Dialog />
    <SignIn :open="showSignIn" :force="false" @close="showSignIn = false" />
  </div>
</template>
