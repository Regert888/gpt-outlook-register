<script setup>
// Email provider configuration.
//
// This page has no provider-specific knowledge. Radio options and form fields
// come from GET /api/mail/providers and each provider's config_fields declaration,
// so adding a backend provider requires no changes here.
import { computed, onActivated, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getMailConfig, getMailProviders, saveMailConfig, testMail } from '@/api/settings'
import FooterToolbar from '@/components/FooterToolbar.vue'

const providers = ref([])
const source = ref('outlook')
const form = ref({})          // { field key: user-entered value }
const saved = ref({})         // Values returned by the backend; secret fields are masked as '***'.
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)

const current = computed(
  () => providers.value.find((p) => p.kind === source.value) || null,
)
const fields = computed(() => current.value?.config_fields || [])

// Pooled providers such as Outlook are tested through individual accounts.
// Show the connection test only for non-pooled providers.
const canTest = computed(() => !!current.value && !current.value.pooled)

/** For saved secret fields, leaving the input blank preserves the existing value. */
function phFor(f) {
  if (f.type === 'password' && saved.value[f.key] === '***') {
    return 'Already configured (leave blank to keep it)'
  }
  return f.placeholder || ''
}

async function load() {
  loading.value = true
  try {
    const [pr, cfg] = await Promise.all([getMailProviders(), getMailConfig()])
    providers.value = pr.providers || []
    saved.value = cfg.config || {}
    source.value = saved.value.mail_source || pr.current || 'outlook'

    // Leave secret inputs blank. Filling the backend mask would overwrite the real value.
    const next = {}
    for (const p of providers.value) {
      for (const f of p.config_fields) {
        next[f.key] = f.type === 'password' ? '' : (saved.value[f.key] ?? '')
      }
    }
    form.value = next
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function save() {
  const payload = { mail_source: source.value }
  for (const f of fields.value) {
    const v = (form.value[f.key] ?? '').trim()
    if (f.type === 'password' && !v) {
      // Blank means unchanged. The backend skips '***' and preserves the stored token.
      if (saved.value[f.key] === '***') continue
    }
    payload[f.key] = v
  }

  const missing = fields.value
    .filter((f) => f.required)
    .filter((f) => {
      const v = (form.value[f.key] ?? '').trim()
      return !v && !(f.type === 'password' && saved.value[f.key] === '***')
    })
  if (missing.length) {
    ElMessage.warning('Complete the following fields: ' + missing.map((f) => f.label).join(', '))
    return
  }

  saving.value = true
  try {
    await saveMailConfig(payload)
    ElMessage.success('Configuration saved')
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  try {
    const r = await testMail()
    ElMessage.success(r.message || 'Connection successful')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    testing.value = false
  }
}

onActivated(() => load())
load()
</script>

<template>
  <div class="page" v-loading="loading">
    <el-card shadow="never" style="max-width: 720px">
      <template #header>
        <span class="section-title" style="margin: 0">Email Provider Configuration</span>
      </template>
      <p class="hint">
        OpenAI registration requires an email address that can receive OTP codes. The options below are generated from providers registered by the backend.
      </p>

      <el-form label-position="top">
        <el-form-item label="Email provider">
          <el-radio-group v-model="source">
            <el-radio v-for="p in providers" :key="p.kind" :value="p.kind">
              {{ p.display_name }}
            </el-radio>
          </el-radio-group>
        </el-form-item>

        <!-- Capability summary describing how the selected provider works. -->
        <el-form-item v-if="current">
          <div class="caps">
            <el-tag size="small" :type="current.pooled ? 'warning' : 'success'">
              {{ current.pooled ? 'Account pool: import accounts and replenish them as needed' : 'Self-hosted: generates addresses automatically' }}
            </el-tag>
            <el-tag size="small" :type="current.ephemeral ? 'success' : 'info'">
              {{ current.ephemeral ? 'New address each time' : 'Fixed address' }}
            </el-tag>
            <el-tag v-if="current.line_segments > 0" size="small" type="info">
              {{ current.line_segments }}-field import format
            </el-tag>
          </div>
        </el-form-item>

        <!-- Configuration fields are entirely driven by the provider declaration. -->
        <el-form-item v-for="f in fields" :key="f.key" :label="f.label">
          <el-input
            v-model="form[f.key]"
            :type="f.type === 'password' ? 'password' : 'text'"
            :show-password="f.type === 'password'"
            :placeholder="phFor(f)"
          />
          <div v-if="f.help" class="hint" style="margin-top: 4px">{{ f.help }}</div>
        </el-form-item>

        <el-alert
          v-if="current && !current.pooled && fields.length"
          type="warning" :closable="false" show-icon
          title="For self-hosted email, configure the domain's catch-all inbox to forward mail to the server. Otherwise, verification codes will not arrive."
        />

        <el-alert
          v-if="current && current.pooled"
          type="info" :closable="false" show-icon
          :title="`${current.display_name} does not require configuration here. Add accounts from the Import Email Accounts page.`"
        />
      </el-form>
    </el-card>

    <FooterToolbar>
      <template #left>
        Email provider: {{ current?.display_name || source }}
      </template>
      <el-button v-if="canTest" :loading="testing" @click="test">Test Connection</el-button>
      <el-button type="primary" :loading="saving" @click="save">Save Configuration</el-button>
    </FooterToolbar>
  </div>
</template>

<style scoped>
.caps {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
