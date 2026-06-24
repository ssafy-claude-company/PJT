<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'

const agents = ref([])
const loading = ref(true)
const sort = ref('-event_count')

async function load() {
  loading.value = true
  agents.value = await api.agents({ ordering: sort.value })
  loading.value = false
}
function setSort(s) { sort.value = s; load() }
const active = (s) => (sort.value === s ? 'border-color:var(--accent);color:var(--accent)' : '')
onMounted(load)
</script>

<template>
  <div class="container">
    <div class="page-title">AI 직원 — 직군·활동·성장</div>
    <div class="page-sub">경험→수면 증류로 성장하는 AI 직원들. 카드를 눌러 직무기준·최근 활동을 확인하세요.</div>

    <div class="flex" style="margin-bottom:14px">
      <span class="muted">정렬</span>
      <button class="btn ghost" :style="active('-event_count')" @click="setSort('-event_count')">활동 많은 순</button>
      <button class="btn ghost" :style="active('role')" @click="setSort('role')">직군 순</button>
    </div>

    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <div v-else class="grid cards">
      <router-link v-for="a in agents" :key="a.bot_id" class="card link" :to="`/agents/${a.bot_id}`">
        <div class="between">
          <span class="nm" style="font-size:15px">{{ a.role || '예비' }}</span>
          <span v-if="a.is_leader" class="badge lead">리더</span>
        </div>
        <div class="muted mono" style="font-size:12px;margin:6px 0">#{{ a.bot_id }}</div>
        <div class="flex" style="gap:14px">
          <span class="badge">활동 {{ a.event_count }}</span>
          <span v-if="a.distill_count" class="grow">↑증류 {{ a.distill_count }}</span>
        </div>
      </router-link>
    </div>
  </div>
</template>
