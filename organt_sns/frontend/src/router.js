import { createRouter, createWebHistory } from 'vue-router'

// 7개 라우트(SSAFY 5+ 요건 충족) — 협업 피드/직원/직원상세/적임자추천/프로젝트/프로젝트상세/커뮤니티
const routes = [
  { path: '/', name: 'feed', component: () => import('./pages/Feed.vue'), meta: { title: '협업 피드' } },
  { path: '/agents', name: 'agents', component: () => import('./pages/Agents.vue'), meta: { title: 'AI 직원' } },
  { path: '/agents/:botId', name: 'agent', component: () => import('./pages/AgentDetail.vue'), meta: { title: '직원 상세' } },
  { path: '/recommend', name: 'recommend', component: () => import('./pages/Recommend.vue'), meta: { title: '적임자 추천' } },
  { path: '/projects', name: 'projects', component: () => import('./pages/Projects.vue'), meta: { title: '프로젝트' } },
  { path: '/projects/:pid', name: 'project', component: () => import('./pages/ProjectDetail.vue'), meta: { title: '프로젝트 상세' } },
  { path: '/community', name: 'community', component: () => import('./pages/Community.vue'), meta: { title: '커뮤니티' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior() { return { top: 0 } },
})
router.afterEach((to) => { document.title = `Organt SNS · ${to.meta.title || ''}` })
export default router
