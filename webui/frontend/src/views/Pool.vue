<script setup>
import { computed, onActivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listAccounts, deleteAccount, bulkDeleteAccounts, resetFailed,
  resetAccount, bulkResetAccounts, releaseStale,
} from '@/api/accounts'
import { getMailProviders } from '@/api/settings'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'

const router = useRouter()
const statsStore = useStatsStore()
const runtime = useRuntimeStore()
const { dataVersion } = storeToRefs(runtime)

const PAGE_SIZE = 20
const rows = ref([])
const total = ref(0)
const page = ref(1)
const statusFilter = ref('')
const kindFilter = ref('')
const bulkStatus = ref('')
const selected = ref([])
const loading = ref(false)
// The pool can contain multiple email providers. These values drive the Provider column and filter.
const providers = ref([])
const byKind = ref({})

const STATUS_TYPE = { available: 'success', in_use: 'warning', done: 'primary', failed: 'danger' }

// List only pooled providers so the filter does not contain empty choices.
const kindOptions = computed(() =>
  providers.value.filter((p) => p.pooled).map((p) => ({
    kind: p.kind,
    label: p.display_name,
    count: byKind.value[p.kind]?.total || 0,
  })),
)

function kindLabel(k) {
  return providers.value.find((p) => p.kind === k)?.display_name || k || 'outlook'
}

async function loadProviders() {
  try {
    providers.value = (await getMailProviders()).providers || []
  } catch (_) { /* Fall back to the raw kind string when provider metadata is unavailable. */ }
}

async function load(resetPage) {
  if (resetPage) page.value = 1
  loading.value = true
  try {
    const { items, total: t, by_kind } = await listAccounts({
      status: statusFilter.value,
      kind: kindFilter.value,
      limit: PAGE_SIZE,
      offset: (page.value - 1) * PAGE_SIZE,
    })
    rows.value = items
    total.value = t
    byKind.value = by_kind || {}
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function afterMutate() { load(); statsStore.refresh() }

async function confirm(msg, title = 'Confirm') {
  try { await ElMessageBox.confirm(msg, title, { type: 'warning', confirmButtonText: 'Confirm', cancelButtonText: 'Cancel' }); return true }
  catch (_) { return false }
}

async function resetFailedAll() {
  if (!(await confirm('Reset all failed accounts to available?'))) return
  try { const r = await resetFailed(); ElMessage.success(`Reset ${r.reset} accounts`); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function releaseStaleAll() {
  try { const r = await releaseStale(); ElMessage.success(`Released ${r.released} stalled accounts`); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function resetSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(`Reset ${emails.length} selected accounts to available? Saved credentials will not change.`))) return
  try { const r = await bulkResetAccounts(emails); ElMessage.success(`Reset ${r.reset} accounts`); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(`Delete ${emails.length} selected accounts? This cannot be undone.`))) return
  try { const r = await bulkDeleteAccounts({ emails }); ElMessage.success(`Deleted ${r.deleted} accounts`); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function bulkDeleteByStatus() {
  if (!bulkStatus.value) { ElMessage.warning('Select a status to delete first'); return }
  const tip = bulkStatus.value === 'all'
    ? 'Delete every account in the email pool, including unregistered accounts?'
    : `Delete all accounts with status ${bulkStatus.value}?`
  if (!(await confirm(tip))) return
  try {
    const r = await bulkDeleteAccounts({ status: bulkStatus.value })
    ElMessage.success(`Deleted ${r.deleted} ${bulkStatus.value} accounts`)
    bulkStatus.value = ''
    afterMutate()
  } catch (e) { ElMessage.error(e.message) }
}
function useAccount(email) {
  router.push({ path: '/register', query: { email } })
}
async function resetOne(email) {
  if (!(await confirm(`Reset ${email} to available?`))) return
  try { await resetAccount(email); ElMessage.success('Account reset'); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteOne(email) {
  if (!(await confirm(`Delete ${email}?`))) return
  try { await deleteAccount(email); ElMessage.success('Account deleted'); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}

watch(page, () => load())
watch(dataVersion, () => load())
onActivated(() => load())
loadProviders()
</script>
<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <span class="section-title" style="margin: 0">Email Account Pool</span>
      </template>

      <el-space wrap style="margin-bottom: 12px">
        <el-select v-model="statusFilter" placeholder="All statuses" style="width: 130px" @change="load(true)">
          <el-option label="All statuses" value="" />
          <el-option label="available" value="available" />
          <el-option label="in_use" value="in_use" />
          <el-option label="done" value="done" />
          <el-option label="failed" value="failed" />
        </el-select>
        <!-- The provider filter is useful only when the pool contains multiple providers. -->
        <el-select
          v-if="kindOptions.length > 1"
          v-model="kindFilter" placeholder="All providers" style="width: 190px" @change="load(true)"
        >
          <el-option label="All providers" value="" />
          <el-option
            v-for="o in kindOptions" :key="o.kind"
            :label="`${o.label} (${o.count})`" :value="o.kind"
          />
        </el-select>
        <el-button @click="load(false)"><el-icon><Refresh /></el-icon>Refresh</el-button>
        <el-button @click="resetFailedAll">Retry Failed</el-button>
        <el-button @click="releaseStaleAll">Release Stalled Accounts</el-button>
      </el-space>

      <el-space wrap style="margin-bottom: 12px">
        <el-button type="primary" plain :disabled="!selected.length" @click="resetSelected">
          Reset Selected ({{ selected.length }})
        </el-button>
        <el-button type="danger" plain :disabled="!selected.length" @click="deleteSelected">
          Delete Selected ({{ selected.length }})
        </el-button>
        <el-select v-model="bulkStatus" placeholder="Delete by status" style="width: 180px">
          <el-option label="Delete all failed" value="failed" />
          <el-option label="Delete all done" value="done" />
          <el-option label="Delete all available" value="available" />
          <el-option label="Delete all in_use" value="in_use" />
          <el-option label="Delete everything (dangerous)" value="all" />
        </el-select>
        <el-button @click="bulkDeleteByStatus">Apply</el-button>
      </el-space>

      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table
        v-else
        v-loading="loading" :data="rows" size="small" stripe
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="44" />
        <el-table-column prop="email" label="Email" min-width="220" show-overflow-tooltip />
        <el-table-column v-if="kindOptions.length > 1" label="Provider" width="130">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ kindLabel(row.kind) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Status" width="110">
          <template #default="{ row }">
            <StatusDot :type="STATUS_TYPE[row.status] || 'info'" :text="row.status" />
          </template>
        </el-table-column>
        <el-table-column prop="fail_reason" label="Failure reason" min-width="180" show-overflow-tooltip />
        <el-table-column label="Actions" width="220" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text @click="useAccount(row.email)">Use</el-button>
            <el-button
              v-if="row.status === 'done' || row.status === 'failed'"
              size="small" text type="primary" @click="resetOne(row.email)"
            >Reset</el-button>
            <el-button size="small" text type="danger" @click="deleteOne(row.email)">Delete</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="No accounts yet. Add accounts from Import Email Accounts." :image-size="70" />
        </template>
      </el-table>

      <div style="display: flex; justify-content: center; margin-top: 14px">
        <el-pagination
          v-model:current-page="page" :page-size="PAGE_SIZE" :total="total"
          layout="prev, pager, next, total" background
        />
      </div>
    </el-card>
  </div>
</template>
