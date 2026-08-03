import http from './request'

// ──────────────── 邮箱来源配置 ────────────────
export const getMailConfig = () => http.get('/api/settings/mail')
export const saveMailConfig = (payload) => http.post('/api/settings/mail', payload)
export const testMail = (payload = {}) => http.post('/api/settings/mail/test', payload)


// ──────────────── SMS 接码配置 ────────────────
export const getSmsConfig = () => http.get('/api/settings/sms')
export const saveSmsConfig = (payload) => http.post('/api/settings/sms', payload)
export const testSms = () => http.post('/api/settings/sms/test')
export const getSmsTopCountries = () => http.get('/api/settings/sms/countries')
export const getSmsAllCountries = (provider = '') =>
  http.get('/api/settings/sms/all_countries', { params: { provider } })

// ──────────────── 自动导出配置 (CPA / SUB2API) ────────────────
export const getExportConfig = () => http.get('/api/settings/export')
export const saveExportConfig = (payload) => http.post('/api/settings/export', payload)
export const testExport = (target) => http.post('/api/settings/export/test', { target })
