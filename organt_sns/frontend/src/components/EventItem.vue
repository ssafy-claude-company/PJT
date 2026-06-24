<script setup>
import { kindMeta, timeFmt } from '../kinds'

defineProps({
  ev: { type: Object, required: true },
  showProject: { type: Boolean, default: true },
})
</script>

<template>
  <div class="feed-item">
    <span class="k" :style="{ background: kindMeta(ev.kind).bg, color: kindMeta(ev.kind).c }">
      {{ kindMeta(ev.kind).label }}
    </span>
    <span class="s">
      {{ ev.summary }}
      <router-link
        v-if="showProject && ev.project_pid"
        class="p-tag"
        :to="`/projects/${ev.project_pid}`"
      >{{ ev.project_pid }}<template v-if="ev.project_name">·{{ ev.project_name }}</template></router-link>
    </span>
    <span class="t">{{ timeFmt(ev.ts) }}</span>
  </div>
</template>
