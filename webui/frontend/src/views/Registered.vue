<script setup>
import { computed, onActivated, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listRegistered, getRegistered, deleteRegistered,
  bulkDeleteRegistered, bulkDeleteAccounts, checkPlus, checkGCash,
  listExportFormats, exportRegistered, updateCredentials,
} from '@/api/register'
import { copyText, fmtTime } from '@/api/request'
import { useFormStore, proxyText } from '@/stores/form'
import { useProxyStore } from '@/stores/proxy'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'
import { formatGCashDetail, gcashStatusType, summarizeGCash } from '@/eligibility'

const { form } = storeToRefs(useFormStore())
// Eligibility checks must use the proxy-pool selection. A stale localStorage
// proxy can fail authentication; never silently fall back and expose the real IP.
const { list: proxyList } = storeToRefs(useProxyStore())
const runtime = useRuntimeStore()
// dataVersion needs storeToRefs for a reactive watch. bumpData is an action and
// stays on the store instance because storeToRefs converts only state/getters.
const { dataVersion } = storeToRefs(runtime)

const PAGE_SIZE = 20
const rows = ref([])
const total = ref(0)
const page = ref(1)
const filter = ref('all')
const selected = ref([])
const loading = ref(false)
const checking = ref(false)
const checkResult = ref('')
const gcashChecking = ref(false)
const gcashResult = ref('')

const PLUS_TYPE = {
  plus_eligible: 'success', plus_active: 'primary', free: 'warning',
  // Keep token_invalid separate from banned because their evidence differs, but
  // show it in red: a 401 for an unexpired token usually means revocation.
  token_invalid: 'danger',
  banned: 'danger', error: 'danger',
}
function plusOf(row) { return row.plus_check || null }
function gcashOf(row) { return row.gcash_check || null }

async function load(resetPage) {
  if (resetPage) page.value = 1
  loading.value = true
  try {
    const { items, total: t } = await listRegistered({
      limit: PAGE_SIZE, offset: (page.value - 1) * PAGE_SIZE, filter: filter.value,
    })
    rows.value = items
    total.value = t
  } catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

function collectEmails(mode) {
  if (mode === 'selected') return selected.value.map((r) => r.email)
  if (mode === 'unchecked') return rows.value.filter((r) => !plusOf(r)).map((r) => r.email)
  return rows.value.map((r) => r.email) // All accounts on the current page.
}

async function doCheck(mode) {
  const emails = collectEmails(mode)
  if (!emails.length) { ElMessage.info('There are no accounts to check on this page'); return }
  checking.value = true
  checkResult.value = `Checking Plus eligibility... (${emails.length})`
  try {
    const { results, note } = await checkPlus(emails, proxyText(form.value))
    let plus = 0, free = 0, banned = 0, failed = 0, badToken = 0
    for (const [email, info] of Object.entries(results)) {
      const row = rows.value.find((r) => r.email === email)
      if (row) row.plus_check = info
      if (info.status === 'plus_eligible' || info.status === 'plus_active') plus++
      else if (info.status === 'banned') banned++
      else if (info.status === 'free') free++
      else if (info.status === 'token_invalid') badToken++
      else if (info.status === 'error') failed++
    }
    // failed/note describe this attempt and are not persisted. badToken is a
    // persisted conclusion because an unexpired token returning 401 is revoked.
    const parts = [`Completed: ${plus} Plus available, ${free} Free, ${banned} deactivated`]
    if (badToken) parts.push(`${badToken} invalid access token(s)`)
    if (failed) parts.push(`${failed} inconclusive`)
    if (note) parts.push(note)
    checkResult.value = parts.join(' · ')
  } catch (e) {
    checkResult.value = ''
    ElMessage.error('Plus check failed: ' + e.message)
  } finally { checking.value = false }
}

async function doGCashCheck() {
  const emails = selected.value.map((row) => row.email)
  if (!emails.length) { ElMessage.info('Select at least one account'); return }
  const approved = await confirm(
    `Probe GCash eligibility for ${emails.length} selected account(s)?\n\n` +
    'This creates and updates a checkout, calculates taxes, and inspects capability metadata.\n' +
    'It never confirms the checkout, starts a custom payment method, or executes payment.',
  )
  if (!approved) return

  gcashChecking.value = true
  gcashResult.value = `Checking GCash eligibility... (${emails.length})`
  try {
    const { results } = await checkGCash(emails, proxyText(form.value))
    for (const [email, info] of Object.entries(results || {})) {
      const row = rows.value.find((item) => item.email === email)
      if (row) row.gcash_check = info
    }
    gcashResult.value = summarizeGCash(results).text
  } catch (error) {
    gcashResult.value = ''
    ElMessage.error('GCash check failed: ' + error.message)
  } finally {
    gcashChecking.value = false
  }
}

// pre-line renders message newlines. Avoid dangerouslyUseHTMLString because
// messages include data such as emails and filenames and must not create XSS.
async function confirm(msg) {
  try {
    await ElMessageBox.confirm(msg, 'Confirm', {
      type: 'warning', confirmButtonText: 'Confirm', cancelButtonText: 'Cancel',
      customClass: 'confirm-multiline',
    })
    return true
  }
  catch (_) { return false }
}
async function deleteOne(email) {
  if (!(await confirm(`Delete credentials for ${email}?`))) return
  try { await deleteRegistered(email); ElMessage.success('Deleted'); load() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(`Delete ${emails.length} selected credential record(s)? This cannot be undone.`))) return
  try { const r = await bulkDeleteRegistered({ emails }); ElMessage.success(`Deleted ${r.deleted} record(s)`); load() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteAll() {
  if (!(await confirm('This will remove every registered credential. The mailbox pool is not affected. Continue?'))) return
  if (!(await confirm('Final confirmation: permanently delete all registered credentials?'))) return
  try { const r = await bulkDeleteRegistered({ all: true }); ElMessage.success(`Deleted ${r.deleted} record(s)`); load() }
  catch (e) { ElMessage.error(e.message) }
}

// Batch export. The backend registry drives the dropdown, so new formats require
// changes only in export_formats.py.
const exportFormats = ref([])
const exporting = ref(false)
const exportVisible = ref(false)
const exportText = ref('')
const exportCount = ref(0)
const exportFilename = ref('')
const exportLabel = ref('')
// Snapshot the backend-provided exported emails. Export-all spans pages, and the
// selection/table may change while the dialog is open or new rows arrive.
const exportedEmails = ref([])
const deletingExported = ref(false)

const exportBtnText = computed(() =>
  selected.value.length ? `Export selected (${selected.value.length})` : 'Export all',
)

async function loadExportFormats() {
  if (exportFormats.value.length) return
  try {
    const { formats } = await listExportFormats()
    exportFormats.value = formats || []
  } catch (e) { ElMessage.error('Failed to load export formats: ' + e.message) }
}

async function doExport(fmt) {
  const emails = selected.value.map((r) => r.email)
  // No selection means export all pages, not just the current page.
  const payload = emails.length ? { format: fmt.id, emails } : { format: fmt.id, all: true }
  exporting.value = true
  try {
    const r = await exportRegistered(payload)
    exportedEmails.value = (r.emails || []).filter(Boolean)
    // Download mode saves binary output directly without preview.
    if (r.mode === 'download') {
      saveBlob(b64ToBytes(r.b64), r.filename, r.mime)
      ElMessage.success(`Downloaded ${r.filename} (${r.count} account(s))`)
      return
    }
    exportText.value = r.text || ''
    exportCount.value = r.count || 0
    exportFilename.value = r.filename || 'export.txt'
    exportLabel.value = r.label || fmt.label
    exportVisible.value = true
  } catch (e) { ElMessage.error('Export failed: ' + e.message) }
  finally { exporting.value = false }
}

function b64ToBytes(b64) {
  const bin = atob(b64 || '')
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes
}

function saveBlob(data, filename, mime) {
  const blob = data instanceof Blob ? data : new Blob([data], { type: mime || 'application/octet-stream' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function downloadExport() {
  saveBlob(exportText.value, exportFilename.value, 'text/plain;charset=utf-8')
}

// Download and delete. The order is mandatory: download, confirm, then delete.
// Database deletion is irreversible while a browser download can fail or be
// canceled. The confirmation reports both table counts as the final safeguard.
async function downloadAndDelete() {
  downloadExport()

  const emails = exportedEmails.value
  if (!emails.length) {
    ElMessage.warning('The export did not return an email list. The file was downloaded, but nothing was deleted.')
    return
  }

  const ok = await confirm(
    `Downloaded ${exportFilename.value}.\n\n` +
    `Delete these ${emails.length} account(s) from:\n` +
    `  · Registered credentials, including 2FA secrets\n` +
    `  · The mailbox pool, including relay links\n\n` +
    `Only the downloaded file will remain. This cannot be undone. Continue?`,
  )
  if (!ok) return

  deletingExported.value = true
  try {
    // Delete credentials first. If a corresponding pool row does not exist or its
    // deletion fails, the visible exported credential rows are still removed.
    const r1 = await bulkDeleteRegistered({ emails })
    let poolDeleted = 0
    try {
      const r2 = await bulkDeleteAccounts({ emails })
      poolDeleted = r2.deleted || 0
    } catch (e) {
      // Pool deletion failure is partial: credentials are gone, but pool rows remain.
      ElMessage.warning('Registered credentials were deleted, but mailbox-pool cleanup failed: ' + e.message)
    }
    ElMessage.success(`Deleted ${r1.deleted} credential record(s) and ${poolDeleted} mailbox record(s)`)
    exportVisible.value = false
    exportedEmails.value = []
    selected.value = []
    load(true)          // Return to page one; the old page may now be empty.
    runtime.bumpData()  // Refresh the account-pool page as well.
  } catch (e) {
    ElMessage.error('Delete failed: ' + e.message)
  } finally {
    deletingExported.value = false
  }
}

// Credential dialog.
const credVisible = ref(false)
const credEmail = ref('')
const credData = ref(null)
// Put totp_secret first because it is the only server-irrecoverable field.
const CRED_KEYS = ['totp_secret', 'totp_factor_id', 'access_token', 'session_token', 'refresh_token', 'id_token', 'device_id', 'csrf_token', 'cookie_header', 'password']
const credRows = computed(() => {
  if (!credData.value) return []
  return CRED_KEYS.filter((k) => credData.value[k]).map((k) => ({ key: k, val: credData.value[k] }))
})
async function viewCred(email) {
  try {
    const { data } = await getRegistered(email)
    credData.value = data
    credEmail.value = email
    credVisible.value = true
  } catch (e) { ElMessage.error('Failed to load credentials: ' + e.message) }
}
async function copyCell(email, field) {
  try {
    const { data } = await getRegistered(email)
    const val = data[field] || ''
    if (!val) { ElMessage.warning(`${field} is empty`); return }
    await copyText(val)
  } catch (e) { ElMessage.error('Failed to load credentials: ' + e.message) }
}
function copyAllJson() {
  if (credData.value) copyText(JSON.stringify(credData.value, null, 2))
}

// Manual credential editing changes only local storage, not OpenAI. registrar's
// login callback will use the updated local values directly.
const editVisible = ref(false)
const editSaving = ref(false)
const editEmail = ref('')
const editPassword = ref('')
const editSecret = ref('')
// Snapshot originals so unchanged fields are omitted and remain untouched.
const editOrigPassword = ref('')
const editOrigSecret = ref('')

function openEdit(row) {
  editEmail.value = row.email
  editPassword.value = row.password || ''
  editSecret.value = row.totp_secret || ''
  editOrigPassword.value = row.password || ''
  editOrigSecret.value = row.totp_secret || ''
  editVisible.value = true
}

async function saveEdit() {
  const pw = editPassword.value
  const sec = editSecret.value.trim()
  const payload = { email: editEmail.value }
  // Send only changed fields so the backend leaves everything else untouched.
  if (pw !== editOrigPassword.value) payload.password = pw
  if (sec !== editOrigSecret.value) payload.totp_secret = sec
  if (payload.password === undefined && payload.totp_secret === undefined) {
    ElMessage.info('No changes')
    editVisible.value = false
    return
  }
  // Replacing an existing irrecoverable secret can lock out 2FA, so confirm only
  // when an existing value is actually being changed.
  if (payload.totp_secret !== undefined && editOrigSecret.value) {
    try {
      await ElMessageBox.confirm(
        `This account already has a 2FA secret:\n${editOrigSecret.value}\n\n` +
        'Replacing it permanently removes the local copy of the old secret.\n' +
        'If the old secret is still active, the account may become inaccessible.',
        'Replace the 2FA secret?',
        { type: 'warning', confirmButtonText: 'Replace', cancelButtonText: 'Cancel' },
      )
    } catch { return }
  }
  editSaving.value = true
  try {
    const r = await updateCredentials(payload)
    ElMessage.success(`Saved: ${(r.changed || []).join(' + ') || 'no changes'}`)
    editVisible.value = false
    await load()
  } catch (e) {
    // Surface the backend's specific HTTP 400 validation reason unchanged.
    ElMessage.error('Save failed: ' + (e.response?.data?.detail || e.message))
  } finally { editSaving.value = false }
}

watch(page, () => load())
watch(dataVersion, () => load())
onActivated(() => load())
</script>
<template>
  <div class="page">
    <el-card shadow="never">
      <template #header><span class="section-title" style="margin: 0">Registered accounts</span></template>

      <el-space wrap style="margin-bottom: 12px">
        <el-button @click="load(false)"><el-icon><Refresh /></el-icon>Refresh</el-button>
        <el-select v-model="filter" style="width: 130px" @change="load(true)">
          <el-option label="All" value="all" />
          <el-option label="Has RT" value="has_rt" />
          <el-option label="No RT" value="no_rt" />
          <el-option label="Not checked" value="unchecked" />
          <el-option label="Free" value="free" />
          <el-option label="Plus eligible" value="plus" />
          <el-option label="Deactivated" value="banned" />
          <el-option label="Token invalid" value="token_invalid" />
        </el-select>
        <el-select
          v-model="form.proxy" filterable clearable allow-create default-first-option
          :reserve-keyword="false" placeholder="Check proxy (blank for direct)"
          style="width: 260px"
        >
          <el-option v-for="p in proxyList" :key="p" :label="p" :value="p" />
        </el-select>
        <el-button :loading="checking" @click="doCheck('unchecked')">Check unchecked</el-button>
        <el-button :loading="checking" @click="doCheck('all')">Recheck page</el-button>
        <el-button :loading="checking" :disabled="!selected.length" @click="doCheck('selected')">
          Check Plus ({{ selected.length }})
        </el-button>
        <el-button
          type="primary" plain :loading="gcashChecking"
          :disabled="!selected.length" @click="doGCashCheck"
        >
          Check GCash ({{ selected.length }})
        </el-button>
        <el-divider direction="vertical" />
        <el-dropdown trigger="click" @command="doExport" @visible-change="(v) => v && loadExportFormats()">
          <el-button :loading="exporting">
            <el-icon><Download /></el-icon>{{ exportBtnText }}
            <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-for="f in exportFormats" :key="f.id" :command="f" :divided="f.mode === 'download' && f.id === 'cpa'">
                {{ f.label }}
                <span v-if="f.note" class="hint" style="margin-left: 6px">{{ f.note }}</span>
              </el-dropdown-item>
              <el-dropdown-item v-if="!exportFormats.length" disabled>Loading...</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-divider direction="vertical" />
        <el-button type="danger" plain :disabled="!selected.length" @click="deleteSelected">
          Delete selected ({{ selected.length }})
        </el-button>
        <el-button type="danger" plain @click="deleteAll">Delete all</el-button>
        <span class="hint">{{ checkResult }}</span>
        <span class="hint">{{ gcashResult }}</span>
      </el-space>

      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table
        v-else
        v-loading="loading" :data="rows" size="small" stripe
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="44" />
        <el-table-column prop="email" label="Email" min-width="200" show-overflow-tooltip />
        <!-- Password is shown because it is required for login and is already in
             the list response. Put the icon after the text to preserve alignment. -->
        <el-table-column label="Password" min-width="170">
          <template #default="{ row }">
            <el-button
              v-if="row.password" size="small" text type="primary"
              class="cell-copy mono" @click="copyText(row.password)"
            >
              {{ row.password }}<el-icon class="ico"><CopyDocument /></el-icon>
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <!-- Show the irrecoverable 2FA secret with one-click copy. Its minimum width
             must hold a 32-character base32 value or overflow:hidden truncates it. -->
        <el-table-column label="2FA" min-width="260">
          <template #default="{ row }">
            <el-button
              v-if="row.totp_secret" size="small" text type="warning"
              class="cell-copy mono" @click="copyText(row.totp_secret)"
            >
              {{ row.totp_secret }}<el-icon class="ico"><CopyDocument /></el-icon>
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="Plus trial" width="150">
          <template #default="{ row }">
            <StatusDot v-if="plusOf(row)" :type="PLUS_TYPE[plusOf(row).status] || 'info'" :text="plusOf(row).label" />
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="GCash eligibility" width="170">
          <template #default="{ row }">
            <el-tooltip
              v-if="gcashOf(row)"
              :content="formatGCashDetail(gcashOf(row), fmtTime)"
              placement="top"
            >
              <span>
                <StatusDot
                  :type="gcashStatusType(gcashOf(row).classification)"
                  :text="gcashOf(row).label"
                />
              </span>
            </el-tooltip>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="access" width="100" align="center">
          <template #default="{ row }">
            <el-button v-if="row.at_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'access_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.at_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="session" width="100" align="center">
          <template #default="{ row }">
            <el-button v-if="row.st_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'session_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.st_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="refresh" width="100" align="center">
          <template #default="{ row }">
            <el-button v-if="row.rt_len > 0" size="small" text type="primary" @click="copyCell(row.email, 'refresh_token')">
              <el-icon><CopyDocument /></el-icon>{{ row.rt_len }}
            </el-button>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="Created" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="Actions" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text @click="viewCred(row.email)">View credentials</el-button>
            <el-button size="small" text type="warning" @click="openEdit(row)">Edit</el-button>
            <el-button size="small" text type="danger" @click="deleteOne(row.email)">Delete</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="No registered accounts yet" :image-size="70" />
        </template>
      </el-table>
      <div style="display: flex; justify-content: center; margin-top: 14px">
        <el-pagination
          v-model:current-page="page" :page-size="PAGE_SIZE" :total="total"
          layout="prev, pager, next, total" background
        />
      </div>

      <el-dialog v-model="exportVisible" width="720px" top="8vh">
        <template #header>
          <div style="display: flex; align-items: center; gap: 12px">
            <span style="font-weight: 600">Export · {{ exportLabel }}</span>
            <el-tag size="small" type="info">{{ exportCount }} row(s)</el-tag>
          </div>
        </template>
        <el-input
          :model-value="exportText" type="textarea" :rows="14" readonly
          class="mono export-area"
        />
        <template #footer>
          <el-button @click="copyText(exportText)">
            <el-icon><CopyDocument /></el-icon>Copy all
          </el-button>
          <el-button type="primary" @click="downloadExport">
            <el-icon><Download /></el-icon>Download {{ exportFilename }}
          </el-button>
          <!-- Separate the destructive action visually. Download first, then show a
               second confirmation with deletion counts for both tables. -->
          <el-button
            type="danger" plain
            :loading="deletingExported"
            :disabled="!exportedEmails.length"
            @click="downloadAndDelete"
          >
            <el-icon><Delete /></el-icon>Download and delete {{ exportedEmails.length }} account(s)
          </el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="credVisible" :title="credEmail" width="760px" top="6vh">
        <template #header>
          <div style="display: flex; align-items: center; gap: 12px">
            <span class="mono" style="font-weight: 600">{{ credEmail }}</span>
            <el-button size="small" @click="copyAllJson">Copy all JSON</el-button>
          </div>
        </template>
        <div v-for="r in credRows" :key="r.key" style="margin-bottom: 12px">
          <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 4px">
            <span class="mono" style="font-weight: 600; color: var(--dango-pink-dark)">{{ r.key }}</span>
            <el-tag size="small" type="info">len={{ r.val.length }}</el-tag>
            <el-button size="small" @click="copyText(r.val)">Copy</el-button>
          </div>
          <el-input :model-value="r.val" type="textarea" :rows="2" readonly class="mono" />
        </div>
        <el-empty v-if="!credRows.length" description="No credential fields" />
      </el-dialog>

      <!-- Record externally known credentials or correct local values. -->
      <el-dialog v-model="editVisible" title="Edit credentials" width="560px" top="10vh">
        <el-alert
          type="warning" :closable="false" show-icon style="margin-bottom: 16px"
          title="This changes only the local record; it does not update OpenAI"
          description="Changing the password here does not change the account password. The login flow will use the value you enter."
        />
        <el-form label-position="top">
          <el-form-item label="Email">
            <el-input :model-value="editEmail" class="mono" disabled />
          </el-form-item>
          <el-form-item label="Password">
            <el-input v-model="editPassword" class="mono" placeholder="Leave blank if the account has no password" />
          </el-form-item>
          <el-form-item label="2FA Secret">
            <el-input
              v-model="editSecret" class="mono"
              placeholder="Base32; spaces, lowercase, and otpauth:// links are normalized"
            />
            <div class="hint" style="margin-top: 6px; line-height: 1.6">
              This value cannot be retrieved from the server. Replacing it permanently removes the old local secret.
            </div>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="editVisible = false">Cancel</el-button>
          <el-button type="primary" :loading="editSaving" @click="saveEdit">Save</el-button>
        </template>
      </el-dialog>
    </el-card>
  </div>
</template>

<style scoped>
/* One-click copy cells for password and 2FA. :deep reaches Element Plus output.
   Reset padding because the modern .el-button.is-text inherits small-button
   padding that shifts the value away from the column's left edge. */
:deep(.el-button.cell-copy.el-button--small) {
  padding: 0 6px 0 0;
  height: 20px;
  font-size: 12px;
}
/* Keep the transparent icon's space with opacity so hover does not shift text. */
:deep(.cell-copy .ico) {
  margin-left: 5px;
  opacity: 0;
  transition: opacity 0.12s;
}
:deep(.cell-copy:hover .ico) { opacity: 0.65; }
</style>

<!-- Not scoped: ElMessageBox mounts under body. Limit styles to our customClass. -->
<style>
.confirm-multiline .el-message-box__message { white-space: pre-line; }
</style>
