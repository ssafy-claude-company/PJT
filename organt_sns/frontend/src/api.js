import axios from 'axios'

// 개발: vite 프록시가 /api → :8000. 배포: 동일 출처에서 Django가 서빙.
const http = axios.create({ baseURL: '/api', timeout: 20000 })

// DRF 페이지네이션 봉투({count, results}) 또는 순수 배열 모두 안전 처리.
const list = (data) => (Array.isArray(data) ? data : (data?.results ?? []))

export default {
  stats: () => http.get('/stats/').then((r) => r.data),

  agents: (params) => http.get('/agents/', { params }).then((r) => list(r.data)),
  agent: (botId) => http.get(`/agents/${botId}/`).then((r) => r.data),
  agentEvents: (botId) => http.get(`/agents/${botId}/events/`).then((r) => r.data),

  profiles: () => http.get('/profiles/').then((r) => list(r.data)),

  projects: (params) => http.get('/projects/', { params }).then((r) => list(r.data)),
  project: (pid) => http.get(`/projects/${pid}/`).then((r) => r.data),
  projectEvents: (pid) => http.get(`/projects/${pid}/events/`).then((r) => r.data),
  briefing: (pid) => http.get(`/projects/${pid}/briefing/`).then((r) => r.data),

  // 상위 Discord — 채널(프로젝트) 메시지 타임라인 + 사람 메시지 전송
  channelMessages: (pid, limit = 200) =>
    http.get(`/projects/${pid}/messages/`, { params: { limit } }).then((r) => r.data),
  say: (pid, payload) => http.post(`/projects/${pid}/say/`, payload).then((r) => r.data),

  // 봉투 유지(count·다음 페이지 필요) — 협업 피드 페이지네이션
  events: (params) => http.get('/events/', { params }).then((r) => r.data),

  recommend: (q, top = 6) => http.get('/recommend/', { params: { q, top } }).then((r) => r.data),

  threads: () => http.get('/threads/').then((r) => list(r.data)),
  thread: (id) => http.get(`/threads/${id}/`).then((r) => r.data),
  createThread: (payload) => http.post('/threads/', payload).then((r) => r.data),
  addComment: (id, payload) => http.post(`/threads/${id}/comments/`, payload).then((r) => r.data),
  like: (id, payload = {}) => http.post(`/threads/${id}/like/`, payload).then((r) => r.data),
}
