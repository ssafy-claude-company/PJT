// 봇 아바타 — 이름 모노그램(이니셜) + 결정적 색(또는 선택 색). 이모지 아님.
// 이름 = 정체성, 직군 = 라벨. 아바타는 이름 기준.

export function monogram(name, role) {
  const s = String(name || role || '?').trim()
  if (/[가-힣]/.test(s)) return s.replace(/[^가-힣]/g, '').slice(0, 1) || '?'   // 한글: 첫 글자
  return s.replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase() || '?'         // 영문: 두 글자
}

function hue(seed) { let h = 0; for (const c of String(seed || '?')) h = (h * 31 + c.charCodeAt(0)) % 360; return h }
export function avatarColor(seed) { return `hsl(${hue(seed)} 42% 52%)` }

// agent.avatar 에 hex 색이 있으면 그 색, 없으면 이름에서 결정적 색.
export function avatarBg(agent) {
  const av = agent && agent.avatar
  if (av && /^#[0-9a-fA-F]{3,8}$/.test(av)) return av
  return avatarColor((agent && (agent.name || agent.role)) || '?')
}

// 스튜디오 색 선택 팔레트(이모지 대체).
export const AVATAR_COLORS = ['#7b78f0', '#5b9bd5', '#52b788', '#d9a44a', '#e9696b', '#c77dff', '#48bf9b', '#e0884a', '#6c8cff', '#b86bd9']
