import { defineStore } from 'pinia'
import { reactive, watch } from 'vue'

const KEY = 'gpt_outlook_register_form_v2'

// Form fields shared across pages and persisted in localStorage.
// The proxy value is shared by registration, automatic runs, and Plus eligibility checks.
const defaults = {
  proxy: '',
  otpTimeout: 10,
  autoConcurrency: 1,
  autoCoolDown: 3,
  autoTargetCount: 0,
  // Enable 2FA after registration. Single and batch flows both default to true.
  // Keep separate settings because the single-run page is also a workflow test bench;
  // temporarily disabling it there must not disable 2FA for a later batch run.
  // localStorage remembers the last choice, while clearing storage restores both defaults to true.
  want2fa: true,
  autoWant2fa: true,
}

// A clearable el-select writes undefined rather than ''. Every proxy consumer
// expects a string and calls trim(), so normalize here to avoid duplicating guards
// and to handle stale invalid values in localStorage.
export function proxyText(form) {
  return String(form?.proxy ?? '').trim()
}

export const useFormStore = defineStore('form', () => {
  let saved = {}
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') } catch (_) { saved = {} }
  const form = reactive({ ...defaults, ...saved })

  // Normalize cleared or persisted nullish proxy values to an empty string.
  watch(() => form.proxy, (v) => {
    if (v === undefined || v === null) form.proxy = ''
  })

  watch(form, (v) => {
    try { localStorage.setItem(KEY, JSON.stringify(v)) } catch (_) {}
  }, { deep: true })

  return { form }
})
