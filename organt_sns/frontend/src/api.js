import axios from 'axios'

// 개발: vite 프록시가 /api → :8000. 배포: 동일 출처에서 Django가 서빙.
const http = axios.create({ baseURL: '/api', timeout: 20000 })

// 인증 — 저장된 토큰을 Authorization 헤더로(회원가입/로그인 시 발급).
http.interceptors.request.use((cfg) => {
  const t = localStorage.getItem('organt_token')
  if (t) cfg.headers['Authorization'] = `Token ${t}`
  return cfg
})

// 토큰이 무효(예: DB 전환으로 계정 사라짐)면 401 → 깨끗이 로그아웃하고 로그인으로.
// 빈 화면/먹통 상태로 남지 않게. 로그인·가입 요청의 401(비번 오류 등)은 제외.
http.interceptors.response.use(
  (r) => r,
  (err) => {
    const url = err.config?.url || ''
    if (err.response?.status === 401 && !url.includes('/auth/')) {
      localStorage.removeItem('organt_token')
      if (!location.pathname.startsWith('/login')) location.replace('/login')
    }
    return Promise.reject(err)
  },
)

// DRF 페이지네이션 봉투({count, results}) 또는 순수 배열 모두 안전 처리.
const list = (data) => (Array.isArray(data) ? data : (data?.results ?? []))

export default {
  stats: () => http.get('/stats/').then((r) => r.data),

  // 인증(회원가입/로그인) — {me, token} 반환
  register: (p) => http.post('/auth/register/', p).then((r) => r.data),
  login: (p) => http.post('/auth/login/', p).then((r) => r.data),
  guestLogin: () => http.post('/auth/guest/').then((r) => r.data),
  logout: () => http.post('/auth/logout/').then((r) => r.data),

  // 소셜(멀티유저)
  me: () => http.get('/me/').then((r) => r.data.me),
  saveMe: (p) => http.post('/me/', p).then((r) => r.data.me),
  workspace: () => http.get('/workspace/').then((r) => r.data.channels),
  people: (q) => http.get('/people/', { params: { q } }).then((r) => r.data.people),
  friends: () => http.get('/friends/').then((r) => r.data.friends),
  addFriend: (handle) => http.post('/friends/', { handle }).then((r) => r.data),     // 친구 '요청' 보내기
  friendRequests: () => http.get('/friends/requests/').then((r) => r.data),          // {incoming, outgoing}
  acceptFriend: (handle) => http.post(`/friends/requests/${handle}/accept/`).then((r) => r.data),
  removeFriend: (handle) => http.delete(`/friends/${handle}/`).then((r) => r.data),  // 삭제/취소/거절
  members: (pid) => http.get(`/projects/${pid}/members/`).then((r) => r.data),       // {members, invited}
  invite: (pid, handle) => http.post(`/projects/${pid}/members/`, { handle }).then((r) => r.data),
  invites: () => http.get('/invites/').then((r) => r.data.invites),                  // 내가 받은 채널 초대
  acceptInvite: (pid) => http.post(`/invites/${pid}/`).then((r) => r.data),
  declineInvite: (pid) => http.delete(`/invites/${pid}/`).then((r) => r.data),

  agents: (params) => http.get('/agents/', { params }).then((r) => list(r.data)),
  agent: (botId) => http.get(`/agents/${botId}/`).then((r) => r.data),
  agentEvents: (botId) => http.get(`/agents/${botId}/events/`).then((r) => r.data),
  editAgent: (botId, payload) => http.patch(`/agents/${botId}/edit/`, payload).then((r) => r.data),  // 봇 편집(관리)
  shareAgent: (botId) => http.post(`/agents/${botId}/share/`).then((r) => r.data),  // 공개/비공개 전환

  profiles: () => http.get('/profiles/').then((r) => list(r.data)),

  projects: (params) => http.get('/projects/', { params }).then((r) => list(r.data)),
  project: (pid) => http.get(`/projects/${pid}/`).then((r) => r.data),
  projectEvents: (pid) => http.get(`/projects/${pid}/events/`).then((r) => r.data),
  briefing: (pid) => http.get(`/projects/${pid}/briefing/`).then((r) => r.data),
  collab: (pid) => http.get(`/projects/${pid}/collab/`).then((r) => r.data),  // 협업 구조(Phase 3)
  article: (pid) => http.get(`/projects/${pid}/article/`).then((r) => r.data),  // 산출물·작업 보드(배포/repo 링크 + Task)

  // 상위 Discord — 채널(프로젝트) 메시지 타임라인 + 사람 메시지 전송
  channelMessages: (pid, limit = 300) =>
    http.get(`/projects/${pid}/messages/`, { params: { limit } }).then((r) => r.data),
  say: (pid, payload) => http.post(`/projects/${pid}/say/`, payload).then((r) => r.data),

  // 스튜디오 — 봇 채용·채널 생성·요청 투입 (디스코드 제약 해제 → 무한·커스텀)
  recruit: (payload) => http.post('/recruit/', payload).then((r) => r.data),
  createChannel: (payload) => http.post('/channels/', payload).then((r) => r.data),
  makeRequest: (pid, payload) => http.post(`/projects/${pid}/request/`, payload).then((r) => r.data),
  requeueStuck: (pid) => http.post(`/projects/${pid}/requeue/`).then((r) => r.data),   // 멎은 요청 다시 맡기기
  stopWork: (pid) => http.post(`/projects/${pid}/stop/`).then((r) => r.data),          // 진행 중 작업 중지
  interject: (pid, payload) => http.post(`/projects/${pid}/interject/`, payload).then((r) => r.data),  // 진행 중 개입(정보 전달)
  // 채널 관리(관리 기능)
  renameChannel: (pid, name) => http.patch(`/projects/${pid}/rename/`, { name }).then((r) => r.data),
  archiveChannel: (pid) => http.post(`/projects/${pid}/archive/`).then((r) => r.data),
  setChannelVisibility: (pid) => http.post(`/projects/${pid}/visibility/`).then((r) => r.data),
  removeChannel: (pid) => http.delete(`/projects/${pid}/remove/`).then((r) => r.data),

  // 봉투 유지(count·다음 페이지 필요) — 협업 피드 페이지네이션
  events: (params) => http.get('/events/', { params }).then((r) => r.data),

  recommend: (q, top = 6) => http.get('/recommend/', { params: { q, top } }).then((r) => r.data),

  threads: () => http.get('/threads/').then((r) => list(r.data)),
  thread: (id) => http.get(`/threads/${id}/`).then((r) => r.data),
  createThread: (payload) => http.post('/threads/', payload).then((r) => r.data),
  addComment: (id, payload) => http.post(`/threads/${id}/comments/`, payload).then((r) => r.data),
  like: (id, payload = {}) => http.post(`/threads/${id}/like/`, payload).then((r) => r.data),
}
