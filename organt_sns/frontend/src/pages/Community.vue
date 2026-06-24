<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'
import { dateFmt } from '../kinds'

const threads = ref([])
const loading = ref(true)
const nt = ref({ title: '', body: '' })
const posting = ref(false)
const open = ref(null)
const detail = ref(null)
const comment = ref('')

const toEpoch = (iso) => new Date(iso).getTime() / 1000

async function load() { threads.value = await api.threads(); loading.value = false }

async function create() {
  if (!nt.value.title.trim()) return
  posting.value = true
  try {
    await api.createThread({ title: nt.value.title.trim(), body: nt.value.body.trim() })
    nt.value = { title: '', body: '' }
    await load()
  } finally { posting.value = false }
}
async function openThread(t) {
  open.value = open.value === t.id ? null : t.id
  if (open.value) detail.value = await api.thread(t.id)
}
async function addComment() {
  if (!comment.value.trim()) return
  await api.addComment(open.value, { body: comment.value.trim() })
  comment.value = ''
  detail.value = await api.thread(open.value)
  await load()
}
async function like(t) { const r = await api.like(t.id); t.like_count = r.like_count }
onMounted(load)
</script>

<template>
  <div class="container">
    <div class="page-title">커뮤니티</div>
    <div class="page-sub">AI 직원들의 협업을 주제로 사람들이 의견을 나누는 공간. 글을 쓰고 댓글·좋아요로 소통하세요.</div>

    <div class="panel" style="margin-bottom:16px">
      <h2>새 글 작성</h2>
      <div style="padding:14px;display:grid;gap:8px">
        <input v-model="nt.title" placeholder="제목" @keyup.enter="create" />
        <textarea v-model="nt.body" rows="2" placeholder="내용(선택)"></textarea>
        <div><button class="btn" @click="create" :disabled="posting || !nt.title.trim()">
          {{ posting ? '등록 중…' : '글 등록' }}
        </button></div>
      </div>
    </div>

    <div v-if="loading" class="empty"><span class="spin"></span></div>
    <div v-else-if="!threads.length" class="empty">아직 글이 없습니다. 첫 글을 남겨보세요.</div>
    <div v-else class="grid" style="gap:12px">
      <div v-for="t in threads" :key="t.id" class="card">
        <div class="between">
          <div class="nm" style="font-size:15px;cursor:pointer" @click="openThread(t)">{{ t.title }}</div>
          <div class="flex">
            <span class="badge">💬 {{ t.comment_count }}</span>
            <button class="btn ghost" style="padding:4px 10px" @click="like(t)">♥ {{ t.like_count }}</button>
          </div>
        </div>
        <div v-if="t.body" class="muted" style="font-size:13px;margin-top:6px">{{ t.body }}</div>
        <div class="muted" style="font-size:11px;margin-top:6px">{{ dateFmt(toEpoch(t.created_at)) }}</div>

        <div v-if="open === t.id && detail" style="margin-top:12px;border-top:1px solid var(--bd2);padding-top:10px">
          <div v-if="!detail.comments || !detail.comments.length" class="muted" style="font-size:12px">첫 댓글을 남겨보세요</div>
          <div v-for="c in detail.comments" :key="c.id" style="padding:6px 0;border-bottom:1px solid var(--bd2)">
            <span class="nm" style="font-size:13px">{{ c.author_name }}</span>
            <span class="muted" style="font-size:11px;margin-left:6px">{{ dateFmt(toEpoch(c.created_at)) }}</span>
            <div style="font-size:13px">{{ c.body }}</div>
          </div>
          <div class="flex" style="margin-top:8px">
            <input v-model="comment" placeholder="댓글을 입력하세요…" @keyup.enter="addComment" />
            <button class="btn" @click="addComment" :disabled="!comment.trim()">댓글</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
