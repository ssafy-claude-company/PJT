import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './style.css'
import { loadMe } from './user'

// 라우터 가드가 동기적으로 인증 상태를 보도록, 마운트 전에 토큰→유저 복원.
loadMe().finally(() => {
  createApp(App).use(router).mount('#app')
})
