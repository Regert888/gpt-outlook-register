import http from './request'

// ──────────────── Email provider configuration ────────────────
// Registered providers include capability and configuration-field declarations.
// Pages render from this metadata, so adding a backend provider requires no frontend changes.
export const getMailProviders = (pooledOnly = false) =>
  http.get('/api/mail/providers', { params: { pooled_only: pooledOnly } })

export const getMailConfig = () => http.get('/api/settings/mail')
export const saveMailConfig = (payload) => http.post('/api/settings/mail', payload)
export const testMail = () => http.post('/api/settings/mail/test')

// ──────────────── SMS verification configuration ────────────────
export const getSmsConfig = () => http.get('/api/settings/sms')
export const saveSmsConfig = (payload) => http.post('/api/settings/sms', payload)
export const testSms = () => http.post('/api/settings/sms/test')
export const getSmsTopCountries = () => http.get('/api/settings/sms/countries')
export const getSmsAllCountries = (provider = '') =>
  http.get('/api/settings/sms/all_countries', { params: { provider } })

// ──────────────── Automatic export configuration (CPA / SUB2API) ────────────────
export const getExportConfig = () => http.get('/api/settings/export')
export const saveExportConfig = (payload) => http.post('/api/settings/export', payload)
export const testExport = (target) => http.post('/api/settings/export/test', { target })
