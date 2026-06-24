import { createRouter, createWebHistory } from 'vue-router'

// 상위 Discord — 채널(프로젝트) 중심. 5개 라우트(F1303/NF1304).
const routes = [
  { path: '/', name: 'home', component: () => import('./pages/Channels.vue'), meta: { title: '채널' } },
  { path: '/channels/:pid', name: 'channel', component: () => import('./pages/Channel.vue'), meta: { title: '채널' } },
  { path: '/studio', name: 'studio', component: () => import('./pages/Studio.vue'), meta: { title: '봇 스튜디오' } },
  { path: '/agents', name: 'agents', component: () => import('./pages/Agents.vue'), meta: { title: 'AI 직원' } },
  { path: '/agents/:botId', name: 'agent', component: () => import('./pages/AgentDetail.vue'), meta: { title: '직원' } },
  { path: '/recommend', name: 'recommend', component: () => import('./pages/Recommend.vue'), meta: { title: '적임자 추천' } },
  { path: '/friends', name: 'friends', component: () => import('./pages/Friends.vue'), meta: { title: '친구' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() { return { top: 0 } },
})
router.afterEach((to) => { document.title = `Organt · ${to.meta.title || ''}` })
export default router
