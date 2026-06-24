<script setup>
import { watch, nextTick, ref } from 'vue'
import { dlg, closeDialog } from '../dialog'

const inputEl = ref(null)
function ok() { closeDialog(dlg.mode === 'prompt' ? (dlg.value.trim() || null) : true) }
function cancel() { closeDialog(dlg.mode === 'prompt' ? null : false) }
watch(() => dlg.open, async (o) => { if (o && dlg.mode === 'prompt') { await nextTick(); inputEl.value?.focus(); inputEl.value?.select() } })
</script>

<template>
  <transition name="dlg">
    <div v-if="dlg.open" class="dlg-scrim" @click.self="cancel">
      <div class="dlg-box" role="dialog" aria-modal="true">
        <div class="dlg-title">{{ dlg.title }}</div>
        <div v-if="dlg.message" class="dlg-msg">{{ dlg.message }}</div>
        <input v-if="dlg.mode === 'prompt'" ref="inputEl" v-model="dlg.value" :placeholder="dlg.placeholder"
               @keyup.enter="ok" @keyup.esc="cancel" style="margin-top:4px" />
        <div class="dlg-actions">
          <button class="btn ghost" @click="cancel">취소</button>
          <button class="btn" :class="{ danger: dlg.danger }" @click="ok">{{ dlg.mode === 'confirm' && dlg.danger ? '삭제' : '확인' }}</button>
        </div>
      </div>
    </div>
  </transition>
</template>

<style scoped>
.dlg-scrim { position: fixed; inset: 0; z-index: 100; background: rgba(0,0,0,.55); backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center; padding: 20px }
.dlg-box { width: 100%; max-width: 380px; background: var(--elevated); border: 1px solid var(--line);
  border-radius: var(--r-lg); box-shadow: var(--shadow-lg); padding: 20px }
.dlg-title { font-size: 15px; font-weight: 650; letter-spacing: -.02em }
.dlg-msg { color: var(--text2); font-size: 13px; margin-top: 8px; line-height: 1.55 }
.dlg-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 18px }
.btn.danger { background: var(--danger); }
.btn.danger:hover { background: #ef7e80 }
.dlg-enter-active, .dlg-leave-active { transition: opacity .15s }
.dlg-enter-from, .dlg-leave-to { opacity: 0 }
.dlg-enter-active .dlg-box { transition: transform .15s }
.dlg-enter-from .dlg-box { transform: translateY(8px) }
</style>
