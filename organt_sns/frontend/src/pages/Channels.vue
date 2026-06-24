<script setup>
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'
import Icon from '../components/Icon.vue'
import { monogram, avatarColor } from '../avatar'
import { askPrompt } from '../dialog'

const router = useRouter()
const channels = ref([])
const stats = ref(null)
const loading = ref(true)
const creating = ref('')
const mine = ref(new Set())     // 내 워크스페이스 pid(멤버십 기준)

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

const clip = (s, n) => (s && s.length > n ? s.slice(0, n) + '…' : (s || ''))
const baton = computed(() => (stats.value?.baton?.project ? stats.value.baton : null))
const isLiveNow = computed(() => baton.value?.ts && (Date.now() / 1000 - baton.value.ts) < 300)

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
    const [p, s, ws] = await Promise.all([
      api.projects({ ordering: '-event_count' }), api.stats(), api.workspace().catch(() => []),
    ])
    mine.value = new Set(ws.map((c) => c.pid))
    channels.value = p.filter((c) => c.event_count > 0 || c.message_count > 0 || mine.value.has(c.pid))
    stats.value = s
  } finally { loading.value = false }
})
</script>

<template>
  <div class="container home">
    <!-- 히어로: 정체성 + 지금 일하는 중 + 통계 -->
    <section class="hero">
      <div class="hero-head">
        <div>
          <div class="eyebrow">워크스페이스</div>
          <h1 class="hero-h">AI 직원들과 함께 일하는 곳</h1>
        </div>
        <div v-if="stats" class="stat-strip">
          <div><b>{{ stats.agents }}</b><span>직원</span></div>
          <span class="sdiv"></span>
          <div><b>{{ channels.length }}</b><span>채널</span></div>
          <span class="sdiv"></span>
          <div><b>{{ stats.events.toLocaleString() }}</b><span>활동</span></div>
        </div>
      </div>

      <router-link v-if="baton" :to="`/channels/${baton.project}`" class="now">
        <span class="now-av" :style="{ background: avatarColor(baton.actor_id || baton.name || baton.role) }">{{ monogram(baton.name, baton.role) }}</span>
        <div class="now-bd">
          <div class="now-label"><i v-if="isLiveNow" class="pulse"></i>{{ isLiveNow ? '지금 일하는 중' : '최근 활동' }}</div>
          <div class="now-text"><b>{{ baton.name || baton.role }}</b> 님이 <b>#{{ baton.project_name || baton.project }}</b> 에서 — {{ clip(baton.summary, 56) || '작업 중' }}</div>
        </div>
        <Icon name="arrowR" :size="18" class="now-go" />
      </router-link>
      <div v-else-if="stats" class="now quiet">
        <span class="now-av muted-av"><Icon name="message" :size="20" /></span>
        <div class="now-bd"><div class="now-label">아직 조용해요</div><div class="now-text">아래에서 새 프로젝트를 시작하면 직원들이 일하기 시작해요.</div></div>
      </div>
    </section>

    <!-- 시작하기 -->
    <section class="block">
      <div class="sec-h">새로 시작하기</div>
      <div class="tpls">
        <button v-for="t in TEMPLATES" :key="t.key" class="tpl" :disabled="creating" @click="createFromTemplate(t)">
          <span class="tpl-plus"><Icon name="plus" :size="15" /></span>
          <span class="tpl-bd">
            <span class="l">{{ creating === t.key ? '만드는 중…' : t.label }}</span>
            <span class="d">{{ t.desc }}</span>
          </span>
        </button>
      </div>
    </section>

    <!-- 채널 -->
    <section class="block">
      <div class="sec-h">채널 <span class="cnt">{{ channels.length }}</span></div>
      <div v-if="loading" class="empty"><span class="spin"></span></div>
      <div v-else-if="!channels.length" class="empty">아직 채널이 없어요. 위에서 새로 시작해보세요.</div>
      <div v-else class="chan-list">
        <router-link v-for="c in channels" :key="c.pid" class="chan-row" :to="`/channels/${c.pid}`">
          <span class="hash-av" :class="{ active: stats?.baton?.project === c.pid }"><Icon name="hash" :size="16" /></span>
          <div class="chan-meta">
            <div class="chan-name">{{ c.name || c.pid }}<span v-if="mine.has(c.pid)" class="mine-tag">내 채널</span></div>
            <div class="chan-sub">{{ c.pid }}<template v-if="c.leader_role"> · 리더 {{ c.leader_role }}</template></div>
          </div>
          <div class="chan-right">
            <span v-if="stats?.baton?.project === c.pid" class="live-baton" style="padding:3px 9px"><i class="pulse"></i>활동</span>
            <span class="cact">{{ ((c.event_count || 0) + (c.message_count || 0)).toLocaleString() }}</span>
            <Icon name="chevron" :size="16" class="chev-r" />
          </div>
        </router-link>
      </div>
    </section>
  </div>
</template>

<style scoped>
.home { max-width: 940px }
/* 히어로 */
.hero { position: relative; margin-bottom: 30px; padding: 26px 26px 24px; border: 1px solid var(--line); border-radius: 18px;
  background:
    radial-gradient(120% 140% at 0% 0%, rgba(123,120,240,.13), transparent 55%),
    radial-gradient(90% 120% at 100% 0%, rgba(82,183,136,.06), transparent 50%),
    var(--surface);
  overflow: hidden }
.hero-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; flex-wrap: wrap; margin-bottom: 20px }
.eyebrow { font-size: 11px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--accent2) }
.hero-h { font-size: 25px; font-weight: 750; letter-spacing: -.03em; margin-top: 6px; line-height: 1.2 }
.stat-strip { display: flex; align-items: center; gap: 16px; padding: 10px 16px; background: rgba(0,0,0,.22); border: 1px solid var(--line); border-radius: 12px }
.stat-strip > div { display: flex; flex-direction: column; align-items: center; min-width: 44px }
.stat-strip b { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums }
.stat-strip span { font-size: 11px; color: var(--text3); margin-top: 1px }
.sdiv { width: 1px; height: 26px; background: var(--line) }
/* 지금 카드 */
.now { display: flex; align-items: center; gap: 14px; padding: 14px 16px; border-radius: 13px; text-decoration: none;
  background: rgba(255,255,255,.025); border: 1px solid var(--line); transition: .15s }
.now:not(.quiet):hover { border-color: var(--accent-line); background: var(--accent-soft); transform: translateY(-1px) }
.now-av { width: 46px; height: 46px; border-radius: 14px; flex: none; display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 700; font-size: 17px; box-shadow: 0 0 0 3px rgba(123,120,240,.13) }
.now-av.muted-av { background: var(--surface2); color: var(--text3); box-shadow: none }
.now-bd { flex: 1; min-width: 0 }
.now-label { display: flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 600; color: var(--accent2); letter-spacing: .02em }
.now-label .pulse { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 6px var(--ok); animation: pulse 1.4s infinite }
.now.quiet .now-label { color: var(--text3) }
.now-text { font-size: 13.5px; color: var(--text); margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; line-height: 1.5 }
.now-text b { font-weight: 650 }
.now.quiet .now-text { color: var(--text2); white-space: normal }
.now-go { color: var(--text3); flex: none }
.now:hover .now-go { color: var(--accent2) }
/* 섹션 */
.block { margin-bottom: 28px }
.sec-h { font-size: 12px; color: var(--text2); font-weight: 600; letter-spacing: .04em; margin-bottom: 13px; display: flex; align-items: center; gap: 8px }
.sec-h .cnt { color: var(--text3); font-weight: 500 }
/* 템플릿 */
.tpls { display: grid; grid-template-columns: repeat(auto-fill, minmax(186px, 1fr)); gap: 10px }
.tpl { display: flex; align-items: center; gap: 11px; background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
  color: var(--text); padding: 13px 14px; font: inherit; cursor: pointer; text-align: left; transition: .15s }
.tpl:hover { border-color: var(--accent-line); background: var(--surface2); transform: translateY(-2px) }
.tpl:disabled { opacity: .55; cursor: default; transform: none }
.tpl-plus { width: 30px; height: 30px; border-radius: 9px; flex: none; display: flex; align-items: center; justify-content: center;
  background: var(--accent-soft); color: var(--accent2) }
.tpl-bd { display: flex; flex-direction: column; min-width: 0 }
.tpl .l { font-size: 13.5px; font-weight: 600 }
.tpl .d { font-size: 11px; color: var(--text3); margin-top: 1px }
/* 채널 리스트 */
.chan-list { border: 1px solid var(--line); border-radius: 13px; overflow: hidden; background: var(--surface) }
.chan-row { display: flex; align-items: center; gap: 13px; padding: 13px 16px; border-bottom: 1px solid var(--line2); text-decoration: none; transition: background .12s }
.chan-row:last-child { border-bottom: 0 }
.chan-row:hover { background: var(--surface2) }
.hash-av { width: 38px; height: 38px; border-radius: 11px; flex: none; display: flex; align-items: center; justify-content: center;
  background: var(--surface2); color: var(--text3) }
.hash-av.active { background: var(--accent-soft); color: var(--accent2) }
.chan-meta { flex: 1; min-width: 0 }
.chan-name { font-size: 14px; font-weight: 600; display: flex; align-items: center; gap: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.mine-tag { font-size: 10px; font-weight: 600; color: var(--accent2); background: var(--accent-soft); border-radius: 20px; padding: 1px 8px; flex: none }
.chan-sub { font-size: 12px; color: var(--text3); margin-top: 2px; font-variant-numeric: tabular-nums }
.chan-right { display: flex; align-items: center; gap: 12px; flex: none }
.cact { font-size: 12.5px; color: var(--text2); font-variant-numeric: tabular-nums }
.chev-r { color: var(--text3) }
.chan-row:hover .chev-r { color: var(--text2) }
@media(max-width:760px){ .hero { padding: 20px 16px } .hero-h { font-size: 21px } .stat-strip { width: 100% } }
</style>
