<script setup>
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from './api'
import Icon from './components/Icon.vue'
import Dialog from './components/Dialog.vue'
import SignIn from './components/SignIn.vue'
import NewChannel from './components/NewChannel.vue'
import { me, isAuthed, logout } from './user'
import { avatarColor } from './avatar'

const route = useRoute()
const router = useRouter()
const myChannels = ref([])      // 내 워크스페이스(내가 멤버/리드인 채널)
const explore = ref([])         // 둘러보기(공개 채널)
const stats = ref(null)
const drawer = ref(false)
const q = ref('')
const showProfile = ref(false)
const showNew = ref(false)
const creatingChan = ref(false)
let timer = null

function newChannel() { showNew.value = true }
async function onCreateChannel(payload) {
  creatingChan.value = true
  try {
    const c = await api.createChannel(payload)
    showNew.value = false
    drawer.value = false
    await load()
    router.push(`/channels/${c.pid}`)
  } finally { creatingChan.value = false }
}

async function load() {
  if (!isAuthed()) return
  try {
    const [mine, p, s] = await Promise.all([
      api.workspace().catch(() => []),
      api.projects({ ordering: '-event_count' }),
      api.stats(),
    ])
    myChannels.value = mine
    const minePids = new Set(mine.map((c) => c.pid))
    explore.value = p.filter((c) => (c.event_count > 0 || c.message_count > 0) && !minePids.has(c.pid))
    stats.value = s
  } catch (e) { /* keep last */ }
}
onMounted(() => { load(); timer = setInterval(load, 8000) })
onUnmounted(() => clearInterval(timer))
watch(() => route.fullPath, () => { drawer.value = false })
watch(() => me.handle, (v) => { if (v) load() })   // 로그인 직후 즉시 로드

const activePid = computed(() => route.params.pid)
const shownExplore = computed(() => {
  const t = q.value.trim().toLowerCase()
  if (!t) return explore.value
  return explore.value.filter((c) => (c.name || c.pid).toLowerCase().includes(t) || c.pid.toLowerCase().includes(t))
})
const isLive = computed(() => stats.value?.baton?.role)
const meBg = computed(() => me.color || avatarColor(me.handle || 'guest'))
const activeChan = (pid) => stats.value && stats.value.baton && stats.value.baton.project === pid
async function doLogout() { await logout(); router.replace('/login') }
</script>

<template>
  <!-- 미인증: 로그인/회원가입 전체 화면 -->
  <router-view v-if="!isAuthed()" />

  <!-- 인증: 앱 셸 -->
  <div v-else class="shell">
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

        <!-- 내 워크스페이스 -->
        <div class="sb-sec between">
          <span><Icon name="folder" :size="13" /> 내 워크스페이스</span>
          <button class="sb-add" title="새 채널" aria-label="새 채널" @click="newChannel"><Icon name="plus" :size="16" /></button>
        </div>
        <router-link v-for="c in myChannels" :key="c.pid" :to="`/channels/${c.pid}`"
                     class="sb-item" :class="{ active: activePid === c.pid, archived: c.status === 'archived' }">
          <Icon class="ic" name="hash" :size="15" />
          <span class="nm">{{ c.name || c.pid }}</span>
          <span v-if="c.role === 'lead'" class="role-pill">리드</span>
          <span v-else-if="activeChan(c.pid)" class="dot" title="지금 활동 중"></span>
        </router-link>
        <div v-if="!myChannels.length" class="sb-hint">
          <Icon name="plus" :size="13" /> 채널을 만들거나 친구에게 초대받으면 여기에 모여요.
        </div>

        <!-- 둘러보기(쇼케이스) -->
        <div class="sb-sec between">
          <span><Icon name="compass" :size="13" /> 둘러보기 · {{ explore.length }}</span>
        </div>
        <input v-if="explore.length > 6" v-model="q" class="field sb-search" placeholder="채널 검색" />
        <router-link v-for="c in shownExplore" :key="c.pid" :to="`/channels/${c.pid}`"
                     class="sb-item" :class="{ active: activePid === c.pid, archived: c.status === 'archived' }">
          <Icon class="ic" name="hash" :size="15" />
          <span class="nm">{{ c.name || c.pid }}</span>
          <Icon v-if="c.status === 'archived'" class="arch-tag" name="archive" :size="14" />
          <span v-else-if="activeChan(c.pid)" class="dot" title="지금 활동 중"></span>
        </router-link>
        <div v-if="!explore.length && !myChannels.length" class="empty" style="padding:14px"><span class="spin"></span></div>
      </div>

      <!-- 내 프로필 + 로그아웃 (하단 고정) -->
      <div class="sb-me-row">
        <button class="sb-me" @click="showProfile = true">
          <span class="me-av" :style="{ background: meBg }">{{ (me.name || me.handle || '?').slice(0, 1) }}</span>
          <span class="me-meta">
            <span class="me-n">{{ me.name || me.handle }}<span v-if="me.is_guest" class="me-guest">체험</span></span>
            <span class="me-h">@{{ me.handle }}</span>
          </span>
          <Icon name="sliders" :size="14" class="me-edit" />
        </button>
        <button class="sb-out" @click="doLogout" title="로그아웃" aria-label="로그아웃"><Icon name="logout" :size="17" /></button>
      </div>
    </aside>
    <main class="main"><router-view /></main>
    <Dialog />
    <SignIn :open="showProfile" @close="showProfile = false" />
    <NewChannel :open="showNew" :busy="creatingChan" @create="onCreateChannel" @close="showNew = false" />
  </div>
</template>
