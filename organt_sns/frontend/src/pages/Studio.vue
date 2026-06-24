<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'

const bots = ref([])
const loading = ref(true)
const form = ref({ role: '', name: '', avatar: '🤖', persona: '' })
const saving = ref(false)
const ROLES = ['백엔드', '프론트엔드', 'QA', '게임 기획자', '디자이너', '데브옵스', 'AI 엔지니어', '데이터 엔지니어', 'PM']
const AVATARS = ['🤖', '🛠', '🎨', '🧪', '🧠', '📊', '🎮', '🔧', '🚀', '🦾', '🛡', '📐']

async function load() {
  bots.value = await api.agents({ ordering: '-event_count' })
  loading.value = false
}
async function recruit() {
  if (!form.value.role.trim()) return
  saving.value = true
  try {
    await api.recruit({ ...form.value })
    form.value = { role: '', name: '', avatar: '🤖', persona: '' }
    await load()
  } finally { saving.value = false }
}
function avatarColor(role) { let h = 0; for (const c of (role || '?')) h = (h * 31 + c.charCodeAt(0)) % 360; return `hsl(${h} 48% 56%)` }
onMounted(load)
</script>

<template>
  <div class="container">
    <div class="page-title">봇 스튜디오</div>
    <div class="page-sub">
      디스코드 계정 제약이 없으니 봇은 무한·커스텀입니다. 직군·인격·아바타를 정해 채용하면, SYS가
      필요할 때 이 봇의 Claude 세션을 띄웁니다.
    </div>

    <div class="panel" style="margin-bottom:24px">
      <h2>봇 채용</h2>
      <div style="padding:18px;display:grid;gap:14px;max-width:560px">
        <div>
          <label class="lbl">직군</label>
          <div class="flex" style="gap:6px;flex-wrap:wrap;margin-bottom:8px">
            <button v-for="r in ROLES" :key="r" class="chip" :class="{ on: form.role === r }" @click="form.role = r">{{ r }}</button>
          </div>
          <input v-model="form.role" placeholder="직군 — 위에서 고르거나 직접 입력" />
        </div>
        <div>
          <label class="lbl">아바타</label>
          <div class="av-grid">
            <button v-for="a in AVATARS" :key="a" class="av-pick" :class="{ on: form.avatar === a }" @click="form.avatar = a">{{ a }}</button>
          </div>
        </div>
        <div>
          <label class="lbl">이름 (선택)</label>
          <input v-model="form.name" placeholder="예: 카이" />
        </div>
        <div>
          <label class="lbl">인격 (시스템 프롬프트, 선택)</label>
          <textarea v-model="form.persona" rows="2" placeholder="예: 보안에 깐깐하고 테스트를 먼저 쓴다"></textarea>
        </div>
        <div><button class="btn" @click="recruit" :disabled="saving || !form.role.trim()">{{ saving ? '채용 중…' : '채용하기' }}</button></div>
      </div>
    </div>

    <div class="sec-h">직원 · {{ bots.length }}명</div>
    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <div v-else class="grid cards">
      <router-link v-for="b in bots" :key="b.bot_id" class="card link" :to="`/agents/${b.bot_id}`">
        <div class="between">
          <div class="flex" style="gap:10px;min-width:0">
            <span class="bot-av" style="width:34px;height:34px;font-size:16px;border-radius:10px" :style="{ background: avatarColor(b.role) }">{{ b.avatar || (b.role || '?').slice(0, 1) }}</span>
            <span class="nm" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ b.role || '예비' }}</span>
          </div>
          <span class="badge" :class="b.created_via === 'sns' ? 'accent' : ''">{{ b.created_via === 'sns' ? '스튜디오' : '두뇌' }}</span>
        </div>
        <div v-if="b.name" class="muted" style="font-size:12.5px;margin-top:8px">{{ b.name }}</div>
        <div v-if="b.persona" class="persona-sm">{{ b.persona.slice(0, 64) }}</div>
        <div class="flex" style="gap:8px;margin-top:10px">
          <span class="badge">활동 {{ b.event_count }}</span>
          <span v-if="b.distill_count" class="grow">증류 {{ b.distill_count }}</span>
        </div>
      </router-link>
    </div>
  </div>
</template>

<style scoped>
.lbl { display: block; font-size: 11px; color: var(--text3); font-weight: 600; letter-spacing: .04em; text-transform: uppercase; margin-bottom: 7px }
.chip { background: var(--surface2); border: 1px solid var(--line); border-radius: 20px; color: var(--text2); padding: 5px 13px;
  font: inherit; font-size: 12.5px; cursor: pointer; transition: .12s }
.chip:hover { color: var(--text) }
.chip.on { background: var(--accent-soft); border-color: var(--accent-line); color: var(--accent2) }
.sec-h { font-size: 11px; color: var(--text3); font-weight: 600; letter-spacing: .05em; text-transform: uppercase; margin-bottom: 12px }
.persona-sm { font-size: 12px; color: var(--text2); margin-top: 6px; line-height: 1.5; padding-left: 10px; border-left: 2px solid var(--line);
  overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical }
</style>
