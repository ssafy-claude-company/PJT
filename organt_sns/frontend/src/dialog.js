// 스타일된 모달 — 네이티브 prompt()/confirm() 대체(다크 테마 일관). 프라미스 반환.
import { reactive } from 'vue'

export const dlg = reactive({
  open: false, mode: 'prompt', title: '', message: '', placeholder: '', value: '', danger: false, _resolve: null,
})

export function askPrompt({ title, placeholder = '', value = '' }) {
  return new Promise((res) => {
    Object.assign(dlg, { open: true, mode: 'prompt', title, message: '', placeholder, value, danger: false, _resolve: res })
  })
}
export function askConfirm({ title, message = '', danger = false }) {
  return new Promise((res) => {
    Object.assign(dlg, { open: true, mode: 'confirm', title, message, placeholder: '', value: '', danger, _resolve: res })
  })
}
// prompt → 입력 문자열(취소 null) · confirm → true/false
export function closeDialog(result) {
  dlg.open = false
  const r = dlg._resolve; dlg._resolve = null
  if (r) r(result)
}
