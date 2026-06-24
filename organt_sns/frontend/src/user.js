// 현재 사람 유저(멀티유저 정체성). 데모: @핸들을 localStorage에 저장하고 헤더로 보낸다.
import { reactive } from 'vue'
import api from './api'

export const me = reactive({ handle: '', name: '', color: '', bio: '', ready: false })

export async function loadMe() {
  const h = localStorage.getItem('organt_handle') || ''
  if (h) { try { const m = await api.me(); if (m) Object.assign(me, m) } catch (e) { /* keep guest */ } }
  me.ready = true
}
export async function signIn(payload) {
  const m = await api.saveMe(payload)
  localStorage.setItem('organt_handle', m.handle)
  Object.assign(me, m)
  return m
}
export function signOut() {
  localStorage.removeItem('organt_handle')
  Object.assign(me, { handle: '', name: '', color: '', bio: '' })
}
export const isGuest = () => !me.handle
