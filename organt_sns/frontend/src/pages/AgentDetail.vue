<script setup>
import { ref, onMounted, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import EventItem from '../components/EventItem.vue'
import Icon from '../components/Icon.vue'
import { dayKey, dayLabel } from '../kinds'
import { monogram, avatarBg, AVATAR_COLORS } from '../avatar'
import { toast } from '../toast'
import { me } from '../user'

const route = useRoute()
const agent = ref(null)
const isMine = computed(() => agent.value && agent.value.owner_handle && agent.value.owner_handle === me.handle)
const events = ref([])
// 활동 피드에 날짜 구분선 삽입 — '시:분'만 나열돼 언제인지 모르는 문제 해결.
const feedRows = computed(() => {
  const out = []; let last = null
  for (const e of events.value) {
    const k = dayKey(e.ts)
    if (k !== last) { out.push({ day: true, key: 'd' + k, label: dayLabel(e.ts) }); last = k }
    out.push({ day: false, key: 'e' + e.seq, ev: e })
  }
  return out
})
const profile = ref(null)
const loading = ref(true)
const editing = ref(false)
const saving = ref(false)
const form = ref({ name: '', role: '', avatar: '', persona: '', model: '' })

const error = ref(false)
async function load() {
  loading.value = true; error.value = false
  const id = route.params.botId
  try {
    agent.value = await api.agent(id)
    events.value = await api.agentEvents(id)
    const profs = await api.profiles()
    profile.value = profs.find((p) => p.role === agent.value.role) || null
  } catch (e) { error.value = true; agent.value = null }
  finally { loading.value = false }
}
function startEdit() {
  form.value = { name: agent.value.name || '', role: agent.value.role || '', avatar: agent.value.avatar || '', persona: agent.value.persona || '', model: agent.value.model || '' }
  editing.value = true
}
async function saveEdit() {
  saving.value = true
  try {
    agent.value = await api.editAgent(route.params.botId, form.value)
    editing.value = false
    toast('직원 정보를 저장했어요')
  } catch (e) { toast('저장하지 못했어요', 'err') }
  finally { saving.value = false }
}
async function doShare() {
  try {
    agent.value = await api.shareAgent(route.params.botId)
    toast(agent.value.visibility === 'public' ? '공개로 전환했어요 — 모두가 쓸 수 있어요' : '비공개로 전환했어요')
  } catch (e) { toast('전환하지 못했어요', 'err') }
}
const isPublic = computed(() => agent.value && agent.value.visibility === 'public')
const joinedFmt = computed(() => {
  if (!agent.value || !agent.value.joined_at) return ''
  try { return new Date(agent.value.joined_at).toLocaleDateString('ko-KR', { year: 'numeric', month: 'long', day: 'numeric' }) } catch (e) { return '' }
})
const origin = computed(() => (agent.value && agent.value.created_via === 'sns') ? '스튜디오 채용' : '디스코드 합류')
onMounted(load)
watch(() => route.params.botId, () => { editing.value = false; load() })
</script>

<template>
  <div class="container" v-if="!loading && agent">
    <router-link to="/agents" class="back"><Icon name="arrowL" :size="15" />우리 직원</router-link>

    <div class="prof-head">
      <span class="big-av" :style="{ background: avatarBg(agent) }">{{ monogram(agent.name, agent.role) }}</span>
      <div style="flex:1;min-width:0">
        <div class="between">
          <div>
            <div class="page-title" style="margin:0">{{ agent.name || '이름 없음' }}</div>
            <div class="flex" style="gap:7px;margin-top:4px;flex-wrap:wrap">
              <span class="muted" style="font-size:14px">{{ agent.role || '대기 중' }}</span>
              <span v-if="agent.is_leader" class="badge lead">리더</span>
              <span v-if="isMine" class="badge accent">내 직원</span>
              <span v-else class="badge">공개 직원</span>
              <span v-if="isMine" class="badge" :class="{ ok: isPublic }">{{ isPublic ? '공유됨 · 공개' : '비공개' }}</span>
              <span v-if="agent.model" class="badge" :title="'이 직원이 쓰는 LLM'">{{ agent.model }}</span>
            </div>
          </div>
          <div v-if="isMine && !editing" class="flex" style="gap:7px">
            <button class="btn ghost sm" @click="doShare" :title="isPublic ? '비공개로 — 나만 사용' : '공개로 — 모두가 보고 쓸 수 있게'">
              <Icon :name="isPublic ? 'lock' : 'globe'" :size="15" />{{ isPublic ? '비공개로' : '공유하기' }}
            </button>
            <button class="btn ghost sm" @click="startEdit"><Icon name="edit" :size="15" />수정</button>
          </div>
        </div>
        <div class="meta-row">
          <span>활동 {{ agent.event_count }}회</span>
          <span v-if="agent.distill_count">성장 {{ agent.distill_count }}회</span>
          <span>{{ origin }}</span>
          <span v-if="isMine && joinedFmt">{{ joinedFmt }} 합류</span>
        </div>
      </div>
    </div>

    <!-- 성격 · 일하는 방식(인격) -->
    <div v-if="agent.persona && !editing" class="panel persona-panel">
      <h2>성격 · 일하는 방식</h2>
      <div class="persona-body">{{ agent.persona }}</div>
    </div>
    <div v-else-if="isMine && !agent.persona && !editing" class="sb-hint" style="margin:0 0 18px">
      아직 성격이 정해지지 않았어요. <button class="linklike" @click="startEdit">성격 추가하기</button> — 어떻게 일하면 좋을지 적어두면 더 또렷한 직원이 됩니다.
    </div>

    <div v-if="editing" class="panel" style="margin-bottom:18px">
      <h2>직원 정보 수정</h2>
      <div style="padding:18px;display:grid;gap:14px">
        <div>
          <label class="lbl">이름</label>
          <input v-model="form.name" placeholder="직원 이름" />
        </div>
        <div>
          <label class="lbl">역할</label>
          <input v-model="form.role" placeholder="역할 — 맡은 일" />
        </div>
        <div>
          <label class="lbl">아바타 색</label>
          <div class="av-grid">
            <button class="sw" :class="{ on: !form.avatar }" :style="{ background: avatarBg({ name: form.name, role: form.role }) }" title="자동" @click="form.avatar = ''"></button>
            <button v-for="c in AVATAR_COLORS" :key="c" class="sw" :class="{ on: form.avatar === c }" :style="{ background: c }" @click="form.avatar = c"></button>
          </div>
        </div>
        <div>
          <label class="lbl">LLM 모델</label>
          <div class="seg">
            <button type="button" :class="{ on: !form.model }" @click="form.model = ''">기본</button>
            <button type="button" :class="{ on: form.model === 'opus' }" @click="form.model = 'opus'">Opus</button>
            <button type="button" :class="{ on: form.model === 'sonnet' }" @click="form.model = 'sonnet'">Sonnet</button>
            <button type="button" :class="{ on: form.model === 'haiku' }" @click="form.model = 'haiku'">Haiku</button>
          </div>
          <div class="muted" style="font-size:11.5px;margin-top:6px">이 직원이 쓸 LLM. ‘기본’이면 러너 전역 모델을 따릅니다(협업 엔진 가동 시 적용).</div>
        </div>
        <div>
          <label class="lbl">성격</label>
          <textarea v-model="form.persona" rows="3" placeholder="어떻게 일하면 좋을지 (선택)"></textarea>
        </div>
        <div class="flex" style="gap:8px">
          <button class="btn" @click="saveEdit" :disabled="saving">{{ saving ? '저장 중…' : '저장' }}</button>
          <button class="btn ghost" @click="editing = false" :disabled="saving">취소</button>
        </div>
      </div>
    </div>

    <div class="grid cols2">
      <div class="panel" style="align-self:start">
        <h2>최근 활동</h2>
        <div v-if="!events.length" class="empty">아직 활동 기록이 없어요</div>
        <template v-for="r in feedRows" :key="r.key">
          <div v-if="r.day" class="feed-day">{{ r.label }}</div>
          <EventItem v-else :ev="r.ev" />
        </template>
      </div>
      <div class="panel" style="align-self:start">
        <h2>쌓은 노하우 · {{ agent.role }}</h2>
        <div v-if="profile" style="padding:16px">
          <div class="flex" style="margin-bottom:12px">
            <span class="grow">성장 {{ profile.distill_count }}회</span>
            <span class="badge">새 경험 {{ profile.experience_count }}</span>
          </div>
          <div class="pre">{{ profile.criteria || '(아직 없어요)' }}</div>
        </div>
        <div v-else class="empty">아직 쌓은 노하우가 없어요</div>
      </div>
    </div>
  </div>
  <div v-else-if="error" class="container empty">직원을 찾을 수 없습니다. <router-link to="/agents" class="muted" style="text-decoration:underline">목록으로</router-link></div>
  <div v-else class="container empty"><span class="spin"></span></div>
</template>

<style scoped>
.back { display: inline-flex; align-items: center; gap: 5px; color: var(--text3); font-size: 13px }
.back:hover { color: var(--text) }
.prof-head { display: flex; gap: 18px; align-items: flex-start; margin: 16px 0 22px }
.big-av { width: 64px; height: 64px; border-radius: 19px; flex: none; display: flex; align-items: center; justify-content: center;
  font-size: 27px; color: #fff; font-weight: 700 }
.persona { margin-top: 14px; color: var(--text); font-size: 13px; line-height: 1.6; padding: 11px 14px;
  background: var(--surface2); border: 1px solid var(--line); border-left: 2px solid var(--accent); border-radius: var(--r) }
.meta-row { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 9px; color: var(--text3); font-size: 12px; font-variant-numeric: tabular-nums }
.meta-row span { position: relative }
.meta-row span:not(:first-child)::before { content: '·'; position: absolute; left: -8px; color: var(--line) }
.persona-panel { margin-bottom: 18px }
.persona-body { padding: 14px 16px; color: var(--text); font-size: 13.5px; line-height: 1.65; white-space: pre-wrap }
.badge.ok { color: var(--ok); background: var(--ok-soft) }
.linklike { background: none; border: 0; color: var(--accent2); font: inherit; font-weight: 600; cursor: pointer; padding: 0; text-decoration: underline }
.lbl { display: block; font-size: 11px; color: var(--text3); font-weight: 600; letter-spacing: .04em; text-transform: uppercase; margin-bottom: 7px }
.sw { width: 30px; height: 30px; border-radius: 50%; border: 2px solid transparent; cursor: pointer; transition: .12s; outline: 1px solid var(--line) }
.sw:hover { transform: scale(1.08) }
.sw.on { border-color: var(--text); outline-color: var(--text) }
.cols2 { grid-template-columns: 1fr 1fr }
.feed-day { padding: 10px 16px 4px; font-size: 11px; font-weight: 700; color: var(--text3); letter-spacing: .03em; border-bottom: 1px solid var(--line2) }
@media(max-width:760px){ .cols2 { grid-template-columns: 1fr } }
@media(max-width:560px){ .prof-head .between { flex-wrap: wrap; gap: 10px } }   /* 액션 버튼이 긴 이름에 안 눌리게 */
</style>
