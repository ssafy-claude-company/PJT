<script setup>
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'
import { monogram, avatarBg } from '../avatar'
import { me } from '../user'

const router = useRouter()
const agents = ref([])
const loading = ref(true)
const sort = ref('-event_count')
const q = ref('')

async function load() {
  loading.value = true
  try { agents.value = await api.agents({ ordering: sort.value }) }
  finally { loading.value = false }   // 실패해도 스피너가 영원히 안 돌게
}
function setSort(s) { sort.value = s; load() }
const active = (s) => (sort.value === s ? 'on' : '')
const shown = computed(() => {
  const t = q.value.trim().toLowerCase()
  if (!t) return agents.value
  return agents.value.filter((a) => (a.role || '').toLowerCase().includes(t) || (a.name || '').toLowerCase().includes(t))
})
const mineList = computed(() => shown.value.filter((a) => a.owner_handle && a.owner_handle === me.handle))
const publicList = computed(() => shown.value.filter((a) => !a.owner_handle))
onMounted(load)
</script>

<template>
  <div class="container">
    <div class="page-title">우리 직원</div>
    <div class="page-sub">일하며 경험을 쌓고 쉬는 동안 정리하며 성장하는 AI 직원들이에요. <b>내 직원</b>은 내가 채용해 성격·역할을 바꿀 수 있고, <b>공개 직원</b>은 모두가 함께 쓰는 쇼케이스 팀이에요.</div>

    <div class="flex" style="margin-bottom:18px;gap:8px;flex-wrap:wrap">
      <input v-model="q" placeholder="이름·역할 검색" style="max-width:240px" />
      <span class="muted" style="font-size:12px">정렬</span>
      <button class="btn ghost sm" :class="active('-event_count')" @click="setSort('-event_count')">활동순</button>
      <button class="btn ghost sm" :class="active('role')" @click="setSort('role')">역할순</button>
    </div>

    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <template v-else>
      <!-- 내 직원 -->
      <div class="sec-h">내 직원 <span class="cnt">{{ mineList.length }}</span></div>
      <div v-if="!mineList.length" class="empty mine-empty">
        아직 내 직원이 없어요. <button class="btn sm" @click="router.push('/studio')">직원 만들기</button>
      </div>
      <div v-else class="grid cards" style="margin-bottom:26px">
        <router-link v-for="a in mineList" :key="a.bot_id" class="card link" :to="`/agents/${a.bot_id}`">
          <div class="flex" style="gap:11px;min-width:0">
            <span class="bot-av" style="width:40px;height:40px;font-size:16px;border-radius:12px" :style="{ background: avatarBg(a) }">{{ monogram(a.name, a.role) }}</span>
            <div style="min-width:0;flex:1">
              <div class="nm" style="font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ a.name || '이름 없음' }}</div>
              <div class="flex" style="gap:6px;margin-top:2px">
                <span class="muted" style="font-size:12.5px">{{ a.role || '대기 중' }}</span>
                <span v-if="a.is_leader" class="badge lead">리더</span>
              </div>
            </div>
            <span class="badge accent">내 직원</span>
          </div>
          <div v-if="a.persona" class="persona-sm">{{ a.persona }}</div>
          <div class="flex" style="gap:8px;margin-top:11px">
            <span class="badge">활동 {{ a.event_count }}</span>
            <span v-if="a.distill_count" class="grow">성장 {{ a.distill_count }}</span>
          </div>
        </router-link>
      </div>

      <!-- 공개 직원(쇼케이스) -->
      <div class="sec-h">공개 직원 <span class="cnt">{{ publicList.length }}</span></div>
      <div v-if="!publicList.length" class="empty">{{ q ? '검색 결과가 없어요' : '공개된 직원이 없어요' }}</div>
      <div v-else class="grid cards">
        <router-link v-for="a in publicList" :key="a.bot_id" class="card link" :to="`/agents/${a.bot_id}`">
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
          </div>
        </router-link>
      </div>
    </template>
  </div>
</template>

<style scoped>
.sec-h { font-size: 12px; color: var(--text2); font-weight: 600; letter-spacing: .04em; margin-bottom: 13px; display: flex; align-items: center; gap: 8px }
.sec-h .cnt { color: var(--text3); font-weight: 500 }
.mine-empty { display: flex; align-items: center; justify-content: center; gap: 12px; margin-bottom: 26px }
.persona-sm { font-size: 12px; color: var(--text2); margin-top: 10px; line-height: 1.5; padding-left: 10px; border-left: 2px solid var(--line);
  overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical }
</style>
