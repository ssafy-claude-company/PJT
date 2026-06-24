<script setup>
import { ref, watch } from 'vue'
import { me, signIn, signOut } from '../user'
import { AVATAR_COLORS } from '../avatar'

const props = defineProps({ open: Boolean, force: Boolean })
const emit = defineEmits(['close'])
const handle = ref(''); const name = ref(''); const color = ref(AVATAR_COLORS[0]); const saving = ref(false); const err = ref('')

// 계정 전환 — 한 브라우저에서 여러 유저를 시연(친구·초대·공동 리드). 로그아웃 후 새 핸들로 가입.
function switchUser() {
  signOut()
  handle.value = ''; name.value = ''; color.value = AVATAR_COLORS[0]; err.value = ''
}

watch(() => props.open, (o) => {
  if (o) { handle.value = me.handle || ''; name.value = me.name || ''; color.value = me.color || AVATAR_COLORS[0]; err.value = '' }
})
async function submit() {
  err.value = ''
  const h = handle.value.trim().toLowerCase()
  if (!/^[a-z0-9_]{2,30}$/.test(h)) { err.value = '핸들은 영소문자·숫자·_ 2~30자예요.'; return }
  saving.value = true
  try { await signIn({ handle: h, name: name.value.trim() || h, color: color.value }); emit('close') }
  catch (e) { err.value = e?.response?.data?.detail || '저장에 실패했어요.' }
  finally { saving.value = false }
}
</script>

<template>
  <div v-if="open" class="scrim" @click.self="!force && emit('close')">
    <div class="box">
      <div class="title">{{ me.handle ? '내 프로필' : 'Organt에 오신 걸 환영해요' }}</div>
      <div v-if="!me.handle" class="sub">이름과 핸들을 정하면 친구를 맺고, 프로젝트를 함께 만들고, 각자의 AI 직원을 데려와 같이 일할 수 있어요.</div>
      <div class="form">
        <div class="flex" style="gap:13px;align-items:center">
          <span class="big" :style="{ background: color }">{{ (name || handle || '?').slice(0, 1) }}</span>
          <div class="sws">
            <button v-for="c in AVATAR_COLORS" :key="c" class="sw" :class="{ on: color === c }" :style="{ background: c }" @click="color = c"></button>
          </div>
        </div>
        <div><label>이름</label><input v-model="name" placeholder="표시 이름 (예: 도진)" @keyup.enter="submit" /></div>
        <div><label>핸들</label><div class="hwrap"><span>@</span><input v-model="handle" :disabled="!!me.handle" placeholder="dojin — 영문·숫자·_" @keyup.enter="submit" /></div></div>
        <div v-if="err" class="err">{{ err }}</div>
        <div class="flex" style="gap:8px;align-items:center">
          <button v-if="me.handle" type="button" class="linkbtn" @click="switchUser" title="로그아웃하고 다른 핸들로 가입">계정 전환</button>
          <span style="flex:1"></span>
          <button v-if="!force" class="btn ghost" @click="emit('close')">취소</button>
          <button class="btn" @click="submit" :disabled="saving">{{ saving ? '…' : me.handle ? '저장' : '시작하기' }}</button>
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
.sub { color: var(--text2); font-size: 13px; margin-top: 8px; line-height: 1.55 }
.form { display: grid; gap: 13px; margin-top: 18px }
.form label { display: block; font-size: 11px; color: var(--text3); font-weight: 600; text-transform: uppercase; letter-spacing: .04em; margin-bottom: 6px }
.big { width: 52px; height: 52px; border-radius: 16px; flex: none; display: flex; align-items: center; justify-content: center; color: #fff; font-weight: 700; font-size: 22px }
.sws { display: flex; flex-wrap: wrap; gap: 6px; flex: 1 }
.sw { width: 28px; height: 28px; border-radius: 50%; border: 2px solid transparent; outline: 1px solid var(--line); cursor: pointer; transition: .1s }
.sw:hover { transform: scale(1.1) } .sw.on { border-color: var(--text); outline-color: var(--text) }
.hwrap { display: flex; align-items: center; gap: 6px } .hwrap span { color: var(--text3); font-size: 15px }
.err { color: var(--danger); font-size: 12.5px }
.linkbtn { background: none; border: 0; color: var(--text3); font: inherit; font-size: 12.5px; cursor: pointer; padding: 4px 2px }
.linkbtn:hover { color: var(--text2); text-decoration: underline }
</style>
