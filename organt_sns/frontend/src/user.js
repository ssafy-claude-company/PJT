// 인증·현재 유저 스토어. 토큰을 localStorage에 저장하고 Authorization 헤더로 보낸다(api.js).
import { reactive } from 'vue'
import api from './api'

export const me = reactive({ handle: '', name: '', color: '', bio: '', is_guest: false, ready: false })

function setMe(m) {
  Object.assign(me, { handle: '', name: '', color: '', bio: '', is_guest: false }, m || {})
}

// 앱 시작 시 1회 — 저장된 토큰으로 현재 유저 복원(무효면 게스트로).
export async function loadMe() {
  const t = localStorage.getItem('organt_token') || ''
  if (t) {
    try { const m = await api.me(); if (m) setMe(m); else clearLocal() }
    catch (e) { /* 네트워크 실패 시 직전 상태 유지 */ }
  }
  me.ready = true
}

function clearLocal() {
  localStorage.removeItem('organt_token')
  setMe(null)
}

function adopt({ me: m, token }) {
  if (token) localStorage.setItem('organt_token', token)
  setMe(m)
  return m
}

export async function register(payload) { return adopt(await api.register(payload)) }
export async function login(payload) { return adopt(await api.login(payload)) }
export async function loginGuest() { return adopt(await api.guestLogin()) }
export async function saveProfile(payload) { const m = await api.saveMe(payload); setMe(m); return m }

export async function logout() {
  try { await api.logout() } catch (e) { /* 무시 */ }
  clearLocal()
}

// 인증 여부 — 핸들이 있으면(체험 계정 포함) 로그인 상태.
export const isAuthed = () => !!me.handle
