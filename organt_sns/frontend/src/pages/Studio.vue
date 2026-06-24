<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'
import { monogram, avatarBg, AVATAR_COLORS } from '../avatar'

const bots = ref([])
const loading = ref(true)
const form = ref({ role: '', name: '', avatar: '', persona: '' })
const saving = ref(false)
const ROLES = ['백엔드', '프론트엔드', 'QA', '게임 기획자', '디자이너', '데브옵스', 'AI 엔지니어', '데이터 엔지니어', 'PM']

async function load() {
  bots.value = await api.agents({ ordering: '-event_count' })
  loading.value = false
}
async function recruit() {
  if (!form.value.role.trim()) return
  saving.value = true
  try {
    await api.recruit({ ...form.value })   // 이름 비우면 백엔드가 고유 이름 자동 배정
    form.value = { role: '', name: '', avatar: '', persona: '' }
    await load()
  } finally { saving.value = false }
}
// 미리보기 색
function previewBg() { return form.value.avatar || avatarBg({ name: form.value.name, role: form.value.role }) }
onMounted(load)
</script>

<template>
  <div class="container">
    <div class="page-title">직원 만들기</div>
    <div class="page-sub">
      원하는 만큼 직원을 만들어 팀을 꾸려보세요. 직원마다 자기 이름이 있고, 역할은 맡은 일이에요.
      한 번 만들어두면 필요할 때 알아서 일하러 옵니다.
    </div>

    <div class="panel" style="margin-bottom:24px">
      <h2>새 직원</h2>
      <div style="padding:18px;display:grid;gap:16px;max-width:560px">
        <div class="flex" style="gap:13px;align-items:center">
          <span class="bot-av" style="width:48px;height:48px;font-size:19px;border-radius:14px" :style="{ background: previewBg() }">{{ monogram(form.name, form.role) }}</span>
          <div class="muted" style="font-size:12.5px">{{ form.name || '이름은 비우면 알아서 지어줘요' }}<span v-if="form.role"> · {{ form.role }}</span></div>
        </div>
        <div>
          <label class="lbl">역할</label>
          <div class="flex" style="gap:6px;flex-wrap:wrap;margin-bottom:8px">
            <button v-for="r in ROLES" :key="r" class="chip" :class="{ on: form.role === r }" @click="form.role = r">{{ r }}</button>
          </div>
          <input v-model="form.role" placeholder="역할 — 위에서 고르거나 직접 적어도 돼요" />
        </div>
        <div>
          <label class="lbl">이름 <span class="muted" style="font-weight:400;text-transform:none">(비우면 알아서 지어줘요)</span></label>
          <input v-model="form.name" placeholder="예: 카이" />
        </div>
        <div>
          <label class="lbl">아바타 색</label>
          <div class="av-grid">
            <button class="sw" :class="{ on: !form.avatar }" :style="{ background: avatarBg({ name: form.name, role: form.role }) }" title="자동" @click="form.avatar = ''"></button>
            <button v-for="c in AVATAR_COLORS" :key="c" class="sw" :class="{ on: form.avatar === c }" :style="{ background: c }" @click="form.avatar = c"></button>
          </div>
        </div>
        <div>
          <label class="lbl">성격 <span class="muted" style="font-weight:400;text-transform:none">(어떻게 일하면 좋을지, 선택)</span></label>
          <textarea v-model="form.persona" rows="2" placeholder="예: 꼼꼼하게 보고 테스트를 먼저 챙겨요"></textarea>
        </div>
        <div><button class="btn" @click="recruit" :disabled="saving || !form.role.trim()">{{ saving ? '만드는 중…' : '직원 만들기' }}</button></div>
      </div>
    </div>

    <div class="sec-h">우리 직원 {{ bots.length }}명</div>
    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <div v-else class="grid cards">
      <router-link v-for="b in bots" :key="b.bot_id" class="card link" :to="`/agents/${b.bot_id}`">
        <div class="flex" style="gap:11px;min-width:0">
          <span class="bot-av" style="width:38px;height:38px;font-size:15px;border-radius:11px" :style="{ background: avatarBg(b) }">{{ monogram(b.name, b.role) }}</span>
          <div style="min-width:0;flex:1">
            <div class="nm" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ b.name || '이름 없음' }}</div>
            <div class="muted" style="font-size:12px">{{ b.role || '대기 중' }}</div>
          </div>
          <span class="badge" :class="b.created_via === 'sns' ? 'accent' : ''">{{ b.created_via === 'sns' ? '내가 만든' : '기본' }}</span>
        </div>
        <div v-if="b.persona" class="persona-sm">{{ b.persona }}</div>
        <div class="flex" style="gap:8px;margin-top:10px">
          <span class="badge">활동 {{ b.event_count }}</span>
          <span v-if="b.distill_count" class="grow">성장 {{ b.distill_count }}</span>
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
.sw { width: 30px; height: 30px; border-radius: 50%; border: 2px solid transparent; cursor: pointer; transition: .12s; outline: 1px solid var(--line) }
.sw:hover { transform: scale(1.08) }
.sw.on { border-color: var(--text); outline-color: var(--text) }
.persona-sm { font-size: 12px; color: var(--text2); margin-top: 10px; line-height: 1.5; padding-left: 10px; border-left: 2px solid var(--line);
  overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical }
</style>
