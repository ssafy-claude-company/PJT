<script setup>
import { ref, onMounted, computed } from 'vue'
import api from '../api'
import { monogram, avatarBg } from '../avatar'

const agents = ref([])
const loading = ref(true)
const sort = ref('-event_count')
const q = ref('')

async function load() {
  loading.value = true
  agents.value = await api.agents({ ordering: sort.value })
  loading.value = false
}
function setSort(s) { sort.value = s; load() }
const active = (s) => (sort.value === s ? 'on' : '')
const shown = computed(() => {
  const t = q.value.trim().toLowerCase()
  if (!t) return agents.value
  return agents.value.filter((a) => (a.role || '').toLowerCase().includes(t) || (a.name || '').toLowerCase().includes(t))
})
onMounted(load)
</script>

<template>
  <div class="container">
    <div class="page-title">우리 직원</div>
    <div class="page-sub">일하면서 경험을 쌓고 쉬는 동안 정리하며 성장하는 직원들이에요. 저마다 이름이 있고 역할은 맡은 일입니다. 카드를 눌러 쌓은 노하우와 활동을 보고, 성격·역할을 바꿀 수 있어요.</div>

    <div class="flex" style="margin-bottom:18px;gap:8px;flex-wrap:wrap">
      <input v-model="q" placeholder="이름·역할 검색" style="max-width:240px" />
      <span class="muted" style="font-size:12px">정렬</span>
      <button class="btn ghost sm" :class="active('-event_count')" @click="setSort('-event_count')">활동순</button>
      <button class="btn ghost sm" :class="active('role')" @click="setSort('role')">역할순</button>
      <span class="muted" style="font-size:12px">· {{ shown.length }}명</span>
    </div>

    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <div v-else-if="!shown.length" class="empty">검색 결과가 없습니다</div>
    <div v-else class="grid cards">
      <router-link v-for="a in shown" :key="a.bot_id" class="card link" :to="`/agents/${a.bot_id}`">
        <div class="flex" style="gap:11px;min-width:0">
          <span class="bot-av" style="width:40px;height:40px;font-size:16px;border-radius:12px" :style="{ background: avatarBg(a) }">{{ monogram(a.name, a.role) }}</span>
          <div style="min-width:0;flex:1">
            <div class="nm" style="font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ a.name || '이름 없음' }}</div>
            <div class="flex" style="gap:6px;margin-top:2px">
              <span class="muted" style="font-size:12.5px">{{ a.role || '대기 중' }}</span>
              <span v-if="a.is_leader" class="badge lead">리더</span>
            </div>
          </div>
        </div>
        <div v-if="a.persona" class="persona-sm">{{ a.persona }}</div>
        <div class="flex" style="gap:8px;margin-top:11px">
          <span class="badge">활동 {{ a.event_count }}</span>
          <span v-if="a.distill_count" class="grow">성장 {{ a.distill_count }}</span>
          <span v-if="a.created_via === 'sns'" class="badge accent">내가 만든</span>
        </div>
      </router-link>
    </div>
  </div>
</template>

<style scoped>
.persona-sm { font-size: 12px; color: var(--text2); margin-top: 10px; line-height: 1.5; padding-left: 10px; border-left: 2px solid var(--line);
  overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical }
</style>
