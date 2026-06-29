import { createRouter, createWebHistory } from 'vue-router'
import { isAuthed } from './user'

// 상위 Discord — 채널(프로젝트) 중심. 모든 기능은 로그인 전제(가드).
const routes = [
  { path: '/login', name: 'login', component: () => import('./pages/Auth.vue'), meta: { title: '로그인', public: true } },
  { path: '/', name: 'home', component: () => import('./pages/Channels.vue'), meta: { title: '채널' } },
  { path: '/channels/:pid', name: 'channel', component: () => import('./pages/Channel.vue'), meta: { title: '채널' } },
  { path: '/studio', name: 'studio', component: () => import('./pages/Studio.vue'), meta: { title: '직원 만들기' } },
  { path: '/agents', name: 'agents', component: () => import('./pages/Agents.vue'), meta: { title: 'AI 직원' } },
  { path: '/agents/:botId', name: 'agent', component: () => import('./pages/AgentDetail.vue'), meta: { title: '직원' } },
  { path: '/recommend', name: 'recommend', component: () => import('./pages/Recommend.vue'), meta: { title: '적임자 추천' } },
  { path: '/friends', name: 'friends', component: () => import('./pages/Friends.vue'), meta: { title: '친구' } },
  { path: '/settings', name: 'settings', component: () => import('./pages/Settings.vue'), meta: { title: '환경 변수' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() { return { top: 0 } },
})

// 인증 가드 — 로그인 안 했으면 /login으로, 이미 했으면 /login 접근 시 홈으로.
router.beforeEach((to) => {
  if (!to.meta.public && !isAuthed()) return { name: 'login', query: to.fullPath !== '/' ? { next: to.fullPath } : {} }
  if (to.name === 'login' && isAuthed()) return { name: 'home' }
})
router.afterEach((to) => { document.title = `Organt · ${to.meta.title || ''}` })
export default router
