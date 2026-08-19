import http from './request'

// ──────────────── Statistics ────────────────
export const getStats = () => http.get('/api/stats')

// ──────────────── Account pool ────────────────
// kind identifies the email provider (outlook, etc.). If omitted, the backend
// guesses from the field count, but Outlook and Gmail both use four fields, so the UI requires it.
export const importAccounts = (text, kind = '') =>
  http.post('/api/import', { text, kind })

export const listAccounts = (params) =>
  http.get('/api/accounts', { params }) // { status, limit, offset, kind }

export const deleteAccount = (email) =>
  http.delete(`/api/accounts/${encodeURIComponent(email)}`)

export const bulkDeleteAccounts = (payload) =>
  http.post('/api/accounts/bulk_delete', payload) // { status } or { emails }

export const resetFailed = () => http.post('/api/accounts/reset_failed')

export const resetAccount = (email) =>
  http.post(`/api/accounts/reset/${encodeURIComponent(email)}`)

export const bulkResetAccounts = (emails) =>
  http.post('/api/accounts/bulk_reset', { emails })

export const releaseStale = () => http.post('/api/accounts/release_stale')
