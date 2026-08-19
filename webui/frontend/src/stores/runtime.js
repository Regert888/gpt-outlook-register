import { defineStore } from 'pinia'
import { ref } from 'vue'
import { createSSE } from '@/api/request'
import { useStatsStore } from './stats'

let _logId = 0
const MAX_LOGS = 2000

const legacyMarker = (...codePoints) => String.fromCodePoint(...codePoints)
const LEGACY_ERROR_MARKERS = [
  legacyMarker(0x5931, 0x8d25),
  legacyMarker(0x62d2, 0x7edd),
]
const LEGACY_SUCCESS_MARKERS = [
  legacyMarker(0x6210, 0x529f),
  legacyMarker(0x5b8c, 0x6210),
  legacyMarker(0x547d, 0x4e2d),
]

function classify(line) {
  const l = (line || '').toLowerCase()
  // Preserve legacy log classification without shipping non-English string literals.
  if (l.includes('error') || l.includes('failed') || l.includes('rejected') || LEGACY_ERROR_MARKERS.some((marker) => l.includes(marker))) return 'err'
  if (l.includes('warning') || l.includes('warn')) return 'warn'
  if (l.includes('success') || l.includes('complete') || l.includes('matched') || l.includes('ok') || LEGACY_SUCCESS_MARKERS.some((marker) => l.includes(marker))) return 'ok'
  return ''
}

// Runtime state: global live log, single-run SSE, automatic-run SSE/status, and alert banner.
// Keeping it in a store prevents background runs and logs from stopping during navigation.
export const useRuntimeStore = defineStore('runtime', () => {
  const logs = ref([])            // { id, text, kind }
  const autoStatus = ref({ state: 'stopped', registered_ok: 0, registered_fail: 0 })
  const banner = ref('')          // Circuit-breaker or critical-error banner.
  const lastRunResult = ref(null) // { email, password, access_token_len, partial } or { error }.
  const dataVersion = ref(0)      // Increment to refresh pool, result, and run tables.
  const runningSingle = ref(false)

  let currentEs = null
  let autoEs = null

  function addLog(text, kind) {
    logs.value.push({ id: ++_logId, text, kind: kind ?? classify(text) })
    if (logs.value.length > MAX_LOGS) logs.value.splice(0, logs.value.length - MAX_LOGS)
  }
  function clearLogs() { logs.value = [] }
  function bumpData() { dataVersion.value++ }
  function dismissBanner() { banner.value = '' }

  // ─── SSE for a single registration run ───
  function streamRun(runId) {
    if (currentEs) { try { currentEs.close() } catch (_) {} }
    runningSingle.value = true
    const es = createSSE(`/api/runs/${runId}/stream`, {
      log: (e) => {
        try {
          const d = JSON.parse(e.data)
          if (d.line) addLog(d.line)
        } catch (_) {}
      },
      status: (e) => {
        try {
          const d = JSON.parse(e.data)
          if (d.kind === 'done') {
            lastRunResult.value = {
              email: d.email,
              password: d.password || '',
              access_token_len: d.access_token_len,
              partial: d.partial,
            }
            addLog(
              `Registration complete: ${d.email}${d.password ? ' / ' + d.password : ''}`
              + ` (access_token=${d.access_token_len}${d.partial ? ', partial credentials' : ''})`,
              'ok',
            )
          } else if (d.kind === 'error') {
            lastRunResult.value = { email: d.email, error: d.message }
            addLog('Error: ' + d.message, 'err')
          } else if (d.kind === 'phase') {
            addLog(`phase=${d.phase} email=${d.email}`, 'evt')
          }
        } catch (_) {}
      },
      end: () => {
        try { es.close() } catch (_) {}
        currentEs = null
        runningSingle.value = false
        useStatsStore().refresh()
        bumpData()
      },
    }, () => {
      try { es.close() } catch (_) {}
      currentEs = null
      runningSingle.value = false
    })
    currentEs = es
  }

  // ─── Global automatic-run SSE, connected at startup with automatic reconnect ───
  function connectAutoStream() {
    if (autoEs) { try { autoEs.close() } catch (_) {} }
    const es = createSSE('/api/auto/stream', {
      state: (e) => {
        try { autoStatus.value = JSON.parse(e.data) } catch (_) {}
      },
      run_started: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[auto] Registration started for ${d.email} (run=${d.run_id})`, 'evt')
          streamRun(d.run_id) // Reuse the single-run SSE to stream this run's logs.
        } catch (_) {}
      },
      run_finished: (e) => {
        try {
          const d = JSON.parse(e.data)
          const tag = d.ok ? '[success]' : (d.category === 'network' ? '[network error; account released]' : '[failed]')
          addLog(`[auto] ${tag} ${d.email} finished`, d.ok ? 'ok' : 'err')
          useStatsStore().refresh()
          bumpData()
        } catch (_) {}
      },
      circuit_break: (e) => {
        try {
          const d = JSON.parse(e.data)
          addLog(`[auto] Circuit breaker: ${d.reason}`, 'err')
          banner.value = d.reason
        } catch (_) {}
      },
    }, () => {
      // Reconnect automatically after a disconnect.
      try { es.close() } catch (_) {}
      autoEs = null
      setTimeout(connectAutoStream, 2000)
    })
    autoEs = es
  }

  return {
    logs, autoStatus, banner, lastRunResult, dataVersion, runningSingle,
    addLog, clearLogs, bumpData, dismissBanner, streamRun, connectAutoStream,
  }
})
