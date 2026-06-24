<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'

const bots = ref([])
const loading = ref(true)
const form = ref({ role: '', name: '', avatar: '🤖', persona: '' })
const saving = ref(false)
const ROLES = ['백엔드', '프론트엔드', 'QA', '게임 기획자', '디자이너', '데브옵스', 'AI 엔지니어', '데이터 엔지니어', 'PM']
const AVATARS = ['🤖', '🛠️', '🎨', '🧪', '🧠', '📊', '🎮', '🔧', '🚀', '🦾', '🛡️', '📐']

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
function avatarColor(role) { let h = 0; for (const c of (role || '?')) h = (h * 31 + c.charCodeAt(0)) % 360; return `hsl(${h} 52% 56%)` }
onMounted(load)
</script>

<template>
  <div class="container">
    <div class="page-title">봇 스튜디오</div>
    <div class="page-sub">
      디스코드 계정 제약이 없으니 <b>봇은 무한·커스텀</b>입니다. 직군·인격·아바타를 정해 클릭 한 번에 채용하세요.
      SYS가 필요할 때 이 봇의 Claude 세션을 띄웁니다.
    </div>

    <div class="panel" style="margin-bottom:18px">
      <h2>＋ 봇 채용</h2>
      <div style="padding:14px;display:grid;gap:10px">
        <div class="flex" style="gap:6px;flex-wrap:wrap">
          <span class="muted" style="font-size:12px">직군</span>
          <button v-for="r in ROLES" :key="r" class="btn ghost" style="padding:3px 9px;font-size:12px"
                  :style="form.role === r ? 'border-color:var(--accent);color:var(--accent)' : ''" @click="form.role = r">{{ r }}</button>
        </div>
        <input v-model="form.role" placeholder="직군 (위에서 고르거나 직접 입력)" />
        <div class="flex" style="gap:8px">
          <input v-model="form.name" placeholder="이름(선택)" style="flex:1" />
          <select v-model="form.avatar" style="width:88px">
            <option v-for="a in AVATARS" :key="a" :value="a">{{ a }}</option>
          </select>
        </div>
        <textarea v-model="form.persona" rows="2"
                  placeholder="인격(시스템 프롬프트, 선택) — 예: 보안에 깐깐하고 테스트를 먼저 쓴다"></textarea>
        <div><button class="btn" @click="recruit" :disabled="saving || !form.role.trim()">{{ saving ? '채용 중…' : '채용' }}</button></div>
      </div>
    </div>

    <div class="muted" style="margin-bottom:10px">직원 {{ bots.length }}명</div>
    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <div v-else class="grid cards">
      <router-link v-for="b in bots" :key="b.bot_id" class="card link" :to="`/agents/${b.bot_id}`">
        <div class="between">
          <div class="flex">
            <span style="width:32px;height:32px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:16px"
                  :style="{ background: avatarColor(b.role) }">{{ b.avatar || (b.role || '?').slice(0, 1) }}</span>
            <span class="nm">{{ b.role || '예비' }}</span>
          </div>
          <span class="badge" :class="{ ok: b.created_via === 'sns' }">{{ b.created_via === 'sns' ? '스튜디오' : '두뇌' }}</span>
        </div>
        <div v-if="b.name" class="muted" style="font-size:12px;margin-top:6px">{{ b.name }}</div>
        <div v-if="b.persona" class="muted" style="font-size:12px;margin-top:4px">“{{ b.persona.slice(0, 60) }}”</div>
        <div class="flex" style="gap:10px;margin-top:8px">
          <span class="badge">활동 {{ b.event_count }}</span>
          <span v-if="b.distill_count" class="grow">↑증류 {{ b.distill_count }}</span>
        </div>
      </router-link>
    </div>
  </div>
</template>
