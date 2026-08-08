<script setup>
import { onActivated, ref } from 'vue'
import { useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { startRegister, getRegistered } from '@/api/register'
import { copyText } from '@/api/request'
import { useFormStore } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import LogPanel from '@/components/LogPanel.vue'

const route = useRoute()
const { form } = storeToRefs(useFormStore())
const { list: proxyList } = storeToRefs(useProxyStore())
const runtime = useRuntimeStore()
const { runningSingle, lastRunResult } = storeToRefs(runtime)

const starting = ref(false)
const regEmail = ref('')
// 2FA 默认开（主人要求每个号都绑）。绑定不可逆，所以留开关。
// 放在 form store（localStorage 持久化）而不是组件局部 ref —— 组件是
// keep-alive 的，切页不丢，但刷新页面会重建，关了就白关。

// 从「邮箱列表 → 使用」跳转过来时，带上指定邮箱
onActivated(() => {
  if (route.query.email) regEmail.value = String(route.query.email)
})

async function run() {
  starting.value = true
  runtime.clearLogs()
  lastRunResult.value = null
  try {
    const r = await startRegister({
      email: regEmail.value.trim() || null,
      proxy: form.value.proxy.trim(),
      otp_timeout: parseInt(form.value.otpTimeout, 10) || 10,
      want_access_token: true,
      want_session_token: true,
      want_refresh_token: true,
      want_2fa: form.value.want2fa,
    })
    runtime.addLog(`[client] 启动注册 run_id=${r.run_id} email=${r.email}`, 'evt')
    runtime.streamRun(r.run_id)
  } catch (e) {
    ElMessage.error(e.message)
    lastRunResult.value = { error: e.message }
  } finally {
    starting.value = false
  }
}

async function copyField(email, field) {
  try {
    const { data } = await getRegistered(email)
    const val = data[field] || ''
    if (!val) { ElMessage.warning(`${field} 为空`); return }
    await copyText(val)
  } catch (e) {
    ElMessage.error('加载凭证失败: ' + e.message)
  }
}
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="10" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">单次注册</span></template>
          <el-form label-position="top">
            <el-form-item label="邮箱（留空 = 自动 claim 下一个 available）">
              <el-input v-model="regEmail" placeholder="留空 = 自动选号 / 或填指定邮箱" clearable />
            </el-form-item>
            <el-form-item label="本次使用的单个代理（可从代理池选，或手动输入；直连留空）">
              <el-select
                v-model="form.proxy" filterable clearable allow-create default-first-option
                :reserve-keyword="false" placeholder="socks5://user:pass@host:1080"
                style="width: 100%"
              >
                <el-option v-for="p in proxyList" :key="p" :label="p" :value="p" />
              </el-select>
              <div class="hint" style="margin-top: 4px">
                Plus 检测、自动批量的兜底代理都复用这里；批量并发轮换请到「代理池」页管理。
              </div>
            </el-form-item>
            <el-form-item label="OTP 等待秒数">
              <el-input-number v-model="form.otpTimeout" :min="10" :max="600" />
            </el-form-item>
            <el-form-item>
              <div style="display: flex; align-items: center; gap: 10px">
                <el-switch v-model="form.want2fa" />
                <span>注册成功后自动绑定 2FA（TOTP）</span>
              </div>
              <div class="hint" style="margin-top: 6px; line-height: 1.5">
                默认开。绑定不可逆、即刻生效：<b>之后该号所有登录都需 6 位动态码</b>；
                secret 仅在绑定时下发<b>一次</b>、服务端取不回，
                请在下方结果或「注册结果」页<b>立刻复制导出</b>并录入验证器，丢失 = 该号 2FA 永久锁死。
                仅对<b>有密码</b>的号生效，无密码号会自动跳过。
              </div>
            </el-form-item>
            <el-button type="primary" :loading="starting || runningSingle" @click="run">
              开始注册
            </el-button>
          </el-form>

          <el-alert
            v-if="lastRunResult && !lastRunResult.error"
            type="success" :closable="false" style="margin-top: 14px"
          >
            注册完成 {{ lastRunResult.email }}
            (access_token len={{ lastRunResult.access_token_len }}{{ lastRunResult.partial ? ', 部分凭证' : '' }})
            <div v-if="lastRunResult.password" class="cred-line">
              <span class="cred-label">密码</span><code class="cred-val">{{ lastRunResult.password }}</code>
            </div>
            <div v-else class="cred-line hint">该号未设置密码（服务端未走密码注册流程）</div>
            <div v-if="lastRunResult.totp_secret" class="cred-line">
              <span class="cred-label">2FA</span><code class="cred-val">{{ lastRunResult.totp_secret }}</code>
              <span class="hint" style="margin-left: 6px">仅此一次！务必复制录入验证器</span>
            </div>
            <div style="margin-top: 8px">
              <el-button size="small" @click="copyText(lastRunResult.email)">复制邮箱</el-button>
              <template v-if="lastRunResult.password">
                <el-button size="small" type="primary" @click="copyText(lastRunResult.password)">复制密码</el-button>
                <el-button size="small" @click="copyText(lastRunResult.email + '----' + lastRunResult.password)">
                  复制 邮箱----密码
                </el-button>
              </template>
              <el-button v-if="lastRunResult.access_token_len > 0" size="small"
                         @click="copyField(lastRunResult.email, 'access_token')">复制 access_token</el-button>
              <el-button v-if="lastRunResult.totp_secret" size="small" type="warning"
                         @click="copyText(lastRunResult.totp_secret)">复制 2FA secret</el-button>
            </div>
          </el-alert>
          <el-alert
            v-else-if="lastRunResult && lastRunResult.error"
            type="error" :closable="false" style="margin-top: 14px" :title="lastRunResult.error"
          />
        </el-card>
      </el-col>

      <el-col :md="14" style="margin-bottom: 16px">
        <el-card shadow="never">
          <LogPanel />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
