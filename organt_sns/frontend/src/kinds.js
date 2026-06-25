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
  vote: { label: '표결', c: VIOLET, bg: VIOLET_BG },
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
  return d.toLocaleString('ko-KR', { hour12: false, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
// 날짜 인식 상대 시각 — 피드가 '시:분:초'만 나열돼 언제인지 모르는 문제 해결.
// 방금 / N분 전 / (오늘)HH:MM / 어제 HH:MM / M월 D일 HH:MM / YYYY년 M월 D일 HH:MM
export const whenFmt = (ts) => {
  if (!ts) return ''
  const d = new Date(ts * 1000), now = new Date()
  const sec = (now - d) / 1000
  if (sec < 60) return '방금'
  if (sec < 3600) return `${Math.floor(sec / 60)}분 전`
  const hm = d.toLocaleTimeString('ko-KR', { hour12: false, hour: '2-digit', minute: '2-digit' })
  if (d.toDateString() === now.toDateString()) return hm
  const y = new Date(now); y.setDate(now.getDate() - 1)
  if (d.toDateString() === y.toDateString()) return `어제 ${hm}`
  const sameYear = d.getFullYear() === now.getFullYear()
  const md = d.toLocaleDateString('ko-KR', sameYear
    ? { month: 'long', day: 'numeric' } : { year: 'numeric', month: 'long', day: 'numeric' })
  return `${md} ${hm}`
}
// epoch(sec) → 날짜 구분선 라벨(요일 포함). 같은 날 메시지 묶음의 머리글.
export const dayLabel = (ts) => {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleDateString('ko-KR', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'short' })
}
export const dayKey = (ts) => {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}.${d.getMonth()}.${d.getDate()}`
}
