<script setup>
import { ref, onMounted, computed } from 'vue'
import api from '../api'
import { timeFmt } from '../kinds'
import { avatarColor, monogram } from '../avatar'
import Icon from './Icon.vue'

const props = defineProps({ pid: { type: String, required: true }, baton: { type: Object, default: null } })
const d = ref(null)
const loading = ref(true)

const initials = (r) => monogram(null, r)
const clip = (s, n) => (s && s.length > n ? s.slice(0, n) + '…' : (s || ''))

const tree = computed(() => {
  const by = {}
  for (const e of (d.value?.delegations || [])) (by[e.from] ||= []).push(e)
  const lead = d.value?.leader_role
  return Object.entries(by).sort((a, b) => (b[0] === lead) - (a[0] === lead) || b[1].length - a[1].length)
})
const batonHere = computed(() => props.baton && props.baton.project === props.pid && props.baton.role)

onMounted(async () => { try { d.value = await api.collab(props.pid) } finally { loading.value = false } })
</script>

<template>
  <div class="panel collab" style="margin:12px 20px 0">
    <h2>협업 구조</h2>
    <div v-if="loading" class="empty" style="padding:22px"><span class="spin"></span></div>
    <div v-else-if="d" style="padding:14px 16px;display:grid;gap:18px">
      <div class="wrap-tags">
        <span v-if="batonHere" class="live-baton"><i class="pulse"></i>{{ batonHere }} 작업 중</span>
        <span class="badge">위임 {{ d.counts.delegations }}</span>
        <span class="badge">교차검증 {{ d.counts.cross_checks }}</span>
        <span class="badge">개입 {{ d.counts.interventions }}</span>
        <span class="badge">배포 {{ d.counts.deploys }}</span>
        <span class="badge">작업 {{ d.counts.tasks }}</span>
      </div>

      <div>
        <div class="sec">역할</div>
        <div class="roles">
          <div v-for="r in d.roles" :key="r.role" class="rcard" :class="{ lead: r.is_leader }">
            <span class="bot-av" style="width:30px;height:30px;font-size:12px" :style="{ background: avatarColor(r.role) }">{{ initials(r.role) }}</span>
            <div>
              <div class="rn">{{ r.role }} <span v-if="r.is_leader" class="badge lead" style="margin-left:2px">리더</span></div>
              <div class="rs">위임 {{ r.out }} · 받음 {{ r.recv }} · 작업 {{ r.work }}<span v-if="r.verify"> · 검증 {{ r.verify }}</span></div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="tree.length">
        <div class="sec">위임 흐름</div>
        <div class="tree">
          <div v-for="[from, edges] in tree" :key="from" class="tnode">
            <div class="tfrom"><span class="dot2" :style="{ background: avatarColor(from) }"></span>{{ from }}</div>
            <div class="tedges">
              <div v-for="e in edges" :key="e.to" class="tedge">
                <Icon name="arrowR" :size="13" class="ar" /><b>{{ e.to }}</b><span class="x">×{{ e.count }}</span>
                <span class="last" v-if="e.last">{{ clip(e.last.replace(/^[^:]*:\s*/, ''), 40) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="d.tasks.length">
        <div class="sec">검증 · 배포 게이트</div>
        <div v-for="t in d.tasks" :key="t.task_id" class="gate">
          <div class="gh">
            <span class="mono muted" style="font-size:12px">{{ t.task_id }}</span>
            <span v-if="t.owner_role" class="badge">{{ t.owner_role }}</span>
            <span style="flex:1"></span>
            <span class="badge" :class="{ ok: t.cross_checks > 0 }">교차검증 {{ t.cross_checks }}</span>
            <span class="badge" :class="{ ok: t.deploy_count > 0 }">배포 {{ t.deploy_count }}</span>
          </div>
          <div v-if="t.purpose || t.goal" class="gbody">{{ clip(t.purpose || t.goal, 160) }}</div>
        </div>
      </div>

      <div v-if="d.outputs && d.outputs.length">
        <div class="sec">산출물</div>
        <div v-for="(o, i) in d.outputs.slice().reverse()" :key="i" class="out">
          <div class="oh"><span class="badge">{{ o.role || '직원' }}</span><span class="t">{{ timeFmt(o.ts) }}</span></div>
          <div class="obody">{{ clip(o.result, 220) }}</div>
          <div v-if="o.links && o.links.length" class="olinks">
            <a v-for="l in o.links" :key="l" :href="l" target="_blank" rel="noopener" class="olink"><Icon name="link" :size="13" />{{ l.replace(/^https?:\/\//, '').slice(0, 40) }}</a>
          </div>
        </div>
      </div>

      <div v-if="d.interventions.length">
        <div class="sec">사람 개입 · {{ d.interventions.length }}</div>
        <div class="iv" v-for="(iv, i) in d.interventions.slice(-6)" :key="i">
          <span class="t">{{ timeFmt(iv.ts) }}</span><span class="who">{{ iv.role || '사람' }}</span>{{ clip(iv.summary, 70) }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sec { font-size: 11px; color: var(--text3); margin-bottom: 9px; font-weight: 600; letter-spacing: .05em; text-transform: uppercase }
.roles { display: flex; flex-wrap: wrap; gap: 8px }
.rcard { display: flex; align-items: center; gap: 10px; background: var(--surface2); border: 1px solid var(--line); border-radius: var(--r); padding: 9px 12px; min-width: 176px }
.rcard.lead { border-color: var(--accent-line) }
.rn { font-size: 13px; font-weight: 600 }
.rs { font-size: 11.5px; color: var(--text2); margin-top: 1px }
.tree { display: grid; gap: 10px }
.tnode { border-left: 2px solid var(--line); padding-left: 12px }
.tfrom { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 7px; margin-bottom: 4px }
.dot2 { width: 8px; height: 8px; border-radius: 50% }
.tedges { display: grid; gap: 3px }
.tedge { font-size: 12.5px; color: var(--text); display: flex; align-items: center; gap: 6px }
.tedge .ar { color: var(--text3) }
.tedge .x { color: var(--accent2); font-size: 11.5px }
.tedge .last { color: var(--text3); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.gate { background: var(--surface2); border: 1px solid var(--line); border-radius: var(--r); padding: 11px 13px; margin-bottom: 8px }
.gh { display: flex; align-items: center; gap: 8px }
.gbody { font-size: 12.5px; color: var(--text2); margin-top: 7px; line-height: 1.55 }
.out { background: var(--surface2); border: 1px solid var(--line); border-radius: var(--r); padding: 11px 13px; margin-bottom: 8px }
.oh { display: flex; align-items: center; gap: 8px; margin-bottom: 6px }
.oh .t { color: var(--text3); font-size: 11px }
.obody { font-size: 12.5px; color: var(--text); line-height: 1.6; white-space: pre-wrap }
.olinks { margin-top: 9px; display: flex; flex-wrap: wrap; gap: 7px }
.olink { font-size: 12px; color: var(--ok); border: 1px solid rgba(82,183,136,.3); border-radius: 20px; padding: 3px 11px; text-decoration: none; display: inline-flex; align-items: center; gap: 6px }
.olink:hover { background: var(--ok-soft) }
.iv { font-size: 12.5px; color: var(--text); padding: 5px 0; border-top: 1px solid var(--line) }
.iv:first-child { border-top: 0 }
.iv .t { color: var(--text3); font-size: 11px; margin-right: 8px }
.iv .who { color: var(--accent2); margin-right: 7px }
</style>
