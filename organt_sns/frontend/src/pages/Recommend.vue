<script setup>
import { ref } from 'vue'
import api from '../api'

const q = ref('')
const result = ref(null)
const loading = ref(false)
const examples = [
  '실시간 멀티플레이 협동 웹게임 서버 동기화',
  'Vue 반응형 화면 UI 렌더링',
  '테스트 품질 검증 버그 엣지케이스',
  '게임 레벨 밸런스 기획',
]
// 추천 근거 4축(가중치 합 1.0) — 색·라벨
const SEG = {
  role_match: { c: '#58a6ff', l: '역할적합' },
  keyword_overlap: { c: '#3fb950', l: '직무기준중복' },
  expertise: { c: '#bc8cff', l: '증류역량' },
  track_record: { c: '#d29922', l: '활동실적' },
}

async function run() {
  if (!q.value.trim()) return
  loading.value = true
  try { result.value = await api.recommend(q.value.trim(), 6) }
  finally { loading.value = false }
}
function ex(e) { q.value = e; run() }
</script>

<template>
  <div class="container">
    <div class="page-title">적임자 추천</div>
    <div class="page-sub">
      요구사항을 입력하면 <b>강점 기반</b>으로 가장 적합한 AI 직원을 추천합니다. 점수는
      역할적합·직무기준 키워드중복·증류역량·활동실적의 가중합이며, 각 추천의 <b>근거를 항별로</b> 보여줍니다.
    </div>

    <div class="flex" style="margin-bottom:10px">
      <input v-model="q" placeholder="예: 실시간 협동 웹게임 서버 동기화" @keyup.enter="run" />
      <button class="btn" @click="run" :disabled="loading">{{ loading ? '추천 중…' : '추천' }}</button>
    </div>
    <div class="wrap-tags" style="margin-bottom:18px">
      <button v-for="e in examples" :key="e" class="btn ghost"
              style="font-size:12px;padding:5px 10px" @click="ex(e)">{{ e }}</button>
    </div>

    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <template v-else-if="result">
      <div class="muted" style="margin-bottom:10px">
        “{{ result.query || '(전반 역량)' }}” 적임자 {{ result.results.length }}명
      </div>
      <div class="grid" style="gap:12px">
        <div v-for="(r, i) in result.results" :key="r.bot_id" class="card">
          <div class="between">
            <div class="flex">
              <span class="mono" style="font-size:18px;font-weight:800;color:var(--mut);width:22px">{{ i + 1 }}</span>
              <router-link :to="`/agents/${r.bot_id}`" class="nm" style="font-size:15px">{{ r.role }}</router-link>
              <span v-if="r.is_leader" class="badge lead">리더</span>
            </div>
            <div class="flex">
              <span v-if="r.distill_count" class="grow">↑증류 {{ r.distill_count }}</span>
              <span class="badge">활동 {{ r.event_count }}</span>
              <span class="mono" style="font-weight:800;color:var(--accent)">{{ (r.score * 100).toFixed(0) }}</span>
            </div>
          </div>
          <div class="scorebar" style="margin-top:10px">
            <i v-for="(seg, k) in SEG" :key="k" :style="{ width: (r.reasons[k] * 100) + '%', background: seg.c }"></i>
          </div>
          <div class="legend">
            <span v-for="(seg, k) in SEG" :key="k">
              <i :style="{ background: seg.c }"></i>{{ seg.l }} {{ (r.reasons[k] * 100).toFixed(0) }}
            </span>
          </div>
        </div>
      </div>
    </template>
    <div v-else class="empty">검색어를 입력하거나 예시를 눌러보세요</div>
  </div>
</template>
