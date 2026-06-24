<script setup>
import { ref } from 'vue'
import api from '../api'
import { monogram, avatarColor } from '../avatar'

const q = ref('')
const result = ref(null)
const loading = ref(false)
const error = ref(false)
const examples = [
  '실시간 멀티플레이 협동 웹게임 서버 동기화',
  'Vue 반응형 화면 UI 렌더링',
  '테스트 품질 검증 버그 엣지케이스',
  '게임 레벨 밸런스 기획',
]
// 추천 근거 4축(가중치 합 1.0) — 디자인 토큰 색 + Channel과 동일 라벨
const SEG = {
  role_match: { c: '#8f8cf5', l: '역할 적합' },
  keyword_overlap: { c: '#52b788', l: '키워드 일치' },
  expertise: { c: '#b88cf0', l: '전문성' },
  track_record: { c: '#d9a44a', l: '실적' },
}

async function run() {
  if (!q.value.trim()) return
  loading.value = true; error.value = false
  try { result.value = await api.recommend(q.value.trim(), 6) }
  catch (e) { error.value = true }
  finally { loading.value = false }
}
function ex(e) { q.value = e; run() }
</script>

<template>
  <div class="container">
    <div class="page-title">직원 찾기</div>
    <div class="page-sub">
      하려는 일을 적으면 <b>가장 잘 맞는 직원</b>을 찾아드려요. 점수는 역할 적합·키워드 일치·전문성·실적을
      종합한 것이고, <b>왜 추천했는지 근거</b>도 함께 보여줍니다.
    </div>

    <div class="flex" style="margin-bottom:10px">
      <input v-model="q" placeholder="예: 실시간 협동 웹게임 서버 동기화" @keyup.enter="run" />
      <button class="btn" @click="run" :disabled="loading">{{ loading ? '추천 중…' : '추천' }}</button>
    </div>
    <div class="wrap-tags" style="margin-bottom:18px">
      <button v-for="e in examples" :key="e" class="ex-chip" @click="ex(e)">{{ e }}</button>
    </div>
    <div v-if="error" class="pending-bar">추천을 불러오지 못했습니다. 잠시 후 다시 시도하세요.</div>

    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <template v-else-if="result">
      <div class="muted" style="margin-bottom:10px">
        “{{ result.query || '전반적인 역량' }}”에 맞는 직원 {{ result.results.length }}명
      </div>
      <div class="grid" style="gap:12px">
        <div v-for="(r, i) in result.results" :key="r.bot_id" class="card">
          <div class="between">
            <div class="flex" style="min-width:0">
              <span class="mono" style="font-size:15px;font-weight:700;color:var(--text3);width:20px">{{ i + 1 }}</span>
              <span class="bot-av sm" :style="{ background: avatarColor(r.name || r.role) }">{{ monogram(r.name, r.role) }}</span>
              <router-link :to="`/agents/${r.bot_id}`" class="nm" style="font-size:14.5px">{{ r.name || r.role }}</router-link>
              <span class="muted" style="font-size:12.5px">{{ r.role }}</span>
              <span v-if="r.is_leader" class="badge lead">리더</span>
            </div>
            <div class="flex">
              <span v-if="r.distill_count" class="grow">성장 {{ r.distill_count }}</span>
              <span class="badge">활동 {{ r.event_count }}</span>
              <span class="mono" style="font-weight:700;color:var(--accent2)">{{ (r.score * 100).toFixed(0) }}<span class="muted" style="font-weight:400;font-size:11px">점</span></span>
            </div>
          </div>
          <div class="scorebar" style="margin-top:10px">
            <i v-for="(seg, k) in SEG" :key="k" :style="{ width: ((r.reasons[k] || 0) * 100) + '%', background: seg.c }"></i>
          </div>
          <div class="legend">
            <span v-for="(seg, k) in SEG" :key="k">
              <i :style="{ background: seg.c }"></i>{{ seg.l }} {{ ((r.reasons[k] || 0) * 100).toFixed(0) }}
            </span>
          </div>
        </div>
      </div>
    </template>
    <div v-else class="empty">검색어를 입력하거나 예시를 눌러보세요</div>
  </div>
</template>

<style scoped>
.ex-chip { background: var(--surface2); border: 1px solid var(--line); border-radius: 20px; color: var(--text2);
  padding: 5px 13px; font: inherit; font-size: 12.5px; cursor: pointer; transition: .12s }
.ex-chip:hover { color: var(--text); border-color: var(--accent-line) }
</style>
