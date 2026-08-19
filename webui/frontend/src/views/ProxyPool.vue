<script setup>
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useProxyStore, isValidProxy, proxyScheme } from '@/stores/proxy'
import { testProxies } from '@/api/proxy'
import { copyText } from '@/api/request'

const proxyStore = useProxyStore()
const { list, count } = storeToRefs(proxyStore)

const draft = ref('')
const testResults = ref({}) // proxy -> { status:'testing'|'ok'|'fail', latency_ms, ip, error }
const testingAll = ref(false)

const rows = computed(() =>
  list.value.map((p, i) => ({
    index: i + 1, proxy: p, valid: isValidProxy(p), result: testResults.value[p] || null,
  })),
)
const invalidCount = computed(() => rows.value.filter((r) => !r.valid).length)

async function runTest(targets) {
  if (!targets.length) return
  for (const p of targets) testResults.value[p] = { status: 'testing' }
  try {
    const { results } = await testProxies(targets)
    for (const [proxy, res] of Object.entries(results)) {
      testResults.value[proxy] = { status: res.ok ? 'ok' : 'fail', ...res }
    }
  } catch (e) {
    for (const p of targets) testResults.value[p] = { status: 'fail', error: e.message }
    ElMessage.error('Test failed: ' + e.message)
  }
}
async function testOne(proxy) {
  await runTest([proxy])
}
async function testAll() {
  if (!count.value) return
  testingAll.value = true
  try { await runTest([...list.value]) }
  finally { testingAll.value = false }
}

function save() {
  if (!draft.value.trim()) { ElMessage.warning('Paste at least one proxy first'); return }
  const r = proxyStore.setFromText(draft.value)
  draft.value = ''
  ElMessage.success(`Saved ${r.kept} proxies${r.duplicated ? ` (${r.duplicated} duplicates removed)` : ''}`)
}
function append() {
  if (!draft.value.trim()) { ElMessage.warning('Paste at least one proxy first'); return }
  const r = proxyStore.append(draft.value)
  draft.value = ''
  ElMessage.success(`Added ${r.added} new proxies`)
}
async function clearAll() {
  if (!count.value) return
  try {
    await ElMessageBox.confirm(`Clear all ${count.value} proxies?`, 'Confirm', { type: 'warning', confirmButtonText: 'Confirm', cancelButtonText: 'Cancel' })
    proxyStore.clear()
    ElMessage.success('Proxy pool cleared')
  } catch (_) { /* cancel */ }
}
function editInDraft() {
  draft.value = proxyStore.text
  ElMessage.info('Loaded the current proxy pool into the editor. Select Replace & Save when finished.')
}
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="10" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">Bulk Import</span></template>
          <p class="hint">
            One per line: <span class="mono">[scheme://][user:pass@]host:port</span><br />
            Proxies without a scheme default to <b>HTTP</b>. SOCKS5 proxies must include <span class="mono">socks5://</span>.<br />
            If a proxy works without a scheme but fails with <span class="mono">socks5://</span>, it is an HTTP proxy.
          </p>
          <el-input
            v-model="draft" type="textarea" :rows="12" class="mono"
            placeholder="socks5://127.0.0.1:7890&#10;socks5://user:pass@1.2.3.4:1080&#10;http://5.6.7.8:8080"
          />
          <div style="margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap">
            <el-button type="primary" @click="save">Replace & Save</el-button>
            <el-button @click="append">Append & Merge</el-button>
            <el-button @click="editInDraft">Load Current Pool</el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :md="14" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span class="section-title" style="margin: 0">
                Current Proxy Pool ({{ count }}<template v-if="invalidCount">, <span style="color: var(--el-color-danger)">{{ invalidCount }} invalid</span></template>)
              </span>
              <div style="display: flex; gap: 8px">
                <el-button size="small" type="primary" plain :loading="testingAll" :disabled="!count" @click="testAll">Test All</el-button>
                <el-button size="small" :disabled="!count" @click="copyText(proxyStore.text)">Copy All</el-button>
                <el-button size="small" type="danger" plain :disabled="!count" @click="clearAll">Clear</el-button>
              </div>
            </div>
          </template>

          <el-table :data="rows" size="small" stripe max-height="440">
            <el-table-column prop="index" label="#" width="48" />
            <el-table-column prop="proxy" label="Proxy" min-width="200" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.proxy }}</span></template>
            </el-table-column>
            <el-table-column label="Format" width="70">
              <template #default="{ row }">
                <el-tag :type="row.valid ? 'success' : 'danger'" size="small" effect="light">
                  {{ row.valid ? 'Valid' : 'Invalid' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="Scheme" width="110">
              <template #default="{ row }">
                <span class="mono" style="font-size: 12px">{{ proxyScheme(row.proxy) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="Connectivity" min-width="150">
              <template #default="{ row }">
                <template v-if="!row.result">
                  <span class="hint">Not tested</span>
                </template>
                <el-tag v-else-if="row.result.status === 'testing'" type="warning" size="small">Testing…</el-tag>
                <template v-else-if="row.result.status === 'ok'">
                  <el-tag type="success" size="small">Available {{ row.result.latency_ms }}ms</el-tag>
                  <span v-if="row.result.ip" class="hint mono" style="margin-left: 6px">{{ row.result.ip }}</span>
                </template>
                <el-tooltip v-else :content="row.result.error || 'Connection failed'" placement="top">
                  <el-tag type="danger" size="small">Failed</el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column label="Actions" width="120" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small" text type="primary"
                  :loading="row.result && row.result.status === 'testing'"
                  @click="testOne(row.proxy)"
                >Test</el-button>
                <el-button size="small" text type="danger" @click="proxyStore.remove(row.proxy)">Delete</el-button>
              </template>
            </el-table-column>
            <template #empty>No proxies. Use Bulk Import on the left to add some.</template>
          </el-table>

          <el-alert
            type="info" :closable="false" show-icon style="margin-top: 12px"
            title="During automatic batch registration, workers rotate through these proxies in order. If the pool is empty, all workers use the proxy configured on the Single Registration page."
          />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
