<script setup>
import { ref, watch } from 'vue'
import { me, saveProfile } from '../user'
import { AVATAR_COLORS } from '../avatar'

const props = defineProps({ open: Boolean })
const emit = defineEmits(['close'])
const name = ref(''); const color = ref(AVATAR_COLORS[0]); const bio = ref(''); const saving = ref(false); const err = ref('')

watch(() => props.open, (o) => {
  if (o) { name.value = me.name || ''; color.value = me.color || AVATAR_COLORS[0]; bio.value = me.bio || ''; err.value = '' }
})
async function submit() {
  saving.value = true; err.value = ''
  try { await saveProfile({ name: name.value.trim() || me.handle, color: color.value, bio: bio.value }); emit('close') }
  catch (e) { err.value = e?.response?.data?.detail || '저장에 실패했어요.' }
  finally { saving.value = false }
}
</script>

<template>
  <div v-if="open" class="scrim" @click.self="emit('close')">
    <div class="box">
      <div class="title">내 프로필</div>
      <div class="sub">@{{ me.handle }}<span v-if="me.is_guest" class="gtag">체험 계정</span></div>
      <div class="form">
        <div class="flex" style="gap:13px;align-items:center">
          <span class="big" :style="{ background: color }">{{ (name || me.handle || '?').slice(0, 1) }}</span>
          <div class="sws">
            <button v-for="c in AVATAR_COLORS" :key="c" class="sw" :class="{ on: color === c }" :style="{ background: c }" @click="color = c"></button>
          </div>
        </div>
        <div><label>이름</label><input v-model="name" placeholder="표시 이름 (예: 도진)" @keyup.enter="submit" /></div>
        <div><label>소개</label><input v-model="bio" placeholder="한 줄 소개 (선택)" maxlength="160" @keyup.enter="submit" /></div>
        <div v-if="err" class="err">{{ err }}</div>
        <div class="flex" style="gap:8px;justify-content:flex-end">
          <button class="btn ghost" @click="emit('close')">취소</button>
          <button class="btn" @click="submit" :disabled="saving">{{ saving ? '…' : '저장' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.scrim { position: fixed; inset: 0; z-index: 110; background: rgba(0,0,0,.6); backdrop-filter: blur(3px);
  display: flex; align-items: center; justify-content: center; padding: 20px }
.box { width: 100%; max-width: 420px; background: var(--elevated); border: 1px solid var(--line); border-radius: 16px;
  box-shadow: var(--shadow-lg); padding: 24px;
  background-image: radial-gradient(110% 90% at 0% 0%, rgba(123,120,240,.12), transparent 55%) }
.title { font-size: 18px; font-weight: 750; letter-spacing: -.02em }
.sub { color: var(--text2); font-size: 13px; margin-top: 5px; display: flex; align-items: center; gap: 8px }
.gtag { font-size: 10.5px; font-weight: 700; color: var(--warn); background: rgba(217,164,74,.13); border-radius: 5px; padding: 2px 7px }
.form { display: grid; gap: 13px; margin-top: 18px }
.form label { display: block; font-size: 11px; color: var(--text3); font-weight: 600; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 6px }
.big { width: 52px; height: 52px; border-radius: 16px; flex: none; display: flex; align-items: center; justify-content: center; color: #fff; font-weight: 700; font-size: 22px }
.sws { display: flex; flex-wrap: wrap; gap: 6px; flex: 1 }
.sw { width: 28px; height: 28px; border-radius: 50%; border: 2px solid transparent; outline: 1px solid var(--line); cursor: pointer; transition: .1s }
.sw:hover { transform: scale(1.1) } .sw.on { border-color: var(--text); outline-color: var(--text) }
.err { color: var(--danger); font-size: 12.5px }
</style>
