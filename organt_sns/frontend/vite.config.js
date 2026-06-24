import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 개발 서버는 /api 를 Django(:8000)로 프록시 → CORS 없이 동일 출처처럼 호출.
// 배포 시엔 dist/ 정적 번들을 Django/정적 호스팅이 서빙(F1305).
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
