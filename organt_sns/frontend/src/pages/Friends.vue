<script setup>
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'
import { me } from '../user'
import { toast } from '../toast'
import { avatarColor } from '../avatar'

const router = useRouter()
const friends = ref([])
const incoming = ref([])      // 받은 친구 요청
const outgoing = ref([])      // 보낸 친구 요청(대기)
const invites = ref([])       // 받은 채널 초대
const q = ref('')
const found = ref([])
const loading = ref(true)
const searching = ref(false)

const pbg = (p) => p.color || avatarColor(p.handle || p.name)
const ini = (p) => (p.name || p.handle || '?').slice(0, 1)
const friendSet = computed(() => new Set(friends.value.map((f) => f.handle)))
const sentSet = computed(() => new Set(outgoing.value.map((f) => f.handle)))

async function load() {
  try {
    const [fr, reqs, inv] = await Promise.all([
      api.friends().catch(() => []),
      api.friendRequests().catch(() => ({ incoming: [], outgoing: [] })),
      api.invites().catch(() => []),
    ])
    friends.value = fr
    incoming.value = reqs.incoming || []
    outgoing.value = reqs.outgoing || []
    invites.value = inv
  } finally { loading.value = false }
}
async function search() {
  if (!q.value.trim()) { found.value = []; return }
  searching.value = true
  try { found.value = await api.people(q.value.trim()) } finally { searching.value = false }
}
async function act(fn, ok, err) {
  try { await fn(); toast(ok) } catch (e) { toast(e?.response?.data?.detail || err, 'err') }
  await load()
}
const add = (handle) => act(() => api.addFriend(handle), '친구 요청을 보냈어요', '요청하지 못했어요')
const acceptFriend = (handle) => act(() => api.acceptFriend(handle), '친구가 됐어요', '수락하지 못했어요')
const declineFriend = (handle) => act(() => api.removeFriend(handle), '요청을 거절했어요', '처리하지 못했어요')
const remove = (handle) => act(() => api.removeFriend(handle), '친구를 삭제했어요', '삭제하지 못했어요')
async function acceptInvite(pid) {
  try { await api.acceptInvite(pid); toast('채널에 들어왔어요'); router.push(`/channels/${pid}`) }
  catch (e) { toast('수락하지 못했어요', 'err'); await load() }
}
const declineInvite = (pid) => act(() => api.declineInvite(pid), '초대를 거절했어요', '처리하지 못했어요')
onMounted(load)
</script>

<template>
  <div class="container" style="max-width:760px">
    <div class="page-title">친구</div>
    <div class="page-sub">친구 요청을 보내고 <b>상대가 수락</b>하면 친구가 돼요. 친구는 서로의 채널에 초대해 함께 프로젝트를 만들 수 있고, 각자 데려온 AI 직원들이 한 팀처럼 협업합니다.</div>

    <div v-if="me.is_guest" class="empty">체험 계정은 친구를 맺을 수 없어요. 회원가입하면 친구를 추가하고 채널에 초대할 수 있어요.</div>
    <template v-else>
      <!-- 받은 채널 초대 -->
      <template v-if="invites.length">
        <div class="sec-h">받은 채널 초대 {{ invites.length }}</div>
        <div class="reqs">
          <div v-for="iv in invites" :key="iv.pid" class="reqrow">
            <span class="pav hash">#</span>
            <div class="pmeta"><div class="pn">{{ iv.name || iv.pid }}</div><div class="ph">{{ iv.owner_handle ? '@' + iv.owner_handle + ' 님의 채널' : '채널' }} · {{ iv.visibility === 'public' ? '공개' : '비공개' }}</div></div>
            <button class="btn sm" @click="acceptInvite(iv.pid)">수락</button>
            <button class="btn ghost sm" @click="declineInvite(iv.pid)">거절</button>
          </div>
        </div>
      </template>

      <!-- 받은 친구 요청 -->
      <template v-if="incoming.length">
        <div class="sec-h">받은 친구 요청 {{ incoming.length }}</div>
        <div class="reqs">
          <div v-for="p in incoming" :key="p.handle" class="reqrow">
            <span class="pav" :style="{ background: pbg(p) }">{{ ini(p) }}</span>
            <div class="pmeta"><div class="pn">{{ p.name }}</div><div class="ph">@{{ p.handle }}</div></div>
            <button class="btn sm" @click="acceptFriend(p.handle)">수락</button>
            <button class="btn ghost sm" @click="declineFriend(p.handle)">거절</button>
          </div>
        </div>
      </template>

      <!-- 친구 추가 -->
      <div class="panel" style="margin-bottom:20px">
        <h2>친구 추가</h2>
        <div style="padding:14px">
          <input v-model="q" placeholder="이름이나 @핸들로 검색" @input="search" />
          <div v-if="found.length" class="results">
            <div v-for="p in found" :key="p.handle" class="prow">
              <span class="pav" :style="{ background: pbg(p) }">{{ ini(p) }}</span>
              <div class="pmeta"><div class="pn">{{ p.name }}</div><div class="ph">@{{ p.handle }}</div></div>
              <button v-if="friendSet.has(p.handle)" class="btn ghost sm" disabled>친구</button>
              <button v-else-if="sentSet.has(p.handle)" class="btn ghost sm" disabled>요청됨</button>
              <button v-else class="btn sm" @click="add(p.handle)">친구 요청</button>
            </div>
          </div>
          <div v-else-if="q && !searching" class="muted" style="font-size:12.5px;margin-top:10px">검색 결과가 없어요. 상대가 프로필을 만들어야 추가할 수 있어요.</div>
        </div>
      </div>

      <!-- 보낸 요청(대기) -->
      <template v-if="outgoing.length">
        <div class="sec-h">보낸 요청 {{ outgoing.length }} · 상대 수락 대기</div>
        <div class="grid cards" style="margin-bottom:20px">
          <div v-for="p in outgoing" :key="p.handle" class="card">
            <div class="flex" style="gap:11px;min-width:0">
              <span class="pav" :style="{ background: pbg(p), opacity: .6 }">{{ ini(p) }}</span>
              <div style="min-width:0;flex:1"><div class="nm">{{ p.name }}</div><div class="muted" style="font-size:12px">@{{ p.handle }}</div></div>
              <button class="btn ghost sm" @click="declineFriend(p.handle)">취소</button>
            </div>
          </div>
        </div>
      </template>

      <!-- 내 친구 -->
      <div class="sec-h">내 친구 {{ friends.length }}명</div>
      <div v-if="loading" class="empty"><span class="spin"></span></div>
      <div v-else-if="!friends.length" class="empty">아직 친구가 없어요. 위에서 핸들로 찾아 요청을 보내보세요.</div>
      <div v-else class="grid cards">
        <div v-for="p in friends" :key="p.handle" class="card">
          <div class="flex" style="gap:11px;min-width:0">
            <span class="pav" :style="{ background: pbg(p) }">{{ ini(p) }}</span>
            <div style="min-width:0;flex:1">
              <div class="nm">{{ p.name }}</div>
              <div class="muted" style="font-size:12px">@{{ p.handle }}</div>
            </div>
            <button class="btn ghost sm" @click="remove(p.handle)">삭제</button>
          </div>
          <div v-if="p.bio" class="muted" style="font-size:12px;margin-top:8px">{{ p.bio }}</div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.sec-h { font-size: 11px; color: var(--text3); font-weight: 600; letter-spacing: .05em; text-transform: uppercase; margin-bottom: 12px }
.reqs { display: grid; gap: 6px; margin-bottom: 22px }
.reqrow { display: flex; align-items: center; gap: 11px; padding: 10px 12px; background: var(--surface); border: 1px solid var(--line); border-radius: 12px }
.reqrow .btn.sm { flex: none }
.results { margin-top: 12px; display: grid; gap: 4px }
.prow { display: flex; align-items: center; gap: 11px; padding: 7px 9px; border-radius: 9px }
.prow:hover { background: var(--surface2) }
.pav { width: 36px; height: 36px; border-radius: 11px; flex: none; display: flex; align-items: center; justify-content: center; color: #fff; font-weight: 650; font-size: 14px }
.pav.hash { background: var(--accent-soft); color: var(--accent2); font-size: 18px }
.pmeta { flex: 1; min-width: 0 } .pn { font-size: 13.5px; font-weight: 600 } .ph { font-size: 12px; color: var(--text3) }
</style>
