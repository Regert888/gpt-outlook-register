<script setup>
import { onActivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listAccounts,
  deleteAccount,
  bulkDeleteAccounts,
  resetFailed,
  resetAccount,
  bulkResetAccounts,
  releaseStale,
  generateICloudAccounts,
  syncICloudAccounts,
  listICloudAccounts,
  resetICloudAccount,
  deleteICloudAccount,
} from '@/api/accounts'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'

const router = useRouter()
const statsStore = useStatsStore()
const runtime = useRuntimeStore()
const { dataVersion } = storeToRefs(runtime)

const PAGE_SIZE = 20
const poolSource = ref('outlook')
const rows = ref([])
const total = ref(0)
const page = ref(1)
const statusFilter = ref('')
const bulkStatus = ref('')
const selected = ref([])
const loading = ref(false)
const icloudGenerating = ref(false)
const icloudSyncing = ref(false)
const icloudGenerateCount = ref(1)

const STATUS_TYPE = {
  available: 'success',
  in_use: 'warning',
  done: 'primary',
  failed: 'danger',
  trash: 'info',
}

function normalizeICloudRow(row) {
  return {
    ...row,
    source: 'icloud_hme',
    status: row.state,
  }
}

function rowSourceLabel(row) {
  if ((row.source || poolSource.value) === 'icloud_hme') return 'iCloud HME'
  return 'Outlook'
}

function statusValue(row) {
  return row.status || row.state || '-'
}

function formatTime(value) {
  if (!value) return '-'
  if (typeof value === 'number') {
    const ms = value > 1000000000000 ? value : value * 1000
    return new Date(ms).toLocaleString('zh-CN', { hour12: false })
  }
  const asNumber = Number(value)
  if (Number.isFinite(asNumber) && String(value).trim() !== '') {
    const ms = asNumber > 1000000000000 ? asNumber : asNumber * 1000
    return new Date(ms).toLocaleString('zh-CN', { hour12: false })
  }
  const date = new Date(value)
  if (!Number.isNaN(date.getTime())) return date.toLocaleString('zh-CN', { hour12: false })
  return String(value)
}

function rowUpdatedAt(row) {
  return formatTime(row.updated_at || row.synced_at || row.created_at)
}

function clearSelection() {
  selected.value = []
}

async function load(resetPage = false) {
  if (resetPage) page.value = 1
  clearSelection()
  loading.value = true
  try {
    if (poolSource.value === 'icloud_hme') {
      const params = {
        limit: PAGE_SIZE,
        offset: (page.value - 1) * PAGE_SIZE,
      }
      if (statusFilter.value) params.state = statusFilter.value
      const r = await listICloudAccounts(params)
      const items = r.items || r.addresses || r.records || []
      rows.value = items.map(normalizeICloudRow)
      total.value = r.total ?? r.count ?? items.length
    } else {
      const { items, total: t } = await listAccounts({
        status: statusFilter.value,
        limit: PAGE_SIZE,
        offset: (page.value - 1) * PAGE_SIZE,
      })
      rows.value = (items || []).map((row) => ({ ...row, source: 'outlook' }))
      total.value = t
    }
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function afterMutate() { load(false); statsStore.refresh() }

async function confirm(msg, title = '确认') {
  try { await ElMessageBox.confirm(msg, title, { type: 'warning', confirmButtonText: '确定', cancelButtonText: '取消' }); return true }
  catch (_) { return false }
}

async function resetFailedAll() {
  if (!(await confirm('把所有 failed 号重置为 available？'))) return
  try { const r = await resetFailed(); ElMessage.success(`重置 ${r.reset} 个`); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function releaseStaleAll() {
  try { const r = await releaseStale(); ElMessage.success(`释放 ${r.released} 个卡死号`); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function resetSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(`重置选中的 ${emails.length} 个号为 available？（已保存凭证不变）`))) return
  try { const r = await bulkResetAccounts(emails); ElMessage.success(`已重置 ${r.reset} 个`); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(`确定删除选中的 ${emails.length} 个号？(不可恢复)`))) return
  try { const r = await bulkDeleteAccounts({ emails }); ElMessage.success(`已删除 ${r.deleted} 个`); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function bulkDeleteByStatus() {
  if (!bulkStatus.value) { ElMessage.warning('请先选择要删除的状态'); return }
  const tip = bulkStatus.value === 'all'
    ? '这会删除邮箱列表里所有号（含未注册的），确定？'
    : `确定删除全部 ${bulkStatus.value} 状态的号？`
  if (!(await confirm(tip))) return
  try {
    const r = await bulkDeleteAccounts({ status: bulkStatus.value })
    ElMessage.success(`已删除 ${r.deleted} 个 ${bulkStatus.value} 号`)
    bulkStatus.value = ''
    afterMutate()
  } catch (e) { ElMessage.error(e.message) }
}
function useAccount(row) {
  router.push({
    path: '/register',
    query: { email: row.email, mail_source: row.source || poolSource.value },
  })
}
async function resetOne(row) {
  const email = row.email
  if (!(await confirm(`重置 ${email} 为 available？`))) return
  try {
    if ((row.source || poolSource.value) === 'icloud_hme') await resetICloudAccount(email)
    else await resetAccount(email)
    ElMessage.success('已重置')
    afterMutate()
  } catch (e) { ElMessage.error(e.message) }
}
async function deleteOne(row) {
  const email = row.email
  const isICloud = (row.source || poolSource.value) === 'icloud_hme'
  const msg = isICloud
    ? `删除本地 iCloud 账号：${email}？仅删除本地记录，iCloud 侧地址保持原状态。`
    : `删除 ${email}？`
  if (!(await confirm(msg))) return
  try {
    if (isICloud) await deleteICloudAccount(email)
    else await deleteAccount(email)
    ElMessage.success('已删除')
    afterMutate()
  } catch (e) { ElMessage.error(e.message) }
}

async function generateICloud() {
  const count = Math.max(1, Number.parseInt(icloudGenerateCount.value, 10) || 1)
  icloudGenerating.value = true
  try {
    const r = await generateICloudAccounts({ count })
    const items = r.items || r.generated || r.addresses || []
    const n = r.count ?? r.generated_count ?? items.length ?? count
    ElMessage.success(`已生成 ${n} 个 iCloud 账号`)
    await load(true)
  } catch (e) { ElMessage.error(e.message) }
  finally { icloudGenerating.value = false }
}

async function syncICloud() {
  icloudSyncing.value = true
  try {
    const r = await syncICloudAccounts()
    const n = r.synced ?? r.total ?? r.count ?? (r.items || r.addresses || []).length
    ElMessage.success(`已同步 ${n} 个 iCloud 账号`)
    await load(true)
  } catch (e) { ElMessage.error(e.message) }
  finally { icloudSyncing.value = false }
}

watch(page, () => load(false))
watch(dataVersion, () => load(false))
watch(poolSource, () => { statusFilter.value = ''; bulkStatus.value = ''; load(true) })
onActivated(() => load(false))
</script>
<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap">
          <span class="section-title" style="margin: 0">邮箱列表</span>
          <el-radio-group v-model="poolSource" size="small">
            <el-radio-button value="outlook">Outlook 接码池</el-radio-button>
            <el-radio-button value="icloud_hme">iCloud 账号池</el-radio-button>
          </el-radio-group>
        </div>
      </template>

      <el-alert
        v-if="poolSource === 'icloud_hme'"
        type="info" :closable="false" show-icon style="margin-bottom: 12px"
        title="这里管理 iCloud Hide My Email 账号池；Cookie、IMAP、label/note 等连接配置仍在「邮箱配置」里维护。"
      />

      <el-space wrap style="margin-bottom: 12px">
        <el-select v-model="statusFilter" placeholder="全部" style="width: 130px" @change="load(true)">
          <el-option label="全部" value="" />
          <el-option label="available" value="available" />
          <el-option label="in_use" value="in_use" />
          <el-option label="done" value="done" />
          <el-option label="failed" value="failed" />
          <el-option v-if="poolSource === 'icloud_hme'" label="trash" value="trash" />
        </el-select>
        <el-button @click="load(false)"><el-icon><Refresh /></el-icon>刷新</el-button>
        <template v-if="poolSource === 'outlook'">
          <el-button @click="resetFailedAll">重试 failed</el-button>
          <el-button @click="releaseStaleAll">释放卡死号</el-button>
        </template>
        <template v-else>
          <el-input-number v-model="icloudGenerateCount" :min="1" :max="50" :step="1" size="small" />
          <el-button type="primary" plain :loading="icloudGenerating" @click="generateICloud">生成 iCloud 账号</el-button>
          <el-button :loading="icloudSyncing" @click="syncICloud">同步 iCloud 账号</el-button>
        </template>
      </el-space>

      <el-space v-if="poolSource === 'outlook'" wrap style="margin-bottom: 12px">
        <el-button type="primary" plain :disabled="!selected.length" @click="resetSelected">
          重置选中 ({{ selected.length }})
        </el-button>
        <el-button type="danger" plain :disabled="!selected.length" @click="deleteSelected">
          删除选中 ({{ selected.length }})
        </el-button>
        <el-select v-model="bulkStatus" placeholder="— 按状态批量删 —" style="width: 180px">
          <el-option label="删全部 failed" value="failed" />
          <el-option label="删全部 done" value="done" />
          <el-option label="删全部 available" value="available" />
          <el-option label="删全部 in_use" value="in_use" />
          <el-option label="删全部（危险）" value="all" />
        </el-select>
        <el-button @click="bulkDeleteByStatus">执行</el-button>
      </el-space>

      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table
        v-else
        v-loading="loading" :data="rows" size="small" stripe
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column v-if="poolSource === 'outlook'" type="selection" width="44" />
        <el-table-column prop="email" label="邮箱账号" min-width="220" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.email }}</span></template>
        </el-table-column>
        <el-table-column label="来源" width="115">
          <template #default="{ row }">
            <el-tag :type="row.source === 'icloud_hme' ? 'primary' : 'success'" size="small" effect="plain">
              {{ rowSourceLabel(row) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <StatusDot :type="STATUS_TYPE[statusValue(row)] || 'info'" :text="statusValue(row)" />
          </template>
        </el-table-column>
        <el-table-column v-if="poolSource === 'icloud_hme'" prop="label" label="Label" min-width="150" show-overflow-tooltip />
        <el-table-column v-if="poolSource === 'icloud_hme'" label="Active" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small" effect="light">
              {{ row.is_active ? 'active' : 'inactive' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column v-if="poolSource === 'icloud_hme'" label="更新时间" min-width="160">
          <template #default="{ row }">{{ rowUpdatedAt(row) }}</template>
        </el-table-column>
        <el-table-column prop="fail_reason" label="失败原因" min-width="180" show-overflow-tooltip />
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button
              size="small" text
              :disabled="poolSource === 'icloud_hme' && (row.state !== 'available' || !row.is_active)"
              @click="useAccount(row)"
            >使用</el-button>
            <el-button
              v-if="poolSource === 'icloud_hme' || row.status === 'done' || row.status === 'failed'"
              size="small" text type="primary"
              :disabled="poolSource === 'icloud_hme' && (row.state === 'available' || row.state === 'in_use')"
              @click="resetOne(row)"
            >重置</el-button>
            <el-button size="small" text type="danger" @click="deleteOne(row)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty
            :description="poolSource === 'icloud_hme' ? '暂无 iCloud 账号，先同步或生成' : '暂无数据，去「导入邮箱」添加接码号'"
            :image-size="70"
          />
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
