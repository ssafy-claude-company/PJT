<script setup>
import { ref, onMounted, computed } from 'vue'
import api from '../api'
import { timeFmt } from '../kinds'

const props = defineProps({ pid: { type: String, required: true }, baton: { type: Object, default: null } })
const d = ref(null)
const loading = ref(true)

function avatarColor(role) { let h = 0; for (const c of (role || '?')) h = (h * 31 + c.charCodeAt(0)) % 360; return `hsl(${h} 52% 56%)` }
const initials = (r) => (r || '?').replace(/[^가-힣A-Za-z]/g, '').slice(0, 2) || '?'

// 위임 엣지를 'from' 기준으로 묶어 트리처럼
const tree = computed(() => {
  const by = {}
  for (const e of (d.value?.delegations || [])) (by[e.from] ||= []).push(e)
  // 리더 먼저, 그 다음 위임 많은 순
  const lead = d.value?.leader_role
  return Object.entries(by).sort((a, b) => (b[0] === lead) - (a[0] === lead) || b[1].length - a[1].length)
})
const batonHere = computed(() => props.baton && props.baton.project === props.pid && props.baton.role)

onMounted(async () => { try { d.value = await api.collab(props.pid) } finally { loading.value = false } })
</script>

<template>
  <div class="panel collab" style="margin:10px 18px 0">
    <h2>🔭 협업 구조</h2>
    <div v-if="loading" class="empty" style="padding:18px"><span class="spin"></span></div>
    <div v-else-if="d" style="padding:12px 14px;display:grid;gap:14px">
      <!-- 카운트 + 베턴 -->
      <div class="wrap-tags">
        <span v-if="batonHere" class="badge ok">🟢 지금 {{ batonHere }} 작업 중</span>
        <span class="badge">위임 {{ d.counts.delegations }}</span>
        <span class="badge">교차검증 {{ d.counts.cross_checks }}</span>
        <span class="badge">개입 {{ d.counts.interventions }}</span>
        <span class="badge">배포 {{ d.counts.deploys }}</span>
        <span class="badge">Task {{ d.counts.tasks }}</span>
      </div>

      <!-- 역할 보드 -->
      <div>
        <div class="sec">역할 — 누가 무엇을</div>
        <div class="roles">
          <div v-for="r in d.roles" :key="r.role" class="rcard" :class="{ lead: r.is_leader }">
            <span class="av" :style="{ background: avatarColor(r.role) }">{{ initials(r.role) }}</span>
            <div class="rmeta">
              <div class="rn">{{ r.role }} <span v-if="r.is_leader" class="lead-tag">리더</span></div>
              <div class="rs">위임↗{{ r.out }} · 받음↘{{ r.recv }} · 작업 {{ r.work }}<span v-if="r.verify"> · 검증 {{ r.verify }}</span></div>
            </div>
          </div>
        </div>
      </div>

      <!-- 위임 흐름(트리) -->
      <div v-if="tree.length">
        <div class="sec">위임 흐름 — 단일 흐름의 갈래</div>
        <div class="tree">
          <div v-for="[from, edges] in tree" :key="from" class="tnode">
            <div class="tfrom"><span class="dot2" :style="{ background: avatarColor(from) }"></span>{{ from }}</div>
            <div class="tedges">
              <div v-for="e in edges" :key="e.to" class="tedge">
                <span class="arrow">→</span><b>{{ e.to }}</b><span class="x">×{{ e.count }}</span>
                <span class="last" v-if="e.last">{{ e.last.replace(/^[^:]*:\s*/, '').slice(0, 40) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 검증·배포 게이트(Task) -->
      <div v-if="d.tasks.length">
        <div class="sec">검증·배포 게이트</div>
        <div v-for="t in d.tasks" :key="t.task_id" class="gate">
          <div class="gh">
            <span class="mono muted">{{ t.task_id }}</span>
            <span v-if="t.owner_role" class="badge">{{ t.owner_role }}</span>
            <span class="grow"></span>
            <span class="badge" :class="{ ok: t.cross_checks > 0 }">⚖ 교차검증 {{ t.cross_checks }}</span>
            <span class="badge" :class="{ ok: t.deploy_count > 0 }">🚀 배포 {{ t.deploy_count }}</span>
          </div>
          <div v-if="t.purpose || t.goal" class="gbody">{{ (t.purpose || t.goal).slice(0, 160) }}</div>
        </div>
      </div>

      <!-- 개입 -->
      <div v-if="d.interventions.length">
        <div class="sec">사람 개입 {{ d.interventions.length }}</div>
        <div class="iv" v-for="(iv, i) in d.interventions.slice(-6)" :key="i">
          <span class="t">{{ timeFmt(iv.ts) }}</span><span class="who">{{ iv.role || '사람' }}</span>{{ (iv.summary || '').slice(0, 70) }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.collab .sec { font-size: 12px; color: var(--muted); margin-bottom: 7px; font-weight: 600 }
.roles { display: flex; flex-wrap: wrap; gap: 8px }
.rcard { display: flex; align-items: center; gap: 8px; background: var(--bg2, #161b22); border: 1px solid var(--line, #21262d); border-radius: 9px; padding: 7px 10px; min-width: 168px }
.rcard.lead { border-color: var(--accent) }
.rcard .av { width: 28px; height: 28px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; color: #fff; font-weight: 700 }
.rn { font-size: 13px; font-weight: 600 }
.lead-tag { font-size: 10px; color: var(--accent); border: 1px solid var(--accent); border-radius: 5px; padding: 0 4px; margin-left: 3px }
.rs { font-size: 11px; color: var(--muted) }
.tree { display: grid; gap: 8px }
.tnode { border-left: 2px solid var(--line, #21262d); padding-left: 10px }
.tfrom { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 6px; margin-bottom: 3px }
.dot2 { width: 9px; height: 9px; border-radius: 50% }
.tedges { display: grid; gap: 2px; padding-left: 4px }
.tedge { font-size: 12px; color: var(--fg, #c9d1d9); display: flex; align-items: center; gap: 5px }
.tedge .arrow { color: var(--muted) }
.tedge .x { color: var(--accent); font-size: 11px }
.tedge .last { color: var(--muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.gate { background: var(--bg2, #161b22); border: 1px solid var(--line, #21262d); border-radius: 9px; padding: 9px 11px; margin-bottom: 7px }
.gh { display: flex; align-items: center; gap: 7px }
.gbody { font-size: 12px; color: var(--muted); margin-top: 6px; line-height: 1.5 }
.iv { font-size: 12px; color: var(--fg, #c9d1d9); padding: 3px 0; border-top: 1px solid var(--line, #21262d) }
.iv .t { color: var(--muted); font-size: 11px; margin-right: 7px }
.iv .who { color: #a5d6ff; margin-right: 6px }
.grow { flex: 1 }
</style>
