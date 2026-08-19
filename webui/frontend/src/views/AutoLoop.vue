<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { autoStart, autoPause, autoResume, autoStop } from '@/api/register'
import { useFormStore, proxyText } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import LogPanel from '@/components/LogPanel.vue'
import StatusDot from '@/components/StatusDot.vue'

const router = useRouter()
const { form } = storeToRefs(useFormStore())
const proxyStore = useProxyStore()
const { count: proxyCount } = storeToRefs(proxyStore)
const runtime = useRuntimeStore()
const { autoStatus } = storeToRefs(runtime)

const st = computed(() => autoStatus.value.state || 'stopped')
const canStart = computed(() => st.value === 'stopped')
const canPause = computed(() => st.value === 'running')
const canResume = computed(() => st.value === 'paused')
const canStop = computed(() => st.value !== 'stopped')

const stateLabel = computed(() => ({
  stopped: 'Stopped', running: 'Running', paused: 'Paused',
}[st.value] || st.value))
const stateType = computed(() => ({
  stopped: 'info', running: 'success', paused: 'warning',
}[st.value] || 'info'))

const workers = computed(() => Array.isArray(autoStatus.value.workers) ? autoStatus.value.workers : [])

async function start() {
  try {
    await autoStart({
      proxy: proxyText(form.value),
      proxy_pool: proxyStore.text,
      concurrency: parseInt(form.value.autoConcurrency, 10) || 1,
      otp_timeout: parseInt(form.value.otpTimeout, 10) || 10,
      want_access_token: true,
      want_session_token: true,
      want_refresh_token: true,
      cool_down_seconds: parseFloat(form.value.autoCoolDown) || 0,
      target_count: parseInt(form.value.autoTargetCount, 10) || 0,
      // Batch registration enables 2FA by default. The backend defaults to false,
      // and this field was previously omitted. Keep the switch because enrollment is irreversible.
      want_2fa: form.value.autoWant2fa,
    })
    ElMessage.success('Automatic registration started')
  } catch (e) { ElMessage.error('Failed to start: ' + e.message) }
}
async function call(fn, name) {
  try { await fn(); ElMessage.success(name + ' successful') }
  catch (e) { ElMessage.error(name + ' failed: ' + e.message) }
}
</script>

<template>
  <div class="page">
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header><span class="section-title" style="margin: 0">Automatic Batch Registration</span></template>

      <el-space wrap :size="16" style="margin-bottom: 12px">
        <el-form-item label="Concurrency" style="margin: 0">
          <el-input-number v-model="form.autoConcurrency" :min="1" :max="20" />
        </el-form-item>
        <el-form-item label="Cooldown (seconds)" style="margin: 0">
          <el-input-number v-model="form.autoCoolDown" :min="0" :max="120" />
        </el-form-item>
        <el-form-item label="Target (0 = unlimited)" style="margin: 0">
          <el-input-number v-model="form.autoTargetCount" :min="0" :max="100000" />
        </el-form-item>
        <el-form-item label="OTP timeout (seconds)" style="margin: 0">
          <el-input-number v-model="form.otpTimeout" :min="10" :max="600" />
        </el-form-item>
      </el-space>

      <el-form-item label="2FA">
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <el-switch v-model="form.autoWant2fa" />
          <span>Automatically enable 2FA (TOTP) after each account is registered</span>
        </div>
        <div class="hint" style="margin-top: 6px; line-height: 1.5">
          Enabled by default. This action is irreversible: every future sign-in will require a six-digit code.
          The secret is shown <b>only once</b> and cannot be recovered from the server. When the run finishes,
          <b>export a backup</b> from the Registration Results page. A 2FA setup failure <b>does not invalidate the account</b>;
          it only adds a warning to the log and the account is still saved. <b>Accounts without passwords are skipped</b>,
          so this applies to each password-based account.
        </div>
      </el-form-item>

      <el-form-item label="Proxy pool">
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
          <el-tag :type="proxyCount ? 'success' : 'info'" effect="light">
            {{ proxyCount }} proxies
          </el-tag>
          <span class="hint">
            {{ proxyCount ? 'Workers rotate through the proxies in order' : 'Empty: all workers use the proxy configured on the Single Registration page' }}
          </span>
          <el-button size="small" @click="router.push('/proxy')">Manage proxy pool</el-button>
        </div>
      </el-form-item>

      <el-space wrap style="margin-top: 8px">
        <el-button type="primary" :disabled="!canStart" @click="start">Start</el-button>
        <el-button :disabled="!canPause" @click="call(autoPause, 'Pause')">Pause</el-button>
        <el-button :disabled="!canResume" @click="call(autoResume, 'Resume')">Resume</el-button>
        <el-button type="danger" :disabled="!canStop" @click="call(autoStop, 'Stop')">Stop</el-button>
      </el-space>

      <el-descriptions :column="4" border size="small" style="margin-top: 16px">
        <el-descriptions-item label="Status"><StatusDot :type="stateType" :text="stateLabel" /></el-descriptions-item>
        <el-descriptions-item label="Successful">
          <b style="color: var(--el-color-success)">{{ autoStatus.registered_ok || 0 }}</b>
          <span v-if="autoStatus.target_count"> / {{ autoStatus.target_count }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="Failed">
          <b style="color: var(--el-color-danger)">{{ autoStatus.registered_fail || 0 }}</b>
        </el-descriptions-item>
        <el-descriptions-item label="Concurrency">{{ autoStatus.concurrency || 1 }}</el-descriptions-item>
      </el-descriptions>

      <div v-if="workers.length" style="margin-top: 12px">
        <el-tag v-for="w in workers" :key="w.id" type="warning" effect="plain" style="margin: 0 6px 6px 0">
          worker-{{ w.id }} · {{ w.email }}
        </el-tag>
      </div>
      <p v-if="autoStatus.last_message" class="hint" style="margin-top: 8px">{{ autoStatus.last_message }}</p>
    </el-card>

    <el-card shadow="never">
      <LogPanel />
    </el-card>
  </div>
</template>
