<script setup>
import { ref, onMounted, computed } from 'vue'
import api from '../api'

const agents = ref([])
const loading = ref(true)
const sort = ref('-event_count')
const q = ref('')

function avatarColor(role) { let h = 0; for (const c of (role || '?')) h = (h * 31 + c.charCodeAt(0)) % 360; return `hsl(${h} 52% 56%)` }

async function load() {
  loading.value = true
  agents.value = await api.agents({ ordering: sort.value })
  loading.value = false
}
function setSort(s) { sort.value = s; load() }
const active = (s) => (sort.value === s ? 'border-color:var(--accent);color:var(--accent)' : '')
const shown = computed(() => {
  const t = q.value.trim().toLowerCase()
  if (!t) return agents.value
  return agents.value.filter((a) => (a.role || '').toLowerCase().includes(t) || (a.name || '').toLowerCase().includes(t))
})
onMounted(load)
</script>

<template>
  <div class="container">
    <div class="page-title">AI 직원 — 직군·활동·성장</div>
    <div class="page-sub">경험→수면 증류로 성장하는 AI 직원들. 카드를 눌러 직무기준·최근 활동을 확인하고, 인격·아바타를 편집하세요.</div>

    <div class="flex" style="margin-bottom:14px;gap:8px;flex-wrap:wrap">
      <input v-model="q" placeholder="직군·이름 검색…" style="max-width:240px" />
      <span class="muted">정렬</span>
      <button class="btn ghost" :style="active('-event_count')" @click="setSort('-event_count')">활동 많은 순</button>
      <button class="btn ghost" :style="active('role')" @click="setSort('role')">직군 순</button>
      <span class="muted" style="font-size:12px">· {{ shown.length }}명</span>
    </div>

    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <div v-else-if="!shown.length" class="empty">검색 결과가 없습니다</div>
    <div v-else class="grid cards">
      <router-link v-for="a in shown" :key="a.bot_id" class="card link" :to="`/agents/${a.bot_id}`">
        <div class="between">
          <div class="flex" style="gap:9px;min-width:0">
            <span class="av-sm" :style="{ background: avatarColor(a.role) }">{{ a.avatar || (a.role || '?').slice(0, 1) }}</span>
            <span class="nm" style="font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ a.role || '예비' }}</span>
          </div>
          <span v-if="a.is_leader" class="badge lead">리더</span>
        </div>
        <div v-if="a.name" class="muted" style="font-size:12px;margin-top:6px">{{ a.name }}</div>
        <div v-if="a.persona" class="muted" style="font-size:12px;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">“{{ a.persona }}”</div>
        <div class="flex" style="gap:14px;margin-top:8px">
          <span class="badge">활동 {{ a.event_count }}</span>
          <span v-if="a.distill_count" class="grow">↑증류 {{ a.distill_count }}</span>
          <span v-if="a.created_via === 'sns'" class="badge ok">스튜디오</span>
        </div>
      </router-link>
    </div>
  </div>
</template>

<style scoped>
.av-sm { width: 30px; height: 30px; border-radius: 50%; flex: none; display: inline-flex; align-items: center;
  justify-content: center; font-size: 14px; color: #fff; font-weight: 700 }
</style>
