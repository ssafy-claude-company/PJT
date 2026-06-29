<script setup>
// 프로젝트 산출물·작업 보드 — 배포(라이브)·저장소(repo) 링크를 1급으로 보여주고, Task 단위로 관리.
// (사용자 요청: ① 프로젝트 결과물(배포/repo 링크)을 따로 보는 창 ② 디스코드처럼 task별 관리)
import { ref, onMounted, computed } from 'vue'
import api from '../api'
import { monogram, avatarColor } from '../avatar'
import { toast } from '../toast'
import Icon from './Icon.vue'

const props = defineProps({ pid: { type: String, required: true } })
const d = ref(null)
const loading = ref(true)
const err = ref(false)
const tab = ref('out')                       // out=산출물, task=작업(Task)

// 링크 종류별 표현 — 배포는 '라이브'로 강조, 저장소는 'repo', 그 외 '링크'
const META = {
  deploy: { icon: 'globe', label: '라이브', cls: 'deploy' },
  repo: { icon: 'folder', label: '저장소', cls: 'repo' },
  link: { icon: 'link', label: '링크', cls: 'link' },
}
const mt = (t) => META[t] || META.link        // 알 수 없는 타입이면 'link'로(패널 크래시 방지)
const deliv = computed(() => d.value?.deliverables || [])
const tasks = computed(() => d.value?.tasks || [])
const clip = (s, n) => (s && s.length > n ? s.slice(0, n) + '…' : (s || ''))
const short = (u) => (u || '').replace(/^https?:\/\//, '').replace(/\/$/, '')

async function copy(url) {
  try { await navigator.clipboard.writeText(url); toast('링크를 복사했어요') }
  catch { toast('복사하지 못했어요') }
}

onMounted(async () => {
  try { d.value = await api.article(props.pid) }
  catch { err.value = true }
  finally { loading.value = false }
})
</script>

<template>
  <div class="panel article" style="margin:12px 20px 0">
    <h2>산출물 · 작업</h2>
    <div v-if="loading" class="empty" style="padding:22px"><span class="spin"></span></div>
    <div v-else-if="err" class="empty2" style="padding:22px">산출물을 불러오지 못했어요. 잠시 후 다시 열어보세요.</div>
    <div v-else-if="d" style="padding:14px 16px;display:grid;gap:14px">
      <!-- 목표 · 상태 -->
      <div class="head">
        <span class="status" :class="{ done: d.status === '완료' }">{{ d.status }}</span>
        <span v-if="d.leader_role" class="hmeta">리더 · {{ d.leader_role }}</span>
        <span class="grow"></span>
        <span v-if="d.stats.live_links" class="hmeta">라이브 {{ d.stats.live_links }}</span>
        <span v-if="d.stats.deploys" class="hmeta">배포 {{ d.stats.deploys }}회</span>
        <span class="hmeta">작업 {{ d.stats.tasks }}</span>
      </div>
      <div v-if="d.goal" class="goal"><span class="goal-k">목표</span>{{ d.goal }}</div>

      <!-- 탭 -->
      <div class="tabs">
        <button class="tab" :class="{ on: tab === 'out' }" @click="tab = 'out'">산출물 <span class="tn">{{ deliv.length }}</span></button>
        <button class="tab" :class="{ on: tab === 'task' }" @click="tab = 'task'">작업 <span class="tn">{{ tasks.length }}</span></button>
      </div>

      <!-- 산출물 — 배포/저장소/링크 카드 -->
      <div v-if="tab === 'out'">
        <div v-if="!deliv.length" class="empty2">아직 공개된 산출물이 없어요. 봇이 배포·저장소 링크를 올리면 여기에 모여요.</div>
        <div v-else class="dlist">
          <a v-for="x in deliv" :key="x.url" :href="x.url" target="_blank" rel="noopener"
             class="dcard" :class="mt(x.type).cls">
            <span class="dico"><Icon :name="mt(x.type).icon" :size="16" /></span>
            <div class="dmain">
              <div class="dtop"><span class="dbadge" :class="mt(x.type).cls">{{ mt(x.type).label }}</span>
                <span class="dlabel">{{ x.label }}</span></div>
              <div class="durl">{{ short(x.url) }}</div>
            </div>
            <button class="dcopy" title="링크 복사" @click.prevent.stop="copy(x.url)"><Icon name="box" :size="14" /></button>
            <Icon name="arrowR" :size="15" class="dgo" />
          </a>
        </div>
      </div>

      <!-- 작업(Task) — 단위별 담당·상태·산출 -->
      <div v-else>
        <div v-if="!tasks.length" class="empty2">아직 등록된 작업(Task)이 없어요. 프로젝트가 진행되면 작업 단위가 쌓여요.</div>
        <div v-else class="tlist">
          <div v-for="t in tasks" :key="t.task_id" class="tcard">
            <div class="th">
              <span class="bot-av" style="width:28px;height:28px;font-size:11px"
                    :style="{ background: avatarColor(t.owner_role || t.title) }">{{ monogram(t.owner_name, t.owner_role) }}</span>
              <div class="tmeta">
                <div class="ttitle">{{ t.title }}</div>
                <div class="tsub">{{ t.owner_name || t.owner_role || '미배정' }}<span v-if="t.status" class="tst"> · {{ t.status }}</span></div>
              </div>
              <span class="tbadges">
                <span class="badge" :class="{ ok: t.cross_checks > 0 }">점검 {{ t.cross_checks }}</span>
                <span class="badge" :class="{ ok: t.deploy_count > 0 }">배포 {{ t.deploy_count }}</span>
              </span>
            </div>
            <div v-if="t.goal && t.goal !== t.title" class="tgoal">{{ clip(t.goal, 180) }}</div>
            <div v-if="t.deliverables && t.deliverables.length" class="tlinks">
              <a v-for="l in t.deliverables" :key="l.url" :href="l.url" target="_blank" rel="noopener"
                 class="tlink" :class="mt(l.type).cls"><Icon :name="mt(l.type).icon" :size="12" />{{ l.label }}</a>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.head { display: flex; align-items: center; gap: 9px; flex-wrap: wrap }
.status { font-size: 12px; font-weight: 700; color: var(--accent2); background: var(--surface2); border: 1px solid var(--line); border-radius: 20px; padding: 3px 11px }
.status.done { color: var(--ok); border-color: rgba(82, 183, 136, .35) }
.hmeta { font-size: 11.5px; color: var(--text3) }
.grow { flex: 1 }
.goal { font-size: 13px; color: var(--text); line-height: 1.6; background: var(--surface2); border: 1px solid var(--line); border-radius: var(--r); padding: 10px 13px }
.goal-k { font-size: 11px; color: var(--text3); font-weight: 700; margin-right: 8px }
.tabs { display: flex; gap: 6px; border-bottom: 1px solid var(--line); padding-bottom: 2px }
.tab { background: none; border: 0; color: var(--text3); font-size: 13px; font-weight: 600; padding: 7px 12px; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -3px }
.tab.on { color: var(--text); border-bottom-color: var(--accent) }
.tab .tn { color: var(--text3); font-size: 11px; margin-left: 3px }
.empty2 { font-size: 12.5px; color: var(--text3); padding: 16px 4px; line-height: 1.6 }

.dlist { display: grid; gap: 9px }
.dcard { display: flex; align-items: center; gap: 12px; background: var(--surface2); border: 1px solid var(--line); border-radius: var(--r); padding: 11px 13px; text-decoration: none; color: var(--text); transition: border-color .12s, background .12s }
.dcard:hover { border-color: var(--accent-line); background: var(--surface) }
.dcard.deploy { border-left: 3px solid var(--ok) }
.dcard.repo { border-left: 3px solid var(--accent2) }
.dcard.link { border-left: 3px solid var(--line) }
.dico { width: 30px; height: 30px; flex: none; display: grid; place-items: center; border-radius: 8px; background: var(--surface); color: var(--text2) }
.dcard.deploy .dico { color: var(--ok) }
.dcard.repo .dico { color: var(--accent2) }
.dmain { flex: 1; min-width: 0 }
.dtop { display: flex; align-items: center; gap: 7px }
.dbadge { font-size: 10.5px; font-weight: 700; border-radius: 5px; padding: 1px 6px; background: var(--surface); color: var(--text2) }
.dbadge.deploy { color: var(--ok); background: var(--ok-soft) }
.dbadge.repo { color: var(--accent2) }
.dlabel { font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.durl { font-size: 11.5px; color: var(--text3); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.dcopy { flex: none; background: none; border: 0; color: var(--text3); cursor: pointer; padding: 4px; border-radius: 6px }
.dcopy:hover { color: var(--text); background: var(--surface) }
.dgo { color: var(--text3); flex: none }

.tlist { display: grid; gap: 9px }
.tcard { background: var(--surface2); border: 1px solid var(--line); border-radius: var(--r); padding: 11px 13px }
.th { display: flex; align-items: center; gap: 10px }
.tmeta { flex: 1; min-width: 0 }
.ttitle { font-size: 13px; font-weight: 600; line-height: 1.4; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }
.tsub { font-size: 11.5px; color: var(--text2); margin-top: 1px }
.tst { color: var(--text3) }
.tbadges { display: flex; gap: 5px; flex: none }
.tgoal { font-size: 12.5px; color: var(--text2); margin-top: 8px; line-height: 1.55 }
.tlinks { margin-top: 9px; display: flex; flex-wrap: wrap; gap: 7px }
.tlink { font-size: 11.5px; border: 1px solid var(--line); border-radius: 20px; padding: 3px 10px; text-decoration: none; display: inline-flex; align-items: center; gap: 5px; color: var(--text2) }
.tlink.deploy { color: var(--ok); border-color: rgba(82, 183, 136, .3) }
.tlink.repo { color: var(--accent2) }
.tlink:hover { background: var(--surface) }
.badge.ok { color: var(--ok) }
</style>
