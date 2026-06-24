<script setup>
import { ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { register, login, loginGuest } from '../user'
import { AVATAR_COLORS } from '../avatar'
import Icon from '../components/Icon.vue'

const router = useRouter()
const route = useRoute()
const mode = ref('login')           // 'login' | 'signup'
const handle = ref(''); const name = ref(''); const password = ref(''); const color = ref(AVATAR_COLORS[0])
const busy = ref(false); const err = ref('')

function go() { router.replace(route.query.next || '/') }

async function submit() {
  err.value = ''
  const h = handle.value.trim().toLowerCase()
  if (!/^[a-z0-9_]{2,30}$/.test(h)) { err.value = '핸들은 영소문자·숫자·_ 2~30자예요.'; return }
  if (password.value.length < 4) { err.value = '비밀번호는 4자 이상이에요.'; return }
  busy.value = true
  try {
    if (mode.value === 'signup') await register({ handle: h, name: name.value.trim() || h, password: password.value, color: color.value })
    else await login({ handle: h, password: password.value })
    go()
  } catch (e) {
    err.value = e?.response?.data?.detail || (mode.value === 'signup' ? '가입에 실패했어요.' : '로그인에 실패했어요.')
  } finally { busy.value = false }
}
async function guest() {
  busy.value = true; err.value = ''
  try { await loginGuest(); go() } catch (e) { err.value = '체험 계정 생성에 실패했어요.' } finally { busy.value = false }
}
</script>

<template>
  <div class="auth">
    <div class="auth-card">
      <div class="brand"><Icon class="mark" name="layers" :size="24" /><span class="wt">Organt</span></div>
      <div class="tag">친구와 AI 직원이 함께 일하는 협업 공간</div>

      <div class="tabs">
        <button :class="{ on: mode === 'login' }" @click="mode = 'login'; err = ''">로그인</button>
        <button :class="{ on: mode === 'signup' }" @click="mode = 'signup'; err = ''">회원가입</button>
      </div>

      <div class="form">
        <div v-if="mode === 'signup'" class="avrow">
          <span class="big-av" :style="{ background: color }">{{ (name || handle || '?').slice(0, 1) }}</span>
          <div class="sws">
            <button v-for="c in AVATAR_COLORS" :key="c" class="sw" :class="{ on: color === c }" :style="{ background: c }" @click="color = c"></button>
          </div>
        </div>

        <label>핸들</label>
        <div class="hwrap"><span>@</span><input v-model="handle" placeholder="dojin" autocapitalize="off" autocomplete="username" @keyup.enter="submit" /></div>

        <template v-if="mode === 'signup'">
          <label>이름</label>
          <input v-model="name" placeholder="표시 이름 (예: 도진)" @keyup.enter="submit" />
        </template>

        <label>비밀번호</label>
        <input v-model="password" type="password" placeholder="4자 이상" :autocomplete="mode === 'signup' ? 'new-password' : 'current-password'" @keyup.enter="submit" />

        <div v-if="err" class="err">{{ err }}</div>
        <button class="btn block" @click="submit" :disabled="busy">{{ busy ? '…' : mode === 'signup' ? '가입하고 시작하기' : '로그인' }}</button>
        <button class="trybtn" @click="guest" :disabled="busy"><Icon name="target" :size="15" />로그인 없이 둘러보기</button>
      </div>
    </div>
    <div class="auth-foot">계정 하나로 친구를 맺고, 채널에 초대해 함께 프로젝트를 만들 수 있어요.</div>
  </div>
</template>

<style scoped>
.auth { min-height: 100vh; min-height: 100dvh; display: flex; flex-direction: column; align-items: center; justify-content: center;
  padding: 24px; gap: 18px;
  background:
    radial-gradient(60% 50% at 18% 0%, rgba(123,120,240,.20), transparent 60%),
    radial-gradient(55% 45% at 100% 100%, rgba(82,183,136,.10), transparent 55%),
    var(--bg); }
.auth-card { width: 100%; max-width: 400px; background: var(--elevated); border: 1px solid var(--line);
  border-radius: 20px; box-shadow: var(--shadow-lg); padding: 30px 26px;
  background-image: radial-gradient(120% 80% at 0% 0%, rgba(123,120,240,.12), transparent 55%); }
.brand { display: flex; align-items: center; gap: 9px; font-weight: 800; font-size: 24px; letter-spacing: -.03em; }
.brand .mark { color: var(--accent); filter: drop-shadow(0 0 10px rgba(123,120,240,.55)); }
.brand .wt { background: linear-gradient(96deg, #fff 10%, var(--accent2) 130%); -webkit-background-clip: text; background-clip: text; color: transparent; }
.tag { color: var(--text2); font-size: 13px; margin-top: 8px; }
.tabs { display: flex; gap: 4px; background: var(--surface); border: 1px solid var(--line); border-radius: 11px; padding: 4px; margin: 22px 0 18px; }
.tabs button { flex: 1; background: none; border: 0; color: var(--text3); font: inherit; font-weight: 600; font-size: 13.5px;
  padding: 9px; border-radius: 8px; cursor: pointer; transition: .12s; }
.tabs button.on { background: var(--accent-soft); color: #fff; }
.form { display: flex; flex-direction: column; }
.form label { font-size: 11px; color: var(--text3); font-weight: 600; text-transform: uppercase; letter-spacing: .04em; margin: 12px 0 6px; }
.form input { background: var(--surface); border: 1px solid var(--line); color: var(--text); border-radius: 9px; padding: 11px 13px; font: inherit; font-size: 14px; width: 100%; transition: border-color .12s; }
.form input:focus { outline: none; border-color: var(--accent-line); }
.hwrap { display: flex; align-items: center; gap: 7px; background: var(--surface); border: 1px solid var(--line); border-radius: 9px; padding: 0 12px; }
.hwrap:focus-within { border-color: var(--accent-line); }
.hwrap span { color: var(--text3); font-size: 15px; }
.hwrap input { background: none; border: 0; padding: 11px 0; }
.hwrap input:focus { border: 0; }
.avrow { display: flex; gap: 13px; align-items: center; margin-bottom: 4px; }
.big-av { width: 52px; height: 52px; border-radius: 16px; flex: none; display: flex; align-items: center; justify-content: center; color: #fff; font-weight: 700; font-size: 22px; }
.sws { display: flex; flex-wrap: wrap; gap: 6px; flex: 1; }
.sw { width: 26px; height: 26px; border-radius: 50%; border: 2px solid transparent; outline: 1px solid var(--line); cursor: pointer; transition: .1s; }
.sw:hover { transform: scale(1.1); } .sw.on { border-color: var(--text); outline-color: var(--text); }
.err { color: var(--danger); font-size: 12.5px; margin-top: 12px; }
.btn.block { width: 100%; margin-top: 18px; padding: 12px; font-size: 14px; }
.trybtn { width: 100%; margin-top: 10px; background: none; border: 0; color: var(--text2); font: inherit; font-size: 13px;
  cursor: pointer; padding: 9px; border-radius: 8px; display: inline-flex; align-items: center; justify-content: center; gap: 7px; transition: .12s; }
.trybtn:hover { color: var(--text); background: var(--surface); }
.auth-foot { color: var(--text3); font-size: 12px; max-width: 360px; text-align: center; line-height: 1.6; }
</style>
