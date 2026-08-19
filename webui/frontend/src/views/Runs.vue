<script setup>
import { onActivated, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { listRuns } from '@/api/register'
import { fmtTime } from '@/api/request'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'

const { dataVersion } = storeToRefs(useRuntimeStore())
const rows = ref([])
const loading = ref(false)

const STATUS_TYPE = { done: 'primary', failed: 'danger', running: 'warning' }

async function load() {
  loading.value = true
  try { const { items } = await listRuns(50); rows.value = items }
  catch (e) { ElMessage.error(e.message) }
  finally { loading.value = false }
}

watch(dataVersion, () => load())
onActivated(() => load())
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; justify-content: space-between">
          <span class="section-title" style="margin: 0">Registration Runs</span>
          <el-button size="small" @click="load"><el-icon><Refresh /></el-icon>Refresh</el-button>
        </div>
      </template>
      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table v-else v-loading="loading" :data="rows" size="small" stripe>
        <el-table-column prop="run_id" label="run_id" width="180">
          <template #default="{ row }"><span class="mono">{{ row.run_id }}</span></template>
        </el-table-column>
        <el-table-column prop="email" label="Email" min-width="200" show-overflow-tooltip />
        <el-table-column label="Status" width="100">
          <template #default="{ row }">
            <StatusDot :type="STATUS_TYPE[row.status] || 'info'" :text="row.status" />
          </template>
        </el-table-column>
        <el-table-column label="Started" width="170">
          <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column prop="error" label="Error" min-width="200" show-overflow-tooltip />
        <template #empty>
          <el-empty description="No registration runs yet" :image-size="70" />
        </template>
      </el-table>
    </el-card>
  </div>
</template>
