import http from './request'
import { gcashProbeRequestConfig } from '../eligibility.js'

// ──────────────── Single registration ────────────────
export const startRegister = (payload) => http.post('/api/register', payload)

// ──────────────── Registration runs ────────────────
export const listRuns = (limit = 50) => http.get('/api/runs', { params: { limit } })

// ──────────────── Registered account results ────────────────
export const listRegistered = (params) =>
  http.get('/api/registered', { params }) // { limit, offset, filter }

export const getRegistered = (email) =>
  http.get(`/api/registered/${encodeURIComponent(email)}`)

export const deleteRegistered = (email) =>
  http.delete(`/api/registered/${encodeURIComponent(email)}`)

// Manually entered credentials: omitted fields remain unchanged; an empty string clears a field.
export const updateCredentials = (payload) =>
  http.post('/api/registered/update_credentials', payload)

export const bulkDeleteRegistered = (payload) =>
  http.post('/api/registered/bulk_delete', payload) // { emails } or { all: true }

// Post-export cleanup also removes the matching account-pool row.
// Re-exported here so Registered.vue does not need to import two API modules.
export { bulkDeleteAccounts } from './accounts'

// Bulk export formats come from backend export_formats.py, so new formats require no frontend changes.
export const listExportFormats = () => http.get('/api/registered/export/formats')
export const exportRegistered = (payload) => http.post('/api/registered/export', payload)

export const checkPlus = (emails, proxy = '') =>
  http.post('/api/registered/check_plus', { emails, proxy })

export const checkGCash = (emails, proxy = '') =>
  http.post(
    '/api/registered/check_gcash',
    { emails, proxy },
    gcashProbeRequestConfig(),
  )

export const exportToPanel = (email, targets) =>
  http.post('/api/registered/export_to_panel', { email, targets })

// ──────────────── Automatic registration loop ────────────────
export const autoStart = (payload) => http.post('/api/auto/start', payload)
export const autoPause = () => http.post('/api/auto/pause')
export const autoResume = () => http.post('/api/auto/resume')
export const autoStop = () => http.post('/api/auto/stop')
export const autoStatus = () => http.get('/api/auto/status')
