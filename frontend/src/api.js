export async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  })
  const text = await res.text()
  let data
  try { data = text ? JSON.parse(text) : null } catch { data = { 明细: text } }
  if (!res.ok) {
    const msg = (data && (data.detail || data.明细)) || res.statusText
    const error = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    error.status = res.status
    error.data = data
    throw error
  }
  return data
}

export const DATA_CHANGED_EVENT = 'sortbay:data-changed'

export function notifyDataChanged(detail = {}) {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(DATA_CHANGED_EVENT, { detail }))
}

export function onDataChanged(handler) {
  if (typeof window === 'undefined') return () => {}
  window.addEventListener(DATA_CHANGED_EVENT, handler)
  return () => window.removeEventListener(DATA_CHANGED_EVENT, handler)
}
