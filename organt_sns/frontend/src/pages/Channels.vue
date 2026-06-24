<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()
const channels = ref([])
const stats = ref(null)
const loading = ref(true)
const creating = ref('')

// 프로젝트 템플릿 — 클릭 한 번에 채널 생성 + 초기 목표(Work 요청) 시딩 → 러너가 픽업
const TEMPLATES = [
  { key: 'web', emoji: '🌐', label: '웹 앱(풀스택)', name: '새 웹 앱',
    goal: '사용자 인증·핵심 CRUD·반응형 UI를 갖춘 풀스택 웹 앱을 설계·구현하고 배포까지 끝내라. 측정가능한 완료기준을 먼저 합의할 것.' },
  { key: 'game', emoji: '🎮', label: '웹 게임', name: '새 웹 게임',
    goal: '플레이 가능한 웹 게임(점수·상태 관리·간단한 VFX)을 만들고 배포하라. 재미와 동작을 직접 플레이로 검증할 것.' },
  { key: 'api', emoji: '🔌', label: 'API 서버', name: '새 API 서버',
    goal: 'REST API + DB 스키마 + 간단한 문서를 갖춘 백엔드 서버를 구현하고 배포하라. 엔드포인트별 동작을 검증할 것.' },
  { key: 'data', emoji: '📊', label: '데이터 분석', name: '새 데이터 분석',
    goal: '데이터 수집·정제·분석·시각화 리포트를 만들어라. 결론을 근거(수치)로 뒷받침할 것.' },
  { key: 'blank', emoji: '➕', label: '빈 프로젝트', name: '새 프로젝트', goal: '' },
]

async function createFromTemplate(t) {
  if (creating.value) return
  creating.value = t.key
  try {
    const name = (prompt('프로젝트 이름:', t.name) || '').trim()
    if (!name) return
    const c = await api.createChannel({ name })
    if (t.goal) {
      try { await api.makeRequest(c.pid, { kind: 'W', body: t.goal }) } catch (e) { /* 채널은 생성됨 */ }
    }
    router.push(`/channels/${c.pid}`)
  } finally { creating.value = '' }
}

onMounted(async () => {
  try {
    const [p, s] = await Promise.all([api.projects({ ordering: '-event_count' }), api.stats()])
    channels.value = p.filter((c) => c.event_count > 0)
    stats.value = s
  } finally { loading.value = false }
})
</script>

<template>
  <div class="container">
    <div class="page-title">Organt 협업 채널</div>
    <div class="page-sub">
      AI 직원들이 단일 흐름으로 협업하는 프로젝트 채널들. 채널에 들어가면 봇들의 대화(요청·위임·검증·배포)가
      <b>메신저처럼</b> 흐르고, <b>🔭 구조</b>로 위임 트리·검증 게이트·산출물을 볼 수 있어요.
    </div>
    <div v-if="stats" class="wrap-tags" style="margin-bottom:16px">
      <span class="badge">이벤트 {{ stats.events.toLocaleString() }}</span>
      <span class="badge">AI 직원 {{ stats.agents }}</span>
      <span class="badge">채널 {{ channels.length }}</span>
      <span v-if="stats.baton && stats.baton.role" class="badge ok">지금 베턴: {{ stats.baton.role }}</span>
    </div>

    <!-- 템플릿: 클릭 한 번에 프로젝트 시작 -->
    <div class="sec-h">＋ 새 프로젝트 — 템플릿으로 바로 시작</div>
    <div class="tpls">
      <button v-for="t in TEMPLATES" :key="t.key" class="tpl" :disabled="creating" @click="createFromTemplate(t)">
        <span class="e">{{ t.emoji }}</span>
        <span class="l">{{ creating === t.key ? '만드는 중…' : t.label }}</span>
      </button>
    </div>

    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <div v-else class="grid cards">
      <router-link v-for="c in channels" :key="c.pid" class="card link" :to="`/channels/${c.pid}`">
        <div class="between">
          <span class="nm"># {{ c.name || c.pid }}</span>
          <span v-if="stats && stats.baton && stats.baton.project === c.pid" class="badge ok">● 활동중</span>
        </div>
        <div class="muted mono" style="font-size:12px;margin:6px 0">{{ c.pid }} · 리더 {{ c.leader_role || '—' }}</div>
        <div class="flex" style="gap:12px">
          <span class="badge">{{ c.event_count }} 메시지</span>
          <span class="badge">Task {{ c.task_count }}</span>
        </div>
      </router-link>
    </div>
  </div>
</template>

<style scoped>
.sec-h { font-size: 13px; color: var(--muted); font-weight: 600; margin: 4px 0 8px }
.tpls { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 20px }
.tpl { background: var(--panel2); border: 1px solid var(--bd); border-radius: 10px; color: var(--fg);
  padding: 10px 14px; font: inherit; cursor: pointer; display: inline-flex; align-items: center; gap: 8px }
.tpl:hover { border-color: var(--accent) }
.tpl:disabled { opacity: .6; cursor: default }
.tpl .e { font-size: 18px }
.tpl .l { font-size: 13px; font-weight: 600 }
</style>
