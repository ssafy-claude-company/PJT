// 협업 이벤트 종류 → 라벨·색. 디자인 토큰 팔레트와 정렬(인디고/그린/앰버/레드/그레이·바이올렛).
const ACCENT = '#8f8cf5', ACCENT_BG = 'rgba(123,120,240,.14)'
const OK = '#5ec98f', OK_BG = 'rgba(82,183,136,.14)'
const WARN = '#d9a44a', WARN_BG = 'rgba(217,164,74,.12)'
const DANGER = '#e9696b', DANGER_BG = 'rgba(233,105,107,.13)'
const VIOLET = '#b88cf0', VIOLET_BG = 'rgba(184,140,240,.13)'
const GREY = '#8a8a93', GREY_BG = 'rgba(255,255,255,.045)'
const FAINT = '#65656f', FAINT_BG = 'rgba(255,255,255,.03)'
export const KIND = {
  delegation: { label: '위임', c: ACCENT, bg: ACCENT_BG },
  consultation: { label: '자문', c: VIOLET, bg: VIOLET_BG },
  work: { label: '작업', c: GREY, bg: GREY_BG },
  goal_set: { label: '목표', c: OK, bg: OK_BG },
  meeting: { label: '회의', c: ACCENT, bg: ACCENT_BG },
  verification: { label: '검증', c: WARN, bg: WARN_BG },
  deploy: { label: '배포', c: OK, bg: OK_BG },
  task_complete: { label: '완료', c: OK, bg: 'rgba(82,183,136,.2)' },
  recruit: { label: '충원', c: ACCENT, bg: ACCENT_BG },
  agent_learned: { label: '학습', c: VIOLET, bg: VIOLET_BG },
  experience_saved: { label: '경험', c: FAINT, bg: FAINT_BG },
  convergence_alert: { label: '경보', c: DANGER, bg: DANGER_BG },
  user_request: { label: '요청', c: ACCENT, bg: 'rgba(123,120,240,.2)' },
  intervention: { label: '개입', c: ACCENT, bg: ACCENT_BG },
  queued: { label: '대기', c: GREY, bg: GREY_BG },
  flow_complete: { label: '종료', c: FAINT, bg: FAINT_BG },
  recovery: { label: '복구', c: FAINT, bg: FAINT_BG },
  denied: { label: '거부', c: DANGER, bg: DANGER_BG },
  raw: { label: '기타', c: FAINT, bg: FAINT_BG },
}
export const kindMeta = (k) => KIND[k] || KIND.raw

// epoch(sec) → HH:MM:SS
export const timeFmt = (ts) => {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('ko-KR', { hour12: false })
}
export const dateFmt = (ts) => {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return d.toLocaleString('ko-KR', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
