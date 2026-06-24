<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'
import Icon from '../components/Icon.vue'
import { askPrompt } from '../dialog'

const router = useRouter()
const channels = ref([])
const stats = ref(null)
const loading = ref(true)
const creating = ref('')

// 프로젝트 템플릿 — 클릭 한 번에 채널 생성 + 초기 목표(Work 요청) 시딩 → 러너가 픽업
const TEMPLATES = [
  { key: 'web', label: '웹 앱', desc: '풀스택 · 인증 · 배포', name: '새 웹 앱',
    goal: '사용자 인증·핵심 CRUD·반응형 UI를 갖춘 풀스택 웹 앱을 설계·구현하고 배포까지 끝내라. 측정가능한 완료기준을 먼저 합의할 것.' },
  { key: 'game', label: '웹 게임', desc: '플레이 가능 · 배포', name: '새 웹 게임',
    goal: '플레이 가능한 웹 게임(점수·상태 관리·간단한 VFX)을 만들고 배포하라. 재미와 동작을 직접 플레이로 검증할 것.' },
  { key: 'api', label: 'API 서버', desc: 'REST · 스키마 · 문서', name: '새 API 서버',
    goal: 'REST API + DB 스키마 + 간단한 문서를 갖춘 백엔드 서버를 구현하고 배포하라. 엔드포인트별 동작을 검증할 것.' },
  { key: 'data', label: '데이터 분석', desc: '수집 · 분석 · 리포트', name: '새 데이터 분석',
    goal: '데이터 수집·정제·분석·시각화 리포트를 만들어라. 결론을 근거(수치)로 뒷받침할 것.' },
  { key: 'blank', label: '빈 프로젝트', desc: '직접 정의', name: '새 프로젝트', goal: '' },
]

async function createFromTemplate(t) {
  if (creating.value) return
  creating.value = t.key
  try {
    const name = await askPrompt({ title: '새 프로젝트', placeholder: '채널 이름', value: t.name })
    if (!name) return
    const c = await api.createChannel({ name })
    if (t.goal) { try { await api.makeRequest(c.pid, { kind: 'W', body: t.goal }) } catch (e) { /* 채널은 생성됨 */ } }
    router.push(`/channels/${c.pid}`)
  } finally { creating.value = '' }
}

onMounted(async () => {
  try {
    const [p, s] = await Promise.all([api.projects({ ordering: '-event_count' }), api.stats()])
    channels.value = p.filter((c) => c.event_count > 0 || c.message_count > 0 || !/^P-/.test(c.pid))
    stats.value = s
  } finally { loading.value = false }
})
</script>

<template>
  <div class="container">
    <div class="page-title">협업 채널</div>
    <div class="page-sub">
      AI 직원들이 단일 흐름으로 협업하는 프로젝트 공간. 채널에 들어가면 직원들의 협업 대화를 시간순으로 따라보고,
      구조 보기로 위임 트리·검증 게이트·산출물을 확인할 수 있습니다.
    </div>
    <div v-if="stats" class="wrap-tags" style="margin-bottom:24px">
      <span class="badge">이벤트 {{ stats.events.toLocaleString() }}</span>
      <span class="badge">AI 직원 {{ stats.agents }}</span>
      <span class="badge">채널 {{ channels.length }}</span>
      <span v-if="stats.baton && stats.baton.role" class="badge ok">베턴 · {{ stats.baton.role }}</span>
    </div>

    <div class="sec-h">새 프로젝트 시작</div>
    <div class="tpls">
      <button v-for="t in TEMPLATES" :key="t.key" class="tpl" :disabled="creating" @click="createFromTemplate(t)">
        <span class="l">{{ creating === t.key ? '만드는 중…' : t.label }}</span>
        <span class="d">{{ t.desc }}</span>
      </button>
    </div>

    <div class="sec-h" style="margin-top:24px">채널</div>
    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <div v-else class="grid cards">
      <router-link v-for="c in channels" :key="c.pid" class="card link" :to="`/channels/${c.pid}`">
        <div class="between">
          <span class="flex" style="gap:8px;min-width:0">
            <Icon name="hash" :size="15" class="muted" />
            <span class="nm" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ c.name || c.pid }}</span>
          </span>
          <span v-if="stats && stats.baton && stats.baton.project === c.pid" class="live-baton" style="padding:3px 9px"><i class="pulse"></i>활동</span>
        </div>
        <div class="muted mono" style="font-size:12px;margin:8px 0">{{ c.pid }} · 리더 {{ c.leader_role || '—' }}</div>
        <div class="flex" style="gap:8px">
          <span class="badge">{{ ((c.event_count || 0) + (c.message_count || 0)).toLocaleString() }} 활동</span>
          <span v-if="c.task_count" class="badge">Task {{ c.task_count }}</span>
          <span v-if="!/^P-/.test(c.pid)" class="badge accent">새 채널</span>
        </div>
      </router-link>
    </div>
  </div>
</template>

<style scoped>
.sec-h { font-size: 11px; color: var(--text3); font-weight: 600; letter-spacing: .05em; text-transform: uppercase; margin: 0 0 12px }
.tpls { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px }
.tpl { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-lg); color: var(--text);
  padding: 14px 16px; font: inherit; cursor: pointer; text-align: left; display: flex; flex-direction: column; gap: 4px; transition: .15s }
.tpl:hover { border-color: var(--accent-line); background: var(--surface2); transform: translateY(-2px) }
.tpl:disabled { opacity: .55; cursor: default; transform: none }
.tpl .l { font-size: 14px; font-weight: 600 }
.tpl .d { font-size: 11.5px; color: var(--text3) }
</style>
