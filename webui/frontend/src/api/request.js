import axios from 'axios'
import { ElMessage } from 'element-plus'

// ────────────────────────────────────────────────────────────
// ⭐ Single source of truth for the backend address.
// When switching to a Go backend, change only this value (or set VITE_API_BASE).
// Blank means same-origin; both the current FastAPI server and a future Gin server use the frontend port.
// ────────────────────────────────────────────────────────────
export const API_BASE = import.meta.env.VITE_API_BASE || ''

const http = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
  timeout: 60000,
})

// Normalize response bodies and error messages. Backend contract:
//   - General errors: non-2xx response with a detail field in the body.
//   - Validation errors (such as per-line import errors): 422 with
//     { message, errors: [{line, error}] } in the body.
//
// Rejected Error objects include .status and .data. Callers that need per-line
// details can read err.data.errors; existing callers can continue using err.message.
http.interceptors.response.use(
  (resp) => resp.data,
  (error) => {
    const data = error?.response?.data
    const detail =
      data?.detail ||
      data?.message ||
      error?.response?.statusText ||
      error?.message ||
      'Request failed'
    const err = new Error(detail)
    err.status = error?.response?.status
    err.data = data
    return Promise.reject(err)
  },
)

export default http

/**
 * Open an SSE connection.
 * @param {string} path Relative path, such as `/api/auto/stream`.
 * @param {Object<string, (ev: MessageEvent)=>void>} handlers Event-name-to-callback map.
 * @param {(err: Event)=>void} [onError] Error callback; the connection closes by default.
 * @returns {EventSource}
 */
export function createSSE(path, handlers = {}, onError) {
  const es = new EventSource(API_BASE + path)
  for (const [event, cb] of Object.entries(handlers)) {
    es.addEventListener(event, cb)
  }
  es.onerror = (err) => {
    if (onError) onError(err)
    else {
      try { es.close() } catch (_) {}
    }
  }
  return es
}

/** Copy text to the clipboard, with a legacy fallback. */
export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.cssText = 'position:fixed;left:-9999px'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    ElMessage.success('Copied to clipboard')
    return true
  } catch (e) {
    ElMessage.error('Copy failed: ' + e.message)
    return false
  }
}

/** Convert a timestamp in seconds to a localized English date and time. */
export function fmtTime(ts) {
  if (!ts) return '-'
  return new Date(ts * 1000).toLocaleString('en-US', { hour12: false })
}
