// 가벼운 토스트 — 액션 결과(성공/실패) 피드백. 조용히 실패하던 동작들에 신호를 준다.
import { reactive } from 'vue'

export const toasts = reactive([])
let _id = 0

export function toast(msg, kind = 'ok') {
  const id = ++_id
  toasts.push({ id, msg, kind })
  setTimeout(() => {
    const i = toasts.findIndex((t) => t.id === id)
    if (i >= 0) toasts.splice(i, 1)
  }, 2800)
}
