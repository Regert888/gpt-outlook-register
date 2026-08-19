import http from './request'

// Proxy connectivity tests run concurrently on the backend and may take time,
// so this endpoint uses a dedicated three-minute timeout.
export const testProxies = (proxies, timeout = 8) =>
  http.post('/api/proxy/test', { proxies, timeout }, { timeout: 180000 })
