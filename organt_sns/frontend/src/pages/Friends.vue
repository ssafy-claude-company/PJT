<script setup>
import { ref, onMounted, computed } from 'vue'
import api from '../api'
import { me, isGuest } from '../user'
import { avatarColor } from '../avatar'

const friends = ref([])
const q = ref('')
const found = ref([])
const loading = ref(true)
const searching = ref(false)

const pbg = (p) => p.color || avatarColor(p.handle || p.name)
const ini = (p) => (p.name || p.handle || '?').slice(0, 1)
const friendSet = computed(() => new Set(friends.value.map((f) => f.handle)))

async function load() { try { friends.value = await api.friends() } catch (e) { friends.value = [] } finally { loading.value = false } }
async function search() {
  if (!q.value.trim()) { found.value = []; return }
  searching.value = true
  try { found.value = await api.people(q.value.trim()) } finally { searching.value = false }
}
async function add(handle) { friends.value = await api.addFriend(handle) }
async function remove(handle) { await api.removeFriend(handle); load() }
onMounted(load)
</script>

<template>
  <div class="container" style="max-width:760px">
    <div class="page-title">친구</div>
    <div class="page-sub">친구를 맺으면 서로의 채널에 초대해 함께 프로젝트를 만들 수 있어요. 각자 데려온 AI 직원들이 한 팀처럼 협업합니다.</div>

    <div v-if="isGuest()" class="empty">먼저 좌측 상단에서 프로필을 만들어 주세요.</div>
    <template v-else>
      <div class="panel" style="margin-bottom:20px">
        <h2>친구 추가</h2>
        <div style="padding:14px">
          <input v-model="q" placeholder="이름이나 @핸들로 검색" @input="search" />
          <div v-if="found.length" class="results">
            <div v-for="p in found" :key="p.handle" class="prow">
              <span class="pav" :style="{ background: pbg(p) }">{{ ini(p) }}</span>
              <div class="pmeta"><div class="pn">{{ p.name }}</div><div class="ph">@{{ p.handle }}</div></div>
              <button v-if="friendSet.has(p.handle)" class="btn ghost sm" disabled>친구</button>
              <button v-else class="btn sm" @click="add(p.handle)">추가</button>
            </div>
          </div>
          <div v-else-if="q && !searching" class="muted" style="font-size:12.5px;margin-top:10px">검색 결과가 없어요. 상대가 프로필을 만들어야 추가할 수 있어요.</div>
        </div>
      </div>

      <div class="sec-h">내 친구 {{ friends.length }}명</div>
      <div v-if="loading" class="empty"><span class="spin"></span></div>
      <div v-else-if="!friends.length" class="empty">아직 친구가 없어요. 위에서 핸들로 찾아 추가해보세요.</div>
      <div v-else class="grid cards">
        <div v-for="p in friends" :key="p.handle" class="card">
          <div class="flex" style="gap:11px;min-width:0">
            <span class="pav" :style="{ background: pbg(p) }">{{ ini(p) }}</span>
            <div style="min-width:0;flex:1">
              <div class="nm">{{ p.name }}</div>
              <div class="muted" style="font-size:12px">@{{ p.handle }}</div>
            </div>
            <button class="btn ghost sm" @click="remove(p.handle)">삭제</button>
          </div>
          <div v-if="p.bio" class="muted" style="font-size:12px;margin-top:8px">{{ p.bio }}</div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.sec-h { font-size: 11px; color: var(--text3); font-weight: 600; letter-spacing: .05em; text-transform: uppercase; margin-bottom: 12px }
.results { margin-top: 12px; display: grid; gap: 4px }
.prow { display: flex; align-items: center; gap: 11px; padding: 7px 9px; border-radius: 9px }
.prow:hover { background: var(--surface2) }
.pav { width: 36px; height: 36px; border-radius: 11px; flex: none; display: flex; align-items: center; justify-content: center; color: #fff; font-weight: 650; font-size: 14px }
.pmeta { flex: 1; min-width: 0 } .pn { font-size: 13.5px; font-weight: 600 } .ph { font-size: 12px; color: var(--text3) }
</style>
