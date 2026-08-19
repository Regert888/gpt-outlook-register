<script setup>
import { onActivated, ref } from 'vue'
import { useRoute } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { startRegister, getRegistered } from '@/api/register'
import { copyText } from '@/api/request'
import { useFormStore, proxyText } from '@/stores/form'
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
// 2FA defaults to enabled but remains optional because enrollment is irreversible.
// Store the setting in the persisted form store rather than a component-local ref;
// keep-alive preserves navigation state, while a page refresh recreates the component.

// Prefill the email when navigating from Email Account Pool → Use.
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
      proxy: proxyText(form.value),
      otp_timeout: parseInt(form.value.otpTimeout, 10) || 10,
      want_access_token: true,
      want_session_token: true,
      want_refresh_token: true,
      want_2fa: form.value.want2fa,
    })
    runtime.addLog(`[client] Registration started run_id=${r.run_id} email=${r.email}`, 'evt')
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
    if (!val) { ElMessage.warning(`${field} is empty`); return }
    await copyText(val)
  } catch (e) {
    ElMessage.error('Failed to load credentials: ' + e.message)
  }
}
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="10" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">Single Registration</span></template>
          <el-form label-position="top">
            <el-form-item label="Email (leave blank to claim the next available account)">
              <el-input v-model="regEmail" placeholder="Leave blank for automatic selection, or enter a specific email" clearable />
            </el-form-item>
            <el-form-item label="Proxy for this run (select from the pool or enter one manually; leave blank for a direct connection)">
              <el-select
                v-model="form.proxy" filterable clearable allow-create default-first-option
                :reserve-keyword="false" placeholder="socks5://user:pass@host:1080"
                style="width: 100%"
              >
                <el-option v-for="p in proxyList" :key="p" :label="p" :value="p" />
              </el-select>
              <div class="hint" style="margin-top: 4px">
                Plus eligibility checks and automatic batch registration use this as their fallback proxy. Configure proxy rotation on the Proxy Pool page.
              </div>
            </el-form-item>
            <el-form-item label="OTP timeout (seconds)">
              <el-input-number v-model="form.otpTimeout" :min="10" :max="600" />
            </el-form-item>
            <el-form-item>
              <div style="display: flex; align-items: center; gap: 10px">
                <el-switch v-model="form.want2fa" />
                <span>Automatically enable 2FA (TOTP) after registration</span>
              </div>
              <div class="hint" style="margin-top: 6px; line-height: 1.5">
                Enabled by default. This action is immediate and irreversible: <b>every future sign-in will require a six-digit code</b>.
                The secret is shown <b>only once</b> and cannot be recovered from the server. Copy and export it immediately from the result below
                or the Registration Results page, then add it to an authenticator. Losing it permanently locks 2FA access to the account.
                This applies only to <b>password-based accounts</b>; accounts without passwords are skipped.
              </div>
            </el-form-item>
            <el-button type="primary" :loading="starting || runningSingle" @click="run">
              Start Registration
            </el-button>
          </el-form>

          <el-alert
            v-if="lastRunResult && !lastRunResult.error"
            type="success" :closable="false" style="margin-top: 14px"
          >
            Registration complete: {{ lastRunResult.email }}
            (access_token length={{ lastRunResult.access_token_len }}{{ lastRunResult.partial ? ', partial credentials' : '' }})
            <div v-if="lastRunResult.password" class="cred-line">
              <span class="cred-label">Password</span><code class="cred-val">{{ lastRunResult.password }}</code>
            </div>
            <div v-else class="cred-line hint">No password was set because the server did not use the password-based registration flow.</div>
            <div v-if="lastRunResult.totp_secret" class="cred-line">
              <span class="cred-label">2FA</span><code class="cred-val">{{ lastRunResult.totp_secret }}</code>
              <span class="hint" style="margin-left: 6px">Shown only once. Copy it to an authenticator now.</span>
            </div>
            <div style="margin-top: 8px">
              <el-button size="small" @click="copyText(lastRunResult.email)">Copy Email</el-button>
              <template v-if="lastRunResult.password">
                <el-button size="small" type="primary" @click="copyText(lastRunResult.password)">Copy Password</el-button>
                <el-button size="small" @click="copyText(lastRunResult.email + '----' + lastRunResult.password)">
                  Copy email----password
                </el-button>
              </template>
              <el-button v-if="lastRunResult.access_token_len > 0" size="small"
                         @click="copyField(lastRunResult.email, 'access_token')">Copy access_token</el-button>
              <el-button v-if="lastRunResult.totp_secret" size="small" type="warning"
                         @click="copyText(lastRunResult.totp_secret)">Copy 2FA Secret</el-button>
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
