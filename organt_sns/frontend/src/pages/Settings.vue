<script setup>
// 내 환경 변수 · 시크릿 금고 — 범용 NAME=VALUE 저장소(플랫폼 무관).
// 어떤 이름이든 암호화 저장. 저장 후 값은 화면에 다시 안 보이고(••••마지막4자), 봇은 값을 못 읽는다.
// 배포는 그 플랫폼이 요구하는 키를 금고에서 찾아 쓴다(Render·Vercel·Netlify… 어댑터별).
import { ref, onMounted, computed } from 'vue'
import api from '../api'
import { toast } from '../toast'
import { me } from '../user'
import Icon from '../components/Icon.vue'

const data = ref(null)
const loading = ref(true)
const saving = ref(false)
const newName = ref('')
const newVal = ref('')
const editing = ref('')           // 교체 중인 이름
const editVal = ref('')

const secrets = computed(() => data.value?.secrets || [])
const isGuest = computed(() => me.is_guest)

// 흔한 배포/서비스 키 이름 — 자동완성 제안일 뿐, 강제 아님(어떤 이름이든 저장 가능).
const SUGGEST = [
  'RENDER_KEY', 'RENDER_OWNER', 'GH_PAT', 'GH_USER',
  'VERCEL_TOKEN', 'VERCEL_ORG_ID', 'VERCEL_PROJECT_ID',
  'NETLIFY_AUTH_TOKEN', 'NETLIFY_SITE_ID', 'FLY_API_TOKEN',
  'CLOUDFLARE_API_TOKEN', 'CLOUDFLARE_ACCOUNT_ID',
  'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'OPENAI_API_KEY',
]

async function load() {
  loading.value = true
  try { data.value = await api.secrets() } finally { loading.value = false }
}
async function add() {
  const n = newName.value.trim(); const v = newVal.value.trim()
  if (!n || !v) { toast('이름과 값을 모두 입력하세요'); return }
  saving.value = true
  try { data.value = await api.setSecret(n, v); newName.value = ''; newVal.value = ''; toast(n + ' 저장됨') }
  catch (e) { toast(e?.response?.data?.detail || '저장 실패') }
  finally { saving.value = false }
}
function startEdit(name) { editing.value = name; editVal.value = '' }
async function replace(name) {
  const v = editVal.value.trim()
  if (!v) { toast('새 값을 입력하세요'); return }
  saving.value = true
  try { data.value = await api.setSecret(name, v); editing.value = ''; editVal.value = ''; toast(name + ' 교체됨') }
  catch (e) { toast(e?.response?.data?.detail || '교체 실패') }
  finally { saving.value = false }
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
      <p class="s-sub">키·값을 <b>암호화해서</b> 맡깁니다. 저장하면 값은 화면에 다시 안 보이고
        <b>봇도 값을 못 읽습니다</b>. 배포할 땐 그 플랫폼이 요구하는 키를 금고가 대신 씁니다(플랫폼 무관).</p>
    </header>

    <div v-if="loading" class="empty" style="padding:30px"><span class="spin"></span></div>
    <div v-else-if="isGuest" class="guest-note">
      체험 계정은 키를 저장할 수 없어요. <router-link to="/login" class="lnk">회원가입</router-link> 후 이용하세요.
    </div>
    <template v-else>
      <section class="card">
        <!-- 저장된 값 목록 -->
        <div v-if="secrets.length" class="rows">
          <div v-for="s in secrets" :key="s.name" class="row">
            <div class="r-main">
              <code class="r-name">{{ s.name }}</code>
              <span class="set"><Icon name="check" :size="12" /><span class="hint">{{ s.hint }}</span></span>
            </div>
            <div class="r-act">
              <button class="lnkbtn" @click="editing === s.name ? (editing = '') : startEdit(s.name)">
                {{ editing === s.name ? '취소' : '교체' }}</button>
              <button class="del" title="삭제" @click="remove(s.name)"><Icon name="trash" :size="14" /></button>
            </div>
            <div v-if="editing === s.name" class="r-edit">
              <input type="password" v-model="editVal" placeholder="새 값…" autocomplete="off" @keyup.enter="replace(s.name)" />
              <button class="btn sm" :disabled="saving" @click="replace(s.name)">{{ saving ? '…' : '저장' }}</button>
            </div>
          </div>
        </div>
        <div v-else class="empty2">아직 저장한 값이 없어요. 아래에서 추가하세요.</div>

        <!-- 추가 -->
        <div class="addrow">
          <input class="k" v-model="newName" list="secret-suggest" placeholder="이름 (예: RENDER_KEY)" autocomplete="off" />
          <input class="v" type="password" v-model="newVal" placeholder="값" autocomplete="off" @keyup.enter="add" />
          <button class="btn sm" :disabled="saving" @click="add">{{ saving ? '…' : '추가' }}</button>
        </div>
        <datalist id="secret-suggest"><option v-for="n in SUGGEST" :key="n" :value="n" /></datalist>
        <p class="tip">배포에 쓰는 키 예시 — <b>Render</b>: RENDER_KEY · RENDER_OWNER · GH_PAT · GH_USER ·
          <b>Vercel</b>: VERCEL_TOKEN · <b>Netlify</b>: NETLIFY_AUTH_TOKEN. 그 외 어떤 이름이든 자유롭게.</p>
      </section>

      <p class="foot"><Icon name="shield" :size="13" /> 값은 서버에 <b>암호화 저장</b>되고 응답·화면으로 다시 나오지 않습니다. 봇 셸에서도 읽을 수 없습니다.</p>
    </template>
  </div>
</template>

<style scoped>
.settings { max-width: 700px; margin: 0 auto; padding: 28px 22px 60px }
.s-head h1 { font-size: 22px; font-weight: 700 }
.s-sub { color: var(--text2); font-size: 13px; line-height: 1.65; margin-top: 8px }
.guest-note { background: var(--surface2); border: 1px solid var(--line); border-radius: var(--r); padding: 20px; color: var(--text2); font-size: 13px; margin-top: 18px }
.lnk { color: var(--accent) }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r); padding: 16px 18px 18px; margin-top: 18px }
.rows { display: grid; gap: 2px; margin-bottom: 14px }
.row { display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 10px; padding: 11px 0; border-bottom: 1px solid var(--line) }
.row:last-child { border-bottom: 0 }
.r-main { display: flex; align-items: center; gap: 12px; min-width: 0 }
.r-name { font-size: 13px; font-weight: 600; color: var(--text); background: var(--surface2); padding: 3px 9px; border-radius: 6px }
.set { font-size: 12px; color: var(--ok); display: inline-flex; align-items: center; gap: 5px }
.hint { color: var(--text3); font-family: monospace; font-size: 11.5px }
.r-act { display: flex; align-items: center; gap: 4px }
.lnkbtn { background: none; border: 0; color: var(--text3); font-size: 12px; cursor: pointer; padding: 4px 8px; border-radius: 6px }
.lnkbtn:hover { color: var(--text); background: var(--surface2) }
.del { background: none; border: 0; color: var(--text3); cursor: pointer; padding: 4px; border-radius: 6px }
.del:hover { color: var(--danger, #e5534b); background: var(--surface2) }
.r-edit { grid-column: 1 / -1; display: flex; gap: 8px; margin-top: 8px }
.empty2 { font-size: 12.5px; color: var(--text3); padding: 8px 2px 14px }
.addrow { display: flex; gap: 8px; padding-top: 14px; border-top: 1px solid var(--line) }
.addrow input, .r-edit input { background: var(--surface2); border: 1px solid var(--line); border-radius: 8px; padding: 8px 11px; color: var(--text); font-size: 13px; min-width: 0 }
.addrow input.k { flex: 0 0 40% }
.addrow input.v, .r-edit input { flex: 1 }
.addrow input:focus, .r-edit input:focus { border-color: var(--accent-line); outline: none }
.tip { font-size: 11.5px; color: var(--text3); line-height: 1.6; margin-top: 12px }
.foot { margin-top: 16px; font-size: 11.5px; color: var(--text3); display: flex; align-items: center; gap: 6px; line-height: 1.5 }
</style>
