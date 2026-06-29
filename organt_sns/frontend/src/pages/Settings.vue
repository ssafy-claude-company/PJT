<script setup>
// 내 환경 변수 · 시크릿 금고(BYO 키) — 배포에 쓸 내 키를 암호화 보관.
// 값은 저장 후 화면에 다시 안 보이고(이름+힌트만), 봇은 이 값을 못 읽습니다.
import { ref, onMounted, computed } from 'vue'
import api from '../api'
import { toast } from '../toast'
import { me } from '../user'
import Icon from '../components/Icon.vue'

const data = ref(null)
const loading = ref(true)
const saving = ref('')
const inputs = ref({})                       // name → 입력 중 값
const newName = ref('')
const newVal = ref('')

// 배포에 필요한 표준 키 4개 — 안내·완비 판정용
const DEPLOY = [
  { name: 'RENDER_KEY', label: 'Render API Key', ph: 'rnd_…', help: 'Render → Account Settings → API Keys' },
  { name: 'RENDER_OWNER', label: 'Render Owner ID', ph: 'tea-… 또는 usr-…', help: 'Render 대시보드 URL에서 확인' },
  { name: 'GH_PAT', label: 'GitHub Token', ph: 'github_pat_… / ghp_…', help: 'fine-grained 권장 · 배포 repo만' },
  { name: 'GH_USER', label: 'GitHub 사용자명', ph: 'myname', help: 'GitHub 계정 아이디' },
]
const have = computed(() => new Map((data.value?.secrets || []).map((s) => [s.name, s])))
const deployReady = computed(() => !!data.value?.deploy_ready)
const custom = computed(() => (data.value?.secrets || []).filter((s) => !DEPLOY.some((d) => d.name === s.name)))
const isGuest = computed(() => me.is_guest)

async function load() {
  loading.value = true
  try { data.value = await api.secrets() } finally { loading.value = false }
}
async function save(name, val) {
  const v = (val ?? inputs.value[name] ?? '').trim()
  if (!v) { toast('값을 입력하세요'); return }
  saving.value = name
  try { data.value = await api.setSecret(name, v); inputs.value[name] = ''; toast(name + ' 저장됨') }
  catch (e) { toast(e?.response?.data?.detail || '저장 실패') }
  finally { saving.value = '' }
}
async function addCustom() {
  const n = newName.value.trim(); const v = newVal.value.trim()
  if (!n || !v) { toast('이름과 값을 모두 입력하세요'); return }
  saving.value = '__new'
  try { data.value = await api.setSecret(n, v); newName.value = ''; newVal.value = ''; toast(n + ' 저장됨') }
  catch (e) { toast(e?.response?.data?.detail || '저장 실패') }
  finally { saving.value = '' }
}
async function remove(name) {
  try { await api.delSecret(name); await load(); toast(name + ' 삭제됨') }
  catch { toast('삭제 실패') }
}
onMounted(load)
</script>

<template>
  <div class="settings">
    <header class="s-head">
      <h1>환경 변수 · 시크릿</h1>
      <p class="s-sub">배포에 쓸 <b>내 키</b>를 암호화해서 맡깁니다. 저장하면 값은 화면에 다시 안 보이고
        <b>봇도 값을 못 읽습니다</b> — 봇은 '배포 버튼'만 갖고, 키는 이 금고가 대신 씁니다.</p>
    </header>

    <div v-if="loading" class="empty" style="padding:30px"><span class="spin"></span></div>
    <div v-else-if="isGuest" class="guest-note">
      체험 계정은 키를 저장할 수 없어요. <router-link to="/login" class="lnk">회원가입</router-link> 후 이용하세요.
    </div>
    <template v-else>
      <!-- 배포 키(BYO) -->
      <section class="card">
        <div class="c-head">
          <h2>배포 키 <span class="byo">BYO</span></h2>
          <span class="ready" :class="{ on: deployReady }">
            <Icon :name="deployReady ? 'check' : 'lock'" :size="13" />{{ deployReady ? '배포 준비 완료' : '미완성' }}</span>
        </div>
        <p class="c-note">이 4개를 채우면 당신 프로젝트의 봇이 <b>당신 Render 계정</b>에 배포합니다(비용·소유 당신). 운영자 키 안 씀.</p>
        <div class="rows">
          <div v-for="d in DEPLOY" :key="d.name" class="row">
            <div class="r-meta">
              <div class="r-name">{{ d.label }} <code>{{ d.name }}</code></div>
              <div class="r-help">{{ d.help }}</div>
            </div>
            <div class="r-state" v-if="have.get(d.name)">
              <span class="set"><Icon name="check" :size="13" />설정됨 <span class="hint">{{ have.get(d.name).hint }}</span></span>
              <button class="del" title="삭제" @click="remove(d.name)"><Icon name="trash" :size="14" /></button>
            </div>
            <div class="r-input">
              <input :type="d.name === 'GH_USER' ? 'text' : 'password'" v-model="inputs[d.name]"
                     :placeholder="have.get(d.name) ? '새 값으로 교체…' : d.ph" autocomplete="off"
                     @keyup.enter="save(d.name)" />
              <button class="btn sm" :disabled="saving === d.name" @click="save(d.name)">
                {{ saving === d.name ? '…' : (have.get(d.name) ? '교체' : '저장') }}</button>
            </div>
          </div>
        </div>
      </section>

      <!-- 기타 환경 변수 -->
      <section class="card">
        <div class="c-head"><h2>기타 환경 변수</h2></div>
        <div v-if="custom.length" class="rows">
          <div v-for="s in custom" :key="s.name" class="row compact">
            <div class="r-meta"><div class="r-name"><code>{{ s.name }}</code></div></div>
            <div class="r-state">
              <span class="set"><Icon name="check" :size="13" />설정됨 <span class="hint">{{ s.hint }}</span></span>
              <button class="del" title="삭제" @click="remove(s.name)"><Icon name="trash" :size="14" /></button>
            </div>
          </div>
        </div>
        <div class="addrow">
          <input class="k" v-model="newName" placeholder="이름 (예: STRIPE_KEY)" autocomplete="off" />
          <input class="v" type="password" v-model="newVal" placeholder="값" autocomplete="off" @keyup.enter="addCustom" />
          <button class="btn sm" :disabled="saving === '__new'" @click="addCustom">{{ saving === '__new' ? '…' : '추가' }}</button>
        </div>
      </section>

      <p class="foot"><Icon name="shield" :size="13" /> 값은 서버에 <b>암호화 저장</b>되고 응답·화면으로 다시 나오지 않습니다. 봇 셸에서도 읽을 수 없습니다.</p>
    </template>
  </div>
</template>

<style scoped>
.settings { max-width: 720px; margin: 0 auto; padding: 28px 22px 60px }
.s-head h1 { font-size: 22px; font-weight: 700 }
.s-sub { color: var(--text2); font-size: 13px; line-height: 1.65; margin-top: 8px }
.guest-note { background: var(--surface2); border: 1px solid var(--line); border-radius: var(--r); padding: 20px; color: var(--text2); font-size: 13px; margin-top: 18px }
.lnk { color: var(--accent) }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); padding: 18px 18px 20px; margin-top: 18px }
.c-head { display: flex; align-items: center; gap: 10px }
.c-head h2 { font-size: 15px; font-weight: 700 }
.byo { font-size: 10px; font-weight: 700; color: var(--accent2); border: 1px solid var(--accent-line); border-radius: 5px; padding: 1px 6px; vertical-align: middle }
.ready { margin-left: auto; font-size: 11.5px; font-weight: 600; color: var(--text3); display: inline-flex; align-items: center; gap: 5px; background: var(--surface2); border: 1px solid var(--line); border-radius: 20px; padding: 3px 11px }
.ready.on { color: var(--ok); border-color: rgba(82, 183, 136, .35) }
.c-note { font-size: 12px; color: var(--text2); line-height: 1.6; margin: 10px 0 14px }
.rows { display: grid; gap: 12px }
.row { display: grid; grid-template-columns: 1fr; gap: 8px; padding: 12px 0; border-top: 1px solid var(--line) }
.row:first-child { border-top: 0; padding-top: 2px }
.row.compact { grid-template-columns: 1fr auto; align-items: center; padding: 9px 0 }
.r-name { font-size: 13px; font-weight: 600 }
.r-name code { font-size: 11px; color: var(--text3); background: var(--surface2); padding: 1px 6px; border-radius: 5px; margin-left: 5px; font-weight: 500 }
.r-help { font-size: 11.5px; color: var(--text3); margin-top: 2px }
.r-state { display: flex; align-items: center; gap: 8px }
.set { font-size: 12px; color: var(--ok); display: inline-flex; align-items: center; gap: 5px }
.hint { color: var(--text3); font-family: monospace; font-size: 11.5px }
.del { background: none; border: 0; color: var(--text3); cursor: pointer; padding: 3px; border-radius: 6px }
.del:hover { color: var(--danger, #e5534b); background: var(--surface2) }
.r-input, .addrow { display: flex; gap: 8px }
.r-input input, .addrow input { flex: 1; background: var(--surface2); border: 1px solid var(--line); border-radius: 8px; padding: 8px 11px; color: var(--text); font-size: 13px; min-width: 0 }
.addrow input.k { flex: 0 0 38% }
.r-input input:focus, .addrow input:focus { border-color: var(--accent-line); outline: none }
.addrow { margin-top: 14px }
.foot { margin-top: 18px; font-size: 11.5px; color: var(--text3); display: flex; align-items: center; gap: 6px; line-height: 1.5 }
</style>
