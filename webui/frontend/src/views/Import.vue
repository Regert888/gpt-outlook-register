<script setup>
// Import accounts into the email pool.
//
// The provider selector, format hint, and placeholder all come from backend
// provider declarations, so adding a provider requires no changes here.
//
// Validation is atomic: if any line is invalid, the backend returns 422 with
// every failing line number and reason, and writes no accounts.
import { computed, onActivated, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { importAccounts } from '@/api/accounts'
import { getMailProviders } from '@/api/settings'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'

const statsStore = useStatsStore()
const runtime = useRuntimeStore()

const providers = ref([])
const kind = ref('')
const text = ref('')
const loading = ref(false)
const result = ref('')
const errors = ref([])      // [{ line, error }]

const current = computed(
  () => providers.value.find((p) => p.kind === kind.value) || null,
)

const lineCount = computed(
  () => text.value.split('\n').filter((l) => l.trim() && !l.trim().startsWith('#')).length,
)

async function loadProviders() {
  try {
    // pooled_only lists only importable providers; self-hosted providers generate their own addresses.
    const r = await getMailProviders(true)
    providers.value = r.providers || []
    // Prefer the active provider; fall back to the first provider when it does not support imports.
    const cur = r.current
    kind.value = providers.value.some((p) => p.kind === cur)
      ? cur
      : (providers.value[0]?.kind || '')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// The page uses keep-alive, so refresh providers on first mount and every
// activation to reflect provider changes made on the settings page.
onMounted(loadProviders)
onActivated(loadProviders)

async function doImport() {
  if (!text.value.trim()) {
    ElMessage.warning('Enter the accounts to import')
    return
  }
  if (!kind.value) {
    ElMessage.warning('Select an email provider first')
    return
  }
  loading.value = true
  result.value = ''
  errors.value = []
  try {
    const r = await importAccounts(text.value.trim(), kind.value)
    result.value = `Parsed ${r.parsed} lines: ${r.inserted} added, ${r.updated} updated, ${r.skipped} skipped`
    ElMessage.success('Import complete')
    text.value = ''
    statsStore.refresh()
    runtime.bumpData()
  } catch (e) {
    // A 422 response includes per-line details; other errors contain one summary message.
    if (e.status === 422 && e.data?.errors?.length) {
      errors.value = e.data.errors
      result.value = `${e.data.errors.length} invalid lines. The entire batch was rejected and nothing was imported.`
      ElMessage.error('Import rejected. Correct the errors and try again.')
    } else {
      result.value = 'Import failed: ' + e.message
      ElMessage.error(e.message)
    }
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <span class="section-title" style="margin: 0">Import Email Accounts</span>
      </template>

      <el-form label-position="top" style="margin-bottom: 4px">
        <el-form-item label="Email provider">
          <el-select v-model="kind" style="width: 260px" placeholder="Select a provider">
            <el-option
              v-for="p in providers"
              :key="p.kind"
              :label="p.display_name"
              :value="p.kind"
            />
          </el-select>
          <span class="hint" style="margin-left: 12px">
            Choose the correct provider. Different providers may use the same {{ current?.line_segments || 4 }}-field format and cannot be identified from the content alone.
          </span>
        </el-form-item>
      </el-form>

      <p class="hint" v-if="current">
        One account per line with {{ current.line_segments }} fields separated by <code>----</code>:<br />
        <code>{{ current.import_hint || '' }}</code>
      </p>

      <el-input
        v-model="text"
        type="textarea"
        :rows="12"
        class="mono"
        :placeholder="current?.import_placeholder || ''"
      />

      <div style="margin-top: 12px; display: flex; align-items: center; gap: 12px">
        <el-button type="primary" :loading="loading" @click="doImport">Import</el-button>
        <span class="hint" v-if="lineCount">{{ lineCount }} lines ready to import</span>
        <span class="hint">{{ result }}</span>
      </div>

      <!-- Per-line errors identify exactly which lines need correction. -->
      <el-alert
        v-if="errors.length"
        type="error"
        :closable="true"
        show-icon
        style="margin-top: 12px"
        title="The following lines are invalid. The entire batch was rejected and the account pool was not changed."
        @close="errors = []"
      >
        <ul class="err-list">
          <li v-for="e in errors" :key="e.line">
            <b>Line {{ e.line }}</b>: {{ e.error }}
          </li>
        </ul>
      </el-alert>
    </el-card>
  </div>
</template>

<style scoped>
.err-list {
  margin: 6px 0 0;
  padding-left: 18px;
  max-height: 220px;
  overflow-y: auto;
  line-height: 1.7;
}
</style>
