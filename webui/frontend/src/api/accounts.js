import http from './request'

// ──────────────── 统计 ────────────────
export const getStats = () => http.get('/api/stats')

// ──────────────── 号池 accounts ────────────────
export const importAccounts = (text) => http.post('/api/import', { text })

export const listAccounts = (params) =>
  http.get('/api/accounts', { params }) // { status, limit, offset }

export const deleteAccount = (email) =>
  http.delete(`/api/accounts/${encodeURIComponent(email)}`)

export const bulkDeleteAccounts = (payload) =>
  http.post('/api/accounts/bulk_delete', payload) // { status } 或 { emails }

export const resetFailed = () => http.post('/api/accounts/reset_failed')

export const resetAccount = (email) =>
  http.post(`/api/accounts/reset/${encodeURIComponent(email)}`)

export const bulkResetAccounts = (emails) =>
  http.post('/api/accounts/bulk_reset', { emails })

export const releaseStale = () => http.post('/api/accounts/release_stale')


// ──────────────── iCloud Hide My Email 账号池 ────────────────
export const generateICloudAccounts = (payload) => http.post('/api/icloud/generate', payload)
export const syncICloudAccounts = (payload = {}) => http.post('/api/icloud/sync', payload)
export const listICloudAccounts = (params = {}) =>
  http.get('/api/icloud/addresses', { params })
export const resetICloudAccount = (email) =>
  http.post(`/api/icloud/addresses/reset/${encodeURIComponent(email)}`)
export const deleteICloudAccount = (email) =>
  http.delete(`/api/icloud/addresses/${encodeURIComponent(email)}`)
