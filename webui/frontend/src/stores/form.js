import { defineStore } from 'pinia'
import { reactive, watch } from 'vue'

const KEY = 'gpt_outlook_register_form_v2'

// 跨页面共享 + localStorage 持久化的表单字段
// （proxy 在 注册 / 自动跑号 / Plus 检测 三处共用）
const defaults = {
  proxy: '',
  otpTimeout: 10,
  autoConcurrency: 1,
  autoCoolDown: 3,
  autoTargetCount: 0,
  // 注册后自动绑 2FA。单次 / 批量都**默认 true**：每个号都要 2FA。
  // 仍然拆成两个字段（而不是共用一个）：单次页是验 bug / 试流程的测试台，
  // 共用的话在那边临时关掉，回头批量跑几百个号就全裸奔了。
  // localStorage 只记住主人上次的选择，不改变默认值：清缓存后两边都回到 true。
  want2fa: true,
  autoWant2fa: true,
}

export const useFormStore = defineStore('form', () => {
  let saved = {}
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') } catch (_) { saved = {} }
  const form = reactive({ ...defaults, ...saved })

  watch(form, (v) => {
    try { localStorage.setItem(KEY, JSON.stringify(v)) } catch (_) {}
  }, { deep: true })

  return { form }
})
