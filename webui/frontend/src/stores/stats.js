import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getStats } from '@/api/accounts'

// Account-pool totals shared by the header and dashboard, refreshed every five seconds.
export const useStatsStore = defineStore('stats', () => {
  const stats = ref({ total: 0, available: 0, in_use: 0, done: 0, failed: 0 })
  let timer = null

  async function refresh() {
    try {
      const { stats: s } = await getStats()
      if (s) stats.value = s
    } catch (e) {
      // Keep polling failures silent so they do not interrupt the user.
      console.error('stats refresh:', e)
    }
  }

  function startPolling(interval = 5000) {
    refresh()
    if (timer) clearInterval(timer)
    timer = setInterval(refresh, interval)
  }

  function stopPolling() {
    if (timer) clearInterval(timer)
    timer = null
  }

  return { stats, refresh, startPolling, stopPolling }
})
