<script setup>
import { ref, watch } from 'vue'
import Icon from './Icon.vue'

const props = defineProps({ open: Boolean, initialName: { type: String, default: '' }, busy: Boolean })
const emit = defineEmits(['create', 'close'])
const name = ref(''); const visibility = ref('private')

watch(() => props.open, (o) => { if (o) { name.value = props.initialName || ''; visibility.value = 'private' } })
function submit() {
  const n = name.value.trim()
  if (!n || props.busy) return
  emit('create', { name: n, visibility: visibility.value })
}
</script>

<template>
  <div v-if="open" class="scrim" @click.self="emit('close')">
    <div class="box">
      <div class="title">새 채널 만들기</div>
      <div class="sub">채널은 프로젝트의 협업 공간이에요. 비공개면 나와 초대한 멤버만 볼 수 있어요.</div>
      <div class="form">
        <input v-model="name" placeholder="채널 이름 (예: 포트폴리오 사이트)" @keyup.enter="submit" />
        <div class="vis">
          <button type="button" :class="{ on: visibility === 'private' }" @click="visibility = 'private'">
            <Icon name="lock" :size="17" />
            <span class="vb"><b>비공개</b><span>나와 초대한 멤버만</span></span>
          </button>
          <button type="button" :class="{ on: visibility === 'public' }" @click="visibility = 'public'">
            <Icon name="globe" :size="17" />
            <span class="vb"><b>공개</b><span>둘러보기에 노출 · 누구나 열람</span></span>
          </button>
        </div>
        <div class="flex" style="gap:8px;justify-content:flex-end">
          <button class="btn ghost" @click="emit('close')">취소</button>
          <button class="btn" @click="submit" :disabled="busy || !name.trim()">{{ busy ? '…' : '만들기' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.scrim { position: fixed; inset: 0; z-index: 110; background: rgba(0,0,0,.6); backdrop-filter: blur(3px);
  display: flex; align-items: center; justify-content: center; padding: 20px }
.box { width: 100%; max-width: 440px; background: var(--elevated); border: 1px solid var(--line); border-radius: 16px;
  box-shadow: var(--shadow-lg); padding: 24px;
  background-image: radial-gradient(110% 90% at 0% 0%, rgba(123,120,240,.12), transparent 55%) }
.title { font-size: 18px; font-weight: 750; letter-spacing: -.02em }
.sub { color: var(--text2); font-size: 13px; margin-top: 7px; line-height: 1.55 }
.form { display: grid; gap: 13px; margin-top: 18px }
.form input { background: var(--surface); border: 1px solid var(--line); color: var(--text); border-radius: 9px;
  padding: 11px 13px; font: inherit; font-size: 14px; width: 100% }
.form input:focus { outline: none; border-color: var(--accent-line) }
.vis { display: grid; grid-template-columns: 1fr 1fr; gap: 9px }
.vis button { display: flex; align-items: center; gap: 10px; text-align: left; background: var(--surface);
  border: 1px solid var(--line); border-radius: 11px; padding: 12px; color: var(--text2); cursor: pointer; font: inherit; transition: .12s }
.vis button:hover { border-color: var(--accent-line) }
.vis button.on { border-color: var(--accent); background: var(--accent-soft); color: var(--text) }
.vis .icon { flex: none; color: var(--accent2) }
.vis .vb { display: flex; flex-direction: column; min-width: 0; line-height: 1.3 }
.vis .vb b { font-size: 13px; font-weight: 650 }
.vis .vb span { font-size: 10.5px; color: var(--text3) }
@media(max-width:520px){ .vis { grid-template-columns: 1fr } }
</style>
