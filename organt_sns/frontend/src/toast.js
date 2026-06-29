// 가벼운 토스트 — 액션 결과(성공/실패) 피드백. 조용히 실패하던 동작들에 신호를 준다.
import { reactive } from 'vue'

export const toasts = reactive([])
let _id = 0

export function dismiss(id) {
  const i = toasts.findIndex((t) => t.id === id)
  if (i >= 0) toasts.splice(i, 1)
}

export function toast(msg, kind = 'ok') {
  // 중복 억제 — 같은 메시지가 떠 있으면 또 안 쌓는다(폴링/연쇄 실패 도배 방지).
  const dup = toasts.find((t) => t.msg === msg && t.kind === kind)
  if (dup) return dup.id
  const id = ++_id
  toasts.push({ id, msg, kind })
  setTimeout(() => dismiss(id), kind === 'err' ? 5200 : 2800)   // 에러는 더 오래 보이게
  return id
}
