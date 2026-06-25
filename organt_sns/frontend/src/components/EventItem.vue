<script setup>
import { kindMeta, whenFmt, dateFmt } from '../kinds'

defineProps({
  ev: { type: Object, required: true },
  showProject: { type: Boolean, default: true },
})

// 로그 접두("이름 → id: 본문")를 떼어 본문만. 화살표 없으면 그대로.
function clean(s) {
  s = (s || '').trim()
  const arrow = s.search(/→/)
  if (arrow >= 0) {
    const colon = s.indexOf(':', arrow)
    if (colon >= 0) { const b = s.slice(colon + 1).trim(); if (b) return b }
  }
  return s
}
</script>

<template>
  <div class="feed-item">
    <span class="k" :style="{ background: kindMeta(ev.kind).bg, color: kindMeta(ev.kind).c }">{{ kindMeta(ev.kind).label }}</span>
    <span class="s">
      {{ clean(ev.summary) }}
      <router-link v-if="showProject && ev.project_pid" class="p-tag" :to="`/channels/${ev.project_pid}`">
        {{ ev.project_pid }}<template v-if="ev.project_name"> · {{ ev.project_name }}</template>
      </router-link>
    </span>
    <span class="t" :title="dateFmt(ev.ts)">{{ whenFmt(ev.ts) }}</span>
  </div>
</template>

<style scoped>
.feed-item { display: flex; align-items: baseline; gap: 10px; padding: 9px 16px; border-bottom: 1px solid var(--line2); font-size: 13px }
.feed-item:last-child { border-bottom: 0 }
.k { font-size: 10.5px; font-weight: 600; padding: 1px 9px; border-radius: 20px; flex: none; white-space: nowrap }
.s { flex: 1; min-width: 0; color: var(--text); line-height: 1.5; word-break: break-word }
.p-tag { color: var(--text3); font-size: 12px; margin-left: 4px; white-space: nowrap }
.p-tag:hover { color: var(--accent2) }
.t { color: var(--text3); font-size: 11px; flex: none; font-variant-numeric: tabular-nums }
</style>
