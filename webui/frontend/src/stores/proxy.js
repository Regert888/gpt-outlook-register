import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

const KEY = 'dango_proxy_pool_v1'
const OLD_FORM_KEY = 'gpt_outlook_register_form_v2'

function parseLines(s) {
  return String(s || '').split('\n').map((x) => x.trim()).filter(Boolean)
}
function dedup(arr) {
  return [...new Set(arr)]
}

// Independently managed proxy list persisted in localStorage.
// Automatic runs rotate proxies by worker order via /api/auto/start proxy_pool.
export const useProxyStore = defineStore('proxy', () => {
  let saved = []
  try { saved = JSON.parse(localStorage.getItem(KEY) || '[]') } catch (_) { saved = [] }
  // Migrate the legacy Automatic Batch page's autoProxyPool textarea once.
  if (!saved.length) {
    try {
      const old = JSON.parse(localStorage.getItem(OLD_FORM_KEY) || '{}')
      if (old.autoProxyPool) saved = dedup(parseLines(old.autoProxyPool))
    } catch (_) { /* ignore */ }
  }

  const list = ref(saved)
  const text = computed(() => list.value.join('\n'))
  const count = computed(() => list.value.length)

  watch(list, (v) => {
    try { localStorage.setItem(KEY, JSON.stringify(v)) } catch (_) {}
  }, { deep: true })

  /** Replace the pool from a block of text and deduplicate it. Returns counts for feedback. */
  function setFromText(s) {
    const parsed = parseLines(s)
    const unique = dedup(parsed)
    list.value = unique
    return { total: parsed.length, kept: unique.length, duplicated: parsed.length - unique.length }
  }

  /** Append and deduplicate a batch of proxies. */
  function append(s) {
    const merged = dedup([...list.value, ...parseLines(s)])
    const added = merged.length - list.value.length
    list.value = merged
    return { added }
  }

  function remove(proxy) {
    list.value = list.value.filter((x) => x !== proxy)
  }
  function clear() {
    list.value = []
  }

  return { list, text, count, setFromText, append, remove, clear }
})

/**
 * Validate [scheme://][user:pass@]host:port proxy syntax.
 * The scheme is optional because curl treats a bare host:port as an HTTP proxy.
 */
export function isValidProxy(p) {
  return /^((socks5h?|socks4|https?):\/\/)?\S+:\d+$/i.test(p.trim())
}

/** Return the effective proxy scheme; a missing scheme defaults to HTTP. */
export function proxyScheme(p) {
  const m = /^(socks5h?|socks4|https?):\/\//i.exec(p.trim())
  return m ? m[1].toLowerCase() : 'http (default)'
}
